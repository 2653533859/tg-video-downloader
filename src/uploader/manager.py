"""Upload queue manager coordinating tasks, persistence, and worker."""

import logging
import os
import threading
from typing import Optional, Dict, Any, List, Set

from src.uploader.base import UploaderBase
from src.uploader.config import UploaderConfig
from src.uploader.persistence import UploadPersistence
from src.uploader.rclone import RcloneUploader
from src.uploader.status import (
    create_upload_task,
    make_upload_id,
    UPLOAD_TERMINAL_STATES,
    UPLOAD_ACTIVE_STATES,
)
from src.uploader.auth import RcloneAuthManager
from src.uploader.worker import UploadWorker

logger = logging.getLogger("tg_downloader.uploader.manager")


class UploadManager:
    """High-level upload coordinator for queueing, executing, and tracking uploads."""

    def __init__(
        self,
        config: Optional[UploaderConfig] = None,
        uploader: Optional[UploaderBase] = None,
        persistence: Optional[UploadPersistence] = None,
    ):
        self.config = config or UploaderConfig.from_env()
        self.uploader = uploader or RcloneUploader(self.config)
        self.persistence = persistence or UploadPersistence()
        config_path = self.config.resolve_config_path()
        binary_path = self.config.resolve_binary()
        self.auth = RcloneAuthManager(
            config_path=config_path,
            remote_name=self.config.remote_name,
            binary_path=binary_path,
        )

        self._queue: List[str] = []  # List of upload_ids
        self._cancelled: Set[str] = set()
        self._lock = threading.RLock()

        self.worker = UploadWorker(
            uploader=self.uploader,
            get_next_task=self._get_next_task,
            update_task_state=self._update_task_state,
            is_task_cancelled=self._is_task_cancelled,
            on_task_finished=self._on_task_finished,
        )

        # Restore any pending tasks from persistence upon startup
        self._restore_pending_tasks()

        if self.config.enabled:
            self.start()
    def start(self):
        """Start the background upload worker."""
        if self.config.enabled:
            self.worker.start()
        else:
            logger.info("Google Drive 上传功能未开启 (GDRIVE_ENABLED=false)")

    def stop(self, timeout: float = 5.0):
        """Stop the background upload worker."""
        self.worker.stop(timeout=timeout)

    def _restore_pending_tasks(self):
        with self._lock:
            pending_tasks = self.persistence.list_tasks(limit=100, status="pending")
            for t in reversed(pending_tasks):
                uid = t["upload_id"]
                if uid not in self._queue:
                    self._queue.append(uid)
            # Requeue any tasks that were left in 'uploading' during previous shutdown
            uploading_tasks = self.persistence.list_tasks(limit=100, status="uploading")
            for t in uploading_tasks:
                uid = t["upload_id"]
                t["status"] = "pending"
                self.persistence.save_task(t)
                if uid not in self._queue:
                    self._queue.append(uid)

    def enqueue(
        self,
        filepath: str,
        folder: str,
        filename: str,
        remote_dir: Optional[str] = None,
        delete_local: Optional[bool] = None,
        task_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Enqueue a file for upload.
        """
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"本地待上传文件不存在: {filepath}")

        # Default remote_dir to folder if not provided
        dest_remote_dir = remote_dir if remote_dir is not None else folder
        should_delete = (
            self.config.delete_local_after_upload
            if delete_local is None
            else bool(delete_local)
        )

        upload_id = make_upload_id(filepath, task_id)

        with self._lock:
            existing = self.persistence.get_task(upload_id)
            if existing and existing.get("status") in UPLOAD_ACTIVE_STATES:
                return existing

            total_bytes = os.path.getsize(filepath) if os.path.isfile(filepath) else 0
            task = create_upload_task(
                filepath=filepath,
                folder=folder,
                filename=filename,
                remote_dir=dest_remote_dir,
                total_bytes=total_bytes,
                delete_local=should_delete,
                task_id=task_id,
            )
            self._cancelled.discard(upload_id)
            self.persistence.save_task(task)
            if upload_id not in self._queue:
                self._queue.append(upload_id)

        logger.info(f"上传任务已入队 [{upload_id}] {filename} -> {dest_remote_dir}")
        if self.config.enabled and not self.worker.is_alive():
            self.worker.start()

        return task

    def maybe_auto_enqueue(
        self,
        filepath: str,
        folder: str,
        filename: str,
        task_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Called after download completion. If auto_upload is enabled, automatically enqueue.
        """
        if not self.config.enabled or not self.config.auto_upload:
            return None
        try:
            return self.enqueue(
                filepath=filepath,
                folder=folder,
                filename=filename,
                delete_local=self.config.delete_local_after_upload,
                task_id=task_id,
            )
        except Exception as exc:
            logger.error(f"自动入队上传失败 [{filename}]: {exc}")
            return None

    def cancel(self, upload_id: str) -> bool:
        """Cancel an upload task."""
        with self._lock:
            self._cancelled.add(upload_id)
            if upload_id in self._queue:
                self._queue.remove(upload_id)
            task = self.persistence.get_task(upload_id)
            if task and task.get("status") in UPLOAD_ACTIVE_STATES:
                task["status"] = "cancelled"
                task["error"] = "用户取消"
                self.persistence.save_task(task)
        self.uploader.cancel(upload_id)
        logger.info(f"已取消上传任务 [{upload_id}]")
        return True

    def retry(self, upload_id: str) -> Optional[Dict[str, Any]]:
        """Retry a failed or cancelled upload task."""
        with self._lock:
            task = self.persistence.get_task(upload_id)
            if not task:
                return None
            if not os.path.exists(task.get("filepath", "")):
                task["error"] = "本地文件已不存在，无法重试"
                self.persistence.save_task(task)
                return task

            task["status"] = "pending"
            task["progress"] = 0.0
            task["error"] = ""
            task["speed"] = ""
            task["speed_bps"] = 0.0
            self._cancelled.discard(upload_id)
            self.persistence.save_task(task)
            if upload_id not in self._queue:
                self._queue.append(upload_id)

        if self.config.enabled and not self.worker.is_alive():
            self.worker.start()

        return task

    def get_task(self, upload_id: str) -> Optional[Dict[str, Any]]:
        return self.persistence.get_task(upload_id)

    def get_task_by_filepath(self, filepath: str) -> Optional[Dict[str, Any]]:
        return self.persistence.get_by_filepath(filepath)

    def get_status_payload(self) -> Dict[str, Any]:
        """Return real-time status summary for UI and API."""
        with self._lock:
            current_id = self.worker.get_current_task_id()
            current_task = self.persistence.get_task(current_id) if current_id else None

            queue_tasks = []
            for qid in self._queue:
                t = self.persistence.get_task(qid)
                if t and t.get("status") == "pending":
                    queue_tasks.append(t)

            recent_finished = self.persistence.list_tasks(limit=10)

        return {
            "enabled": self.config.enabled,
            "configured": self.auth.get_info().get("configured", False),
            "auto_upload": self.config.auto_upload,
            "remote": self.config.remote_name,
            "target_dir": self.config.target_dir,
            "delete_local": self.config.delete_local_after_upload,
            "current": current_task,
            "queue_count": len(queue_tasks),
            "queue": queue_tasks[:20],
            "recent": recent_finished,
        }

    def _get_next_task(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            while self._queue:
                uid = self._queue.pop(0)
                if uid in self._cancelled:
                    continue
                task = self.persistence.get_task(uid)
                if task and task.get("status") == "pending":
                    return task
            return None

    def _update_task_state(self, upload_id: str, **updates):
        with self._lock:
            task = self.persistence.get_task(upload_id)
            if not task:
                return
            task.update(updates)
            self.persistence.save_task(task)

    def _is_task_cancelled(self, upload_id: str) -> bool:
        with self._lock:
            return upload_id in self._cancelled

    def _on_task_finished(self, upload_id: str, filepath: str):
        with self._lock:
            self._cancelled.discard(upload_id)
