"""Unit tests for UploadManager, UploadWorker, and UploadPersistence."""

import os
import time
import pytest
from unittest.mock import Mock, patch, MagicMock

from src.uploader.config import UploaderConfig
from src.uploader.persistence import UploadPersistence
from src.uploader.status import make_upload_id, create_upload_task
from src.uploader.worker import UploadWorker
from src.uploader.manager import UploadManager
from src.download.worker import DownloadWorker


class TestUploadPersistence:
    def test_save_get_and_list_tasks(self, tmp_path):
        db_path = str(tmp_path / "test_uploads.sqlite3")
        store = UploadPersistence(db_path)

        task = create_upload_task(
            filepath="/tmp/video1.mp4",
            folder="channel_1",
            filename="video1.mp4",
            remote_dir="TG_Downloads/channel_1",
            total_bytes=1000,
        )
        store.save_task(task)

        loaded = store.get_task(task["upload_id"])
        assert loaded is not None
        assert loaded["filename"] == "video1.mp4"
        assert loaded["status"] == "pending"

        # Update status
        task["status"] = "uploading"
        task["progress"] = 45.5
        store.save_task(task)

        updated = store.get_task(task["upload_id"])
        assert updated["status"] == "uploading"
        assert updated["progress"] == 45.5

        # Query by filepath
        by_fp = store.get_by_filepath("/tmp/video1.mp4")
        assert by_fp is not None
        assert by_fp["upload_id"] == task["upload_id"]

        # List tasks
        all_tasks = store.list_tasks(limit=10)
        assert len(all_tasks) == 1

        active_tasks = store.list_tasks(status="uploading")
        assert len(active_tasks) == 1

        # Delete task
        store.delete_task(task["upload_id"])
        assert store.get_task(task["upload_id"]) is None


class TestUploadWorker:
    def test_worker_run_success(self, tmp_path):
        test_file = tmp_path / "sample.mp4"
        test_file.write_text("dummy video")

        mock_uploader = Mock()
        mock_uploader.upload.return_value = {"success": True, "error": ""}

        tasks = [{
            "upload_id": "up_1",
            "filepath": str(test_file),
            "folder": "chan",
            "filename": "sample.mp4",
            "remote_dir": "remote_chan",
            "status": "pending",
            "delete_local": False,
        }]

        updates = []

        def get_task():
            return tasks.pop(0) if tasks else None

        def update_state(uid, **kwargs):
            updates.append((uid, kwargs))

        worker = UploadWorker(
            uploader=mock_uploader,
            get_next_task=get_task,
            update_task_state=update_state,
            is_task_cancelled=lambda uid: False,
            poll_interval=0.1,
        )

        worker.start()
        time.sleep(0.3)
        worker.stop(timeout=1.0)

        mock_uploader.upload.assert_called_once()
        # Verify status transitions
        statuses = [kwargs.get("status") for uid, kwargs in updates if "status" in kwargs]
        assert "uploading" in statuses
        assert "done" in statuses

    def test_worker_handles_cancelled_task(self, tmp_path):
        test_file = tmp_path / "sample.mp4"
        test_file.write_text("dummy")

        mock_uploader = Mock()
        tasks = [{
            "upload_id": "up_cancelled",
            "filepath": str(test_file),
            "status": "pending",
        }]
        updates = []

        worker = UploadWorker(
            uploader=mock_uploader,
            get_next_task=lambda: tasks.pop(0) if tasks else None,
            update_task_state=lambda uid, **kwargs: updates.append((uid, kwargs)),
            is_task_cancelled=lambda uid: True,  # Already cancelled
            poll_interval=0.1,
        )

        worker.start()
        time.sleep(0.3)
        worker.stop(timeout=1.0)

        mock_uploader.upload.assert_not_called()
        statuses = [kwargs.get("status") for uid, kwargs in updates if "status" in kwargs]
        assert "cancelled" in statuses


