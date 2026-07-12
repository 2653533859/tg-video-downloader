"""Download queue worker."""

import os
import time


class DownloadWorker:
    def __init__(
        self,
        *,
        download_dir_for_dialog,
        release_tasks,
        process_queue,
        copy_task_state,
        set_task_state,
        update_task_state,
        is_cancelled,
        get_cached_message,
        resolve_message,
        get_video_info,
        supports_tdl_download,
        download_with_telegram,
        tdl_executor,
        save_resume_info,
        format_size,
        log_info,
        log_error,
    ):
        self.download_dir_for_dialog = download_dir_for_dialog
        self.release_tasks = release_tasks
        self.process_queue = process_queue
        self.copy_task_state = copy_task_state
        self.set_task_state = set_task_state
        self.update_task_state = update_task_state
        self.is_cancelled = is_cancelled
        self.get_cached_message = get_cached_message
        self.resolve_message = resolve_message
        self.get_video_info = get_video_info
        self.supports_tdl_download = supports_tdl_download
        self.download_with_telegram = download_with_telegram
        self.tdl_executor = tdl_executor
        self.save_resume_info = save_resume_info
        self.format_size = format_size
        self.log_info = log_info
        self.log_error = log_error

    def run(self, task_items, dialog_name):
        try:
            save_dir = self.download_dir_for_dialog(dialog_name)
            os.makedirs(save_dir, exist_ok=True)
        except Exception as exc:
            self.log_error(f"_do_download: 创建目录失败: {exc}")
            self.release_tasks(task_items)
            self.process_queue()
            return

        for task in task_items:
            self._run_one(task, dialog_name, save_dir)

        self.release_tasks(task_items)
        self.process_queue()

    def _run_one(self, task, dialog_name, save_dir):
        task_id = task.get("task_id")
        entity_id = task.get("entity_id")
        msg_id = task.get("msg_id")
        if not task_id or entity_id is None or msg_id is None:
            return

        if self.is_cancelled(task_id):
            state = self.copy_task_state(task_id) or {}
            state.update({
                "status": "cancelled",
                "error": "已取消",
                "speed": "",
                "speed_bps": 0.0,
                "queue_position": None,
                "queue_size": 0,
            })
            self.set_task_state(task_id, state)
            return

        info = self._resolve_info(task, task_id, entity_id, msg_id)
        if not info:
            self.update_task_state(
                task_id,
                status="error",
                error="消息不包含可下载视频",
                finish_time=time.time(),
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
            )
            return

        filepath = os.path.join(save_dir, info["filename"])
        if self._skip_existing(task_id, entity_id, msg_id, dialog_name, info, filepath):
            return

        downloader = task.get("downloader")
        if downloader == "tdl" and not self.supports_tdl_download(entity_id):
            self.log_info(f"[{task_id}] tdl 已对该频道禁用，改用 Telegram 直连")
            downloader = "telegram"
        elif downloader not in ("tdl", "telegram"):
            downloader = "tdl" if self.supports_tdl_download(entity_id) else "telegram"
        self.update_task_state(task_id, downloader=downloader)
        if downloader == "telegram":
            self._run_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath)
            return

        self.tdl_executor().download(
            task_id,
            entity_id,
            msg_id,
            dialog_name,
            info,
            filepath,
            save_dir,
        )

    def _resolve_info(self, task, task_id, entity_id, msg_id):
        info = task.get("info")
        if info:
            return info

        message = self.get_cached_message(msg_id, entity_id)
        if not message:
            try:
                self.log_info(f"[{task_id}] 缓存未命中，重新获取消息 entity={entity_id} msg={msg_id}")
                message = self.resolve_message(entity_id, msg_id)
            except Exception as exc:
                self.log_error(f"[{task_id}] 重新获取消息失败: {exc}")
                message = None
        return self.get_video_info(message) if message else None

    def _skip_existing(self, task_id, entity_id, msg_id, dialog_name, info, filepath):
        if not (os.path.exists(filepath) and os.path.getsize(filepath) == info["size"]):
            return False
        self.log_info(f"跳过(已存在) [{task_id}] {info['filename']}")
        final_size = info.get("size") or os.path.getsize(filepath)
        self.set_task_state(task_id, {
            "filename": info["filename"],
            "progress": 100,
            "status": "skipped",
            "finish_time": time.time(),
            "downloaded": self.format_size(final_size),
            "total": self.format_size(final_size),
            "error": "",
            "speed": "",
            "msg_id": msg_id,
            "entity_id": entity_id,
            "dialog_name": dialog_name,
            "downloaded_bytes": final_size,
            "total_bytes": final_size,
            "expected_bytes": info.get("size") or final_size,
            "final_bytes": final_size,
            "document_id": str(info.get("document_id") or ""),
            "integrity": "ok",
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
        })
        return True

    def _run_telegram(self, task_id, entity_id, msg_id, dialog_name, info, filepath):
        total_bytes = info.get("size") or 0
        try:
            self.download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath)
        except Exception as exc:
            err = str(exc)
            self.log_error(f"下载失败 [{task_id}] {info.get('filename','?')}: {err}")
            cur_file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            if self.is_cancelled(task_id) or "取消" in err:
                self.update_task_state(task_id, status="cancelled", error="已取消", finish_time=time.time())
                if cur_file_size > 0:
                    self._save_resume(task_id, filepath, info, cur_file_size, total_bytes, entity_id, msg_id, dialog_name)
            else:
                self.update_task_state(task_id, status="error", error=err, finish_time=time.time())
                if cur_file_size > 0:
                    self._save_resume(task_id, filepath, info, cur_file_size, total_bytes, entity_id, msg_id, dialog_name)
            self.update_task_state(task_id, speed="", speed_bps=0.0, queue_position=None, queue_size=0)

    def _save_resume(self, task_id, filepath, info, offset, total_bytes, entity_id, msg_id, dialog_name):
        self.save_resume_info(task_id, {
            "filepath": filepath,
            "filename": info["filename"],
            "offset": offset,
            "total": total_bytes,
            "entity_id": entity_id,
            "msg_id": msg_id,
            "dialog_name": dialog_name,
        })

