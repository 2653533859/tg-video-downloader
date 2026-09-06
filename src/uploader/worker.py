"""Background worker thread for processing upload queue."""

import logging
import os
import threading
import time
from typing import Callable, Optional, Dict, Any

from src.uploader.base import UploaderBase
from src.uploader.status import UPLOAD_TERMINAL_STATES

logger = logging.getLogger("tg_downloader.uploader.worker")


class UploadWorker:
    """Consumes upload tasks one by one in a dedicated daemon thread."""

    def __init__(
        self,
        *,
        uploader: UploaderBase,
        get_next_task: Callable[[], Optional[Dict[str, Any]]],
        update_task_state: Callable[..., Any],
        is_task_cancelled: Callable[[str], bool],
        on_task_finished: Optional[Callable[[str, str], None]] = None,
        poll_interval: float = 1.0,
    ):
        self.uploader = uploader
        self.get_next_task = get_next_task
        self.update_task_state = update_task_state
        self.is_task_cancelled = is_task_cancelled
        self.on_task_finished = on_task_finished
        self.poll_interval = poll_interval

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current_task_id: Optional[str] = None
        self._lock = threading.RLock()

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="UploadWorker", daemon=True)
            self._thread.start()
            logger.info("UploadWorker 已启动")

    def stop(self, timeout: float = 5.0):
        with self._lock:
            self._stop_event.set()
            current_id = self._current_task_id
        if current_id:
            try:
                self.uploader.cancel(current_id)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            logger.info("UploadWorker 已停止")

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def get_current_task_id(self) -> Optional[str]:
        with self._lock:
            return self._current_task_id

    def _run_loop(self):
        while not self._stop_event.is_set():
            task = None
            try:
                task = self.get_next_task()
            except Exception as exc:
                logger.error(f"获取下一个上传任务失败: {exc}")

            if not task:
                self._stop_event.wait(self.poll_interval)
                continue

            upload_id = task.get("upload_id")
            with self._lock:
                self._current_task_id = upload_id

            try:
                self._execute_task(task)
            except Exception as exc:
                logger.exception(f"执行上传任务异常 [{upload_id}]: {exc}")
                self.update_task_state(
                    upload_id,
                    status="error",
                    error=str(exc),
                    finished_at=time.time(),
                )
            finally:
                with self._lock:
                    self._current_task_id = None
                if self.on_task_finished and upload_id:
                    try:
                        self.on_task_finished(upload_id, task.get("filepath", ""))
                    except Exception:
                        pass

    def _execute_task(self, task: Dict[str, Any]):
        upload_id = task["upload_id"]
        filepath = task["filepath"]
        remote_dir = task.get("remote_dir", "")
        delete_local = bool(task.get("delete_local", False))

        if self.is_task_cancelled(upload_id):
            self.update_task_state(upload_id, status="cancelled", error="已取消", finished_at=time.time())
            return

        if not os.path.exists(filepath):
            self.update_task_state(
                upload_id,
                status="error",
                error=f"本地文件不存在: {filepath}",
                finished_at=time.time(),
            )
            return

        total_bytes = os.path.getsize(filepath)
        self.update_task_state(
            upload_id,
            status="uploading",
            progress=0.0,
            total_bytes=total_bytes,
            error="",
        )

        last_report_time = 0.0

        def progress_cb(info: Dict[str, Any]):
            nonlocal last_report_time
            now = time.time()
            if now - last_report_time < 0.8 and info.get("percentage", 0) < 100:
                return
            last_report_time = now
            self.update_task_state(
                upload_id,
                progress=info.get("percentage", 0.0),
                speed=info.get("speed", ""),
                speed_bps=info.get("speed_bps", 0.0),
                bytes_transferred=info.get("bytes", 0),
                total_bytes=info.get("total_bytes", total_bytes),
            )

        def check_cancelled() -> bool:
            return self._stop_event.is_set() or self.is_task_cancelled(upload_id)

        result = self.uploader.upload(
            local_path=filepath,
            remote_dir=remote_dir,
            progress_callback=progress_cb,
            is_cancelled=check_cancelled,
            delete_on_success=delete_local,
            task_key=upload_id,
        )

        finish_time = time.time()
        if result.get("cancelled") or check_cancelled():
            self.update_task_state(
                upload_id,
                status="cancelled",
                error="上传已取消",
                speed="",
                speed_bps=0.0,
                finished_at=finish_time,
            )
        elif result.get("success"):
            self.update_task_state(
                upload_id,
                status="done",
                progress=100.0,
                speed="",
                speed_bps=0.0,
                bytes_transferred=total_bytes,
                error="",
                finished_at=finish_time,
            )
        else:
            self.update_task_state(
                upload_id,
                status="error",
                error=result.get("error") or "上传失败",
                speed="",
                speed_bps=0.0,
                finished_at=finish_time,
            )