class TestUploadManager:
    def test_enqueue_and_status(self, tmp_path):
        db_path = str(tmp_path / "uploads_test.sqlite3")
        store = UploadPersistence(db_path)
        cfg = UploaderConfig(enabled=False, auto_upload=False)
        mock_uploader = Mock()

        mgr = UploadManager(config=cfg, uploader=mock_uploader, persistence=store)

        test_file = tmp_path / "vid.mp4"
        test_file.write_text("content")

        # 1. Enqueue
        task = mgr.enqueue(str(test_file), "chan", "vid.mp4")
        assert task["status"] == "pending"
        assert task["filename"] == "vid.mp4"

        # 2. Duplicate enqueue returns existing
        dup = mgr.enqueue(str(test_file), "chan", "vid.mp4")
        assert dup["upload_id"] == task["upload_id"]

        # 3. Status payload
        status = mgr.get_status_payload()
        assert status["queue_count"] == 1
        assert len(status["queue"]) == 1
        assert status["queue"][0]["upload_id"] == task["upload_id"]

        # 4. Cancel
        assert mgr.cancel(task["upload_id"]) is True
        cancelled_task = mgr.get_task(task["upload_id"])
        assert cancelled_task["status"] == "cancelled"

        # 5. Retry
        retried = mgr.retry(task["upload_id"])
        assert retried is not None
        assert retried["status"] == "pending"

    def test_maybe_auto_enqueue(self, tmp_path):
        db_path = str(tmp_path / "uploads_test.sqlite3")
        store = UploadPersistence(db_path)
        test_file = tmp_path / "vid.mp4"
        test_file.write_text("content")

        # When auto_upload is False
        cfg_disabled = UploaderConfig(enabled=True, auto_upload=False)
        mgr_disabled = UploadManager(config=cfg_disabled, persistence=store)
        assert mgr_disabled.maybe_auto_enqueue(str(test_file), "chan", "vid.mp4") is None

        # When auto_upload is True
        cfg_enabled = UploaderConfig(enabled=True, auto_upload=True)
        mgr_enabled = UploadManager(config=cfg_enabled, persistence=store)
        res = mgr_enabled.maybe_auto_enqueue(str(test_file), "chan", "vid.mp4")
        assert res is not None
        assert res["status"] == "pending"


class TestDownloadWorkerCompletionHook:
    def test_download_worker_triggers_hook(self, tmp_path):
        hook_calls = []

        def mock_hook(task_id, filepath, dialog_name, filename):
            hook_calls.append({
                "task_id": task_id,
                "filepath": filepath,
                "dialog_name": dialog_name,
                "filename": filename,
            })

        mock_tdl = Mock()
        save_dir = str(tmp_path / "save")
        os.makedirs(save_dir, exist_ok=True)
        target_file = os.path.join(save_dir, "vid.mp4")
        with open(target_file, "w") as f:
            f.write("content")

        worker = DownloadWorker(
            download_dir_for_dialog=lambda d: save_dir,
            release_tasks=lambda tasks: None,
            process_queue=lambda: None,
            copy_task_state=lambda tid: {},
            set_task_state=lambda tid, state: None,
            update_task_state=lambda tid, **kwargs: None,
            is_cancelled=lambda tid: False,
            get_cached_message=lambda mid, eid: None,
            resolve_message=lambda eid, mid: None,
            get_video_info=lambda msg: None,
            supports_tdl_download=lambda eid: False,
            download_with_telegram=lambda tid, eid, mid, dname, info, fpath: None,
            tdl_executor=lambda: mock_tdl,
            save_resume_info=lambda tid, info: None,
            format_size=lambda s: "1MB",
            log_info=lambda msg: None,
            log_error=lambda msg: None,
            on_download_complete=mock_hook,
        )

        task = {
            "task_id": "test_1",
            "entity_id": 123,
            "msg_id": 456,
            "info": {"filename": "vid.mp4", "size": 7},
            "downloader": "telegram",
        }

        worker._run_one(task, "test_dialog", save_dir)

        assert len(hook_calls) == 1
        assert hook_calls[0]["task_id"] == "test_1"
        assert hook_calls[0]["filename"] == "vid.mp4"
        assert hook_calls[0]["dialog_name"] == "test_dialog"
