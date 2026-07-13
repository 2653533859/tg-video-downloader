"""tdl subprocess download executor."""

import contextlib
import os
import subprocess
import threading
import time


class TdlDownloadExecutor:
    def __init__(
        self,
        *,
        build_message_url,
        build_command,
        clear_tdl_error,
        register_process,
        drop_process,
        get_process,
        set_tdl_error,
        last_tdl_error,
        stop_process,
        detect_resume_offset,
        resolve_progress_path,
        prepare_telegram_fallback_target,
        save_resume_info,
        clear_resume_info,
        update_task_state,
        set_task_state,
        copy_task_state,
        is_cancelled,
        should_capture_error_line,
        choose_more_specific_error,
        reconcile_progress_size,
        did_restart_from_scratch,
        should_retry_error,
        should_fallback,
        remember_fallback_channel,
        validate_completion,
        download_with_telegram,
        format_size,
        log_info,
        log_warning,
        log_error,
        restart_reset_min_bytes,
        stall_timeout=600,
        resource_lock=None,
    ):
        self.build_message_url = build_message_url
        self.build_command = build_command
        self.clear_tdl_error = clear_tdl_error
        self.register_process = register_process
        self.drop_process = drop_process
        self.get_process = get_process
        self.set_tdl_error = set_tdl_error
        self.last_tdl_error = last_tdl_error
        self.stop_process = stop_process
        self.detect_resume_offset = detect_resume_offset
        self.resolve_progress_path = resolve_progress_path
        self.prepare_telegram_fallback_target = prepare_telegram_fallback_target
        self.save_resume_info = save_resume_info
        self.clear_resume_info = clear_resume_info
        self.update_task_state = update_task_state
        self.set_task_state = set_task_state
        self.copy_task_state = copy_task_state
        self.is_cancelled = is_cancelled
        self.should_capture_error_line = should_capture_error_line
        self.choose_more_specific_error = choose_more_specific_error
        self.reconcile_progress_size = reconcile_progress_size
        self.did_restart_from_scratch = did_restart_from_scratch
        self.should_retry_error = should_retry_error
        self.should_fallback = should_fallback
        self.remember_fallback_channel = remember_fallback_channel
        self.validate_completion = validate_completion
        self.download_with_telegram = download_with_telegram
        self.format_size = format_size
        self.log_info = log_info
        self.log_warning = log_warning
        self.log_error = log_error
        self.restart_reset_min_bytes = restart_reset_min_bytes
        self.stall_timeout = stall_timeout
        # tdl 单实例 Bolt DB 约束：显式资源锁，串行化 tdl 子进程调用。
        # None 时用 nullcontext（不加锁），保持既有行为与可测性。
        self.resource_lock = resource_lock

    def download(self, task_id, entity_id, msg_id, dialog_name, info, filepath, save_dir):
        message_url = self._build_message_url_or_error(task_id, entity_id, msg_id)
        if not message_url:
            return

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
        })

        retry_count = 0
        last_retry_size = start_offset
        try:
            while True:
                try:
                    # tdl 子进程访问单实例 Bolt DB，用资源锁串行化
                    with (self.resource_lock or contextlib.nullcontext()):
                        start_offset, last_retry_size = self._run_once(
                            task_id,
                            entity_id,
                            msg_id,
                            dialog_name,
                            info,
                            filepath,
                            save_dir,
                            message_url,
                            total_bytes,
                            start_offset,
                            retry_count,
                            last_retry_size,
                        )
                    break
                except Exception as exc:
                    err = str(exc)
                    cur_progress_path = self.resolve_progress_path(filepath)
                    cur_file_size = os.path.getsize(cur_progress_path) if os.path.exists(cur_progress_path) else 0
                    if self.did_restart_from_scratch(
                        retry_count=retry_count,
                        previous_size=last_retry_size,
                        current_size=cur_file_size,
                        start_offset=start_offset,
                    ):
                        self.log_warning(
                            f"[{task_id}] 断点失效，tdl 从头开始下载"
                            f"（期望续传 {self.format_size(last_retry_size or start_offset)}，"
                            f"实际 {self.format_size(cur_file_size)}）"
                        )
                        start_offset = 0
                        last_retry_size = cur_file_size
                        retry_count += 1
                        time.sleep(min(2 * retry_count, 10))
                        continue
                    if self.should_retry_error(
                        err,
                        retry_count,
                        current_size=cur_file_size,
                        last_retry_size=last_retry_size,
                    ) and not self.is_cancelled(task_id):
                        retry_count += 1
                        last_retry_size = cur_file_size
                        self._save_resume(task_id, filepath, info, cur_file_size, total_bytes, entity_id, msg_id, dialog_name)
                        self.update_task_state(
                            task_id,
                            status="downloading",
                            error=f"连接中断，正在续传（第 {retry_count} 次，已保留 {self.format_size(cur_file_size)}）",
                            speed="",
                            speed_bps=0.0,
                        )
                        self.log_warning(f"[{task_id}] tdl 下载中断，准备自动续传: {err}")
                        time.sleep(min(2 * retry_count, 10))
                        continue
                    raise
        except Exception as exc:
            self._handle_failure(exc, task_id, entity_id, msg_id, dialog_name, info, filepath, total_bytes)
        finally:
            proc = self.get_process(task_id)
            self.stop_process(proc)
            self.drop_process(task_id)

    def _build_message_url_or_error(self, task_id, entity_id, msg_id):
        try:
            return self.build_message_url(entity_id, msg_id)
        except Exception as exc:
            self.update_task_state(
                task_id,
                status="error",
                error=str(exc),
                finish_time=time.time(),
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
            )
            return ""

    def _run_once(
        self,
        task_id,
        entity_id,
        msg_id,
        dialog_name,
        info,
        filepath,
        save_dir,
        message_url,
        total_bytes,
        start_offset,
        retry_count,
        last_retry_size,
    ):
        command = self.build_command(message_url, save_dir, info["filename"])
        self.clear_tdl_error(task_id)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.register_process(task_id, process)

        output_thread = threading.Thread(
            target=self._drain_output,
            args=(task_id, process),
            daemon=True,
        )
        output_thread.start()

        if retry_count > 0:
            progress_path = self.resolve_progress_path(filepath)
            resumed_size = os.path.getsize(progress_path) if os.path.exists(progress_path) else start_offset
            written = resumed_size
            last_bytes = resumed_size
        else:
            written = start_offset
            last_bytes = start_offset

        allow_offset_correction = start_offset > 0
        last_time = time.time()
        last_save_time = time.time()
        restart_logged = False
        last_progress_written = written
        last_progress_time = time.time()

        while True:
            if self.is_cancelled(task_id):
                self.stop_process(process)
                raise Exception("下载已取消")

            progress_path = self.resolve_progress_path(filepath)
            current_size = os.path.getsize(progress_path) if os.path.exists(progress_path) else written
            current_size, allow_offset_correction = self.reconcile_progress_size(
                current_size=current_size,
                written=written,
                allow_offset_correction=allow_offset_correction,
            )
            written = current_size

            if written > last_progress_written:
                last_progress_written = written
                last_progress_time = time.time()
            elif time.time() - last_progress_time > self.stall_timeout:
                self.log_warning(f"[{task_id}] tdl 下载停滞超过 {self.stall_timeout}s，终止进程")
                self.stop_process(process)
                raise RuntimeError("下载停滞，连接可能已断开")

            if (
                not restart_logged
                and start_offset > self.restart_reset_min_bytes
                and time.time() - last_save_time > 10
                and written < int(start_offset * 0.5)
            ):
                restart_logged = True
                self.log_warning(
                    f"[{task_id}] tdl 未续传（期望 {self.format_size(start_offset)}，"
                    f"当前 {self.format_size(written)}），将从头下载"
                )
                start_offset = 0
                last_retry_size = 0

            now = time.time()
            elapsed = now - last_time
            speed_bps = 0.0
            speed_label = ""
            if elapsed >= 0.5:
                delta = written - last_bytes
                speed_bps = delta / elapsed if elapsed > 0 else 0.0
                speed_label = self.format_size(speed_bps) + "/s" if speed_bps > 0 else ""
                last_bytes = written
                last_time = now

            pct = int(written / total_bytes * 100) if total_bytes else 0
            state = self.copy_task_state(task_id)
            if state and state.get("status") not in {"done", "skipped", "error", "cancelled"}:
                updates = {
                    "progress": min(pct, 99) if total_bytes and written < total_bytes else pct,
                    "status": "downloading",
                    "downloaded": self.format_size(written),
                    "downloaded_bytes": written,
                    "error": "",
                }
                if speed_label:
                    updates["speed"] = speed_label
                    updates["speed_bps"] = speed_bps
                self.update_task_state(task_id, **updates)

            if now - last_save_time >= 10:
                self._save_resume(task_id, filepath, info, written, total_bytes, entity_id, msg_id, dialog_name)
                last_save_time = now

            if process.poll() is not None:
                break
            time.sleep(0.5)

        final_size = self._final_size_after_success(task_id, process, output_thread, filepath, written, total_bytes)
        self.update_task_state(
            task_id,
            progress=100,
            status="done",
            finish_time=time.time(),
            downloaded=self.format_size(final_size),
            total=self.format_size(final_size),
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
        return start_offset, last_retry_size

    def _drain_output(self, task_id, process):
        try:
            if not process.stdout:
                return
            for line in process.stdout:
                line = line.strip()
                if line:
                    self.log_info(f"[tdl:{task_id}] {line}")
                    if self.should_capture_error_line(line):
                        current_error = self.last_tdl_error(task_id)
                        self.set_tdl_error(task_id, self.choose_more_specific_error(current_error, line))
        except Exception:
            pass

    def _final_size_after_success(self, task_id, process, output_thread, filepath, written, total_bytes):
        final_progress_path = self.resolve_progress_path(filepath)
        final_size = os.path.getsize(final_progress_path) if os.path.exists(final_progress_path) else written
        output_thread.join(timeout=0.5)
        if process.returncode != 0:
            last_error = self.last_tdl_error(task_id)
            raise RuntimeError(last_error or f"tdl 退出码 {process.returncode}")

        tmp_path = filepath + ".tmp"
        if os.path.exists(filepath):
            final_size = os.path.getsize(filepath)
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                    self.log_info(f"[{task_id}] 清理残留 .tmp 文件")
                except Exception:
                    pass
        elif os.path.exists(tmp_path):
            final_size = os.path.getsize(tmp_path)

        completion_error = self.validate_completion(total_bytes=total_bytes, final_size=final_size)
        if completion_error:
            raise RuntimeError(completion_error)
        if total_bytes and final_size <= 0:
            raise RuntimeError("tdl 未产生有效下载数据")
        return final_size

    def _handle_failure(self, exc, task_id, entity_id, msg_id, dialog_name, info, filepath, total_bytes):
        err = str(exc)
        self.log_error(f"下载失败 [{task_id}] {info.get('filename','?')}: {err}")
        cur_progress_path = self.resolve_progress_path(filepath)
        cur_file_size = os.path.getsize(cur_progress_path) if os.path.exists(cur_progress_path) else 0

        if self.should_fallback(err) and not self.is_cancelled(task_id):
            self.remember_fallback_channel(entity_id, err)
            self.log_warning(f"[{task_id}] tdl 无法解析消息链接，切换 Telegram 直连: {err}")
            self.update_task_state(
                task_id,
                status="downloading",
                error="tdl 解析失败，切换 Telegram 直连",
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
                downloader="telegram",
            )
            try:
                self.clear_tdl_error(task_id)
                filepath = self.prepare_telegram_fallback_target(filepath)
                self.download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath)
                return
            except Exception as fallback_exc:
                err = str(fallback_exc)
                self.log_error(f"[{task_id}] Telegram 直连回退失败: {err}")
                cur_file_size = os.path.getsize(filepath) if os.path.exists(filepath) else cur_file_size

        if self.is_cancelled(task_id) or "取消" in err:
            self.update_task_state(task_id, status="cancelled", error="已取消", finish_time=time.time())
            if cur_file_size > 0:
                self._save_resume(task_id, filepath, info, cur_file_size, total_bytes, entity_id, msg_id, dialog_name)
                self.log_info(f"[{task_id}] 已取消，保留部分文件 {self.format_size(cur_file_size)}")
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
