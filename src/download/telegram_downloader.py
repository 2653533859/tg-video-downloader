"""Telegram direct download executor."""

import os
import time


class TelegramDirectDownloader:
    def __init__(
        self,
        *,
        tg_client,
        ensure_connection,
        run_async,
        resolve_message,
        next_chunk,
        detect_resume_offset,
        save_resume_info,
        clear_resume_info,
        set_task_state,
        update_task_state,
        is_cancelled,
        should_retry_error,
        validate_completion,
        calc_timeout,
        format_size,
        log_info,
        log_warning,
        max_retry_attempts,
        chunk_timeout,
    ):
        self.tg_client = tg_client
        self.ensure_connection = ensure_connection
        self.run_async = run_async
        self.resolve_message = resolve_message
        self.next_chunk = next_chunk
        self.detect_resume_offset = detect_resume_offset
        self.save_resume_info = save_resume_info
        self.clear_resume_info = clear_resume_info
        self.set_task_state = set_task_state
        self.update_task_state = update_task_state
        self.is_cancelled = is_cancelled
        self.should_retry_error = should_retry_error
        self.validate_completion = validate_completion
        self.calc_timeout = calc_timeout
        self.format_size = format_size
        self.log_info = log_info
        self.log_warning = log_warning
        self.max_retry_attempts = max_retry_attempts
        self.chunk_timeout = chunk_timeout

    def download(self, task_id, entity_id, msg_id, dialog_name, info, filepath):
        self.ensure_connection(allow_reconnect=True)
        total_bytes = info.get("size") or 0
        existing_size = self.detect_resume_offset(task_id, filepath, total_bytes)
        if existing_size > 0 and total_bytes > 0 and existing_size < total_bytes:
            self.log_info(
                f"[{task_id}] 发现部分文件 {info['filename']}: "
                f"{self.format_size(existing_size)}/{self.format_size(total_bytes)}，将续传"
            )

        start_offset = existing_size if (existing_size > 0 and total_bytes > 0 and existing_size < total_bytes) else 0
        init_pct = int(start_offset / total_bytes * 100) if total_bytes and start_offset else 0
        self.set_task_state(task_id, {
            "filename": info["filename"],
            "progress": init_pct,
            "status": "downloading",
            "downloaded": self.format_size(start_offset) if start_offset else "0B",
            "total": self.format_size(total_bytes) if total_bytes else "",
            "error": f"续传 {self.format_size(start_offset)}" if start_offset else "",
            "speed": "",
            "msg_id": msg_id,
            "entity_id": entity_id,
            "dialog_name": dialog_name,
            "downloaded_bytes": start_offset,
            "total_bytes": total_bytes,
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
            "downloader": "telegram",
        })

        retry_count = 0
        while True:
            message = self.resolve_message(entity_id, msg_id, force_refresh=True)
            if not message or not getattr(getattr(message, "media", None), "document", None):
                raise RuntimeError("消息不包含可下载视频")

            start_offset = self.detect_resume_offset(task_id, filepath, total_bytes)

            async def _runner():
                written = start_offset
                last_bytes = start_offset
                last_time = time.time()
                last_save_time = time.time()
                mode = "ab" if start_offset else "wb"
                with open(filepath, mode) as output:
                    iterator = self.tg_client.iter_download(
                        message.media.document,
                        offset=start_offset,
                        file_size=total_bytes or None,
                        request_size=512 * 1024,
                    )
                    while True:
                        try:
                            chunk = await self.next_chunk(iterator, timeout=self.chunk_timeout)
                        except StopAsyncIteration:
                            break
                        if self.is_cancelled(task_id):
                            raise RuntimeError("下载已取消")
                        if not chunk:
                            continue
                        output.write(chunk)
                        output.flush()
                        written += len(chunk)
                        now = time.time()
                        speed_bps = 0.0
                        speed_label = ""
                        elapsed = now - last_time
                        if elapsed >= 0.5:
                            delta = written - last_bytes
                            speed_bps = delta / elapsed if elapsed > 0 else 0.0
                            speed_label = self.format_size(speed_bps) + "/s" if speed_bps > 0 else ""
                            last_bytes = written
                            last_time = now
                        pct = int(written / total_bytes * 100) if total_bytes else 0
                        self.update_task_state(
                            task_id,
                            progress=min(pct, 99) if total_bytes and written < total_bytes else pct,
                            status="downloading",
                            downloaded=self.format_size(written),
                            downloaded_bytes=written,
                            error="",
                            speed=speed_label,
                            speed_bps=speed_bps,
                        )
                        if now - last_save_time >= 10:
                            self.save_resume_info(task_id, {
                                "filepath": filepath,
                                "filename": info["filename"],
                                "offset": written,
                                "total": total_bytes,
                                "entity_id": entity_id,
                                "msg_id": msg_id,
                                "dialog_name": dialog_name,
                            })
                            last_save_time = now
                return written

            timeout = self.calc_timeout(max(total_bytes - start_offset, 0) or total_bytes)
            try:
                final_size = self.run_async(lambda: _runner(), timeout=timeout, allow_reconnect=True)
                break
            except Exception as exc:
                if self.is_cancelled(task_id) or "取消" in str(exc):
                    raise
                if retry_count >= self.max_retry_attempts or not self.should_retry_error(exc):
                    raise
                retry_count += 1
                current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                self.save_resume_info(task_id, {
                    "filepath": filepath,
                    "filename": info["filename"],
                    "offset": current_size,
                    "total": total_bytes,
                    "entity_id": entity_id,
                    "msg_id": msg_id,
                    "dialog_name": dialog_name,
                })
                self.update_task_state(
                    task_id,
                    status="downloading",
                    downloaded=self.format_size(current_size),
                    downloaded_bytes=current_size,
                    error=f"连接中断，刷新媒体引用后自动续传（第 {retry_count} 次）",
                    speed="",
                    speed_bps=0.0,
                )
                self.log_warning(
                    f"[{task_id}] Telegram 下载中断，刷新媒体引用后自动续传"
                    f"（第 {retry_count} 次，已保留 {self.format_size(current_size)}）: {exc}"
                )
                time.sleep(min(2 * retry_count, 20))

        completion_error = self.validate_completion(total_bytes=total_bytes, final_size=final_size)
        if completion_error:
            raise RuntimeError(completion_error)
        if total_bytes and final_size <= 0:
            raise RuntimeError("Telegram 未产生有效下载数据")

        self.update_task_state(
            task_id,
            progress=100,
            status="done",
            finish_time=time.time(),
            downloaded=self.format_size(final_size),
            total=self.format_size(final_size),
            error="",
            speed="",
            downloaded_bytes=final_size,
            total_bytes=final_size,
            expected_bytes=total_bytes,
            final_bytes=final_size,
            document_id=str(info.get("document_id") or ""),
            integrity="ok",
            speed_bps=0.0,
            queue_position=None,
            queue_size=0,
        )
        self.clear_resume_info(task_id)
        self.log_info(f"下载完成 [{task_id}] {info['filename']} ({self.format_size(final_size)})")
