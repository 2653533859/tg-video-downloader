"""Rclone executor implementation for cloud uploads."""

import json
import logging
import os
import subprocess
import threading
import time
from typing import Callable, Optional, Dict, Any, List

from src.uploader.base import UploaderBase
from src.uploader.config import UploaderConfig
from src.utils.formatting import format_size

logger = logging.getLogger("tg_downloader.uploader.rclone")


def format_speed(speed_bps: float) -> str:
    """Format bytes per second into human-readable speed string."""
    if not speed_bps or speed_bps <= 0:
        return "0 B/s"
    return f"{format_size(speed_bps)}/s"


def parse_rclone_json_line(line: str) -> Optional[Dict[str, Any]]:
    """
    Parse a single JSON log line from rclone with --use-json-log.
    Returns normalized progress dictionary or None.
    """
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return None

    stats = data.get("stats")
    if not stats or not isinstance(stats, dict):
        return None

    percentage = stats.get("percentage")
    if percentage is None:
        total = stats.get("totalBytes", 0) or 0
        transferred = stats.get("bytes", 0) or 0
        percentage = round((transferred / total) * 100, 1) if total > 0 else 0

    speed = stats.get("speed", 0.0) or 0.0
    bytes_transferred = stats.get("bytes", 0) or 0
    total_bytes = stats.get("totalBytes", 0) or 0
    eta = stats.get("eta")

    return {
        "percentage": float(percentage),
        "bytes": int(bytes_transferred),
        "total_bytes": int(total_bytes),
        "speed_bps": float(speed),
        "speed": format_speed(speed),
        "eta": int(eta) if eta is not None else None,
        "raw_msg": data.get("msg", ""),
    }


class RcloneUploader(UploaderBase):
    """Executes file uploads using rclone subprocess with real-time JSON log streaming."""

    def __init__(self, config: Optional[UploaderConfig] = None):
        self.config = config or UploaderConfig.from_env()
        self._active_processes: Dict[str, subprocess.Popen] = {}
        self._lock = threading.RLock()

    def build_command(
        self,
        local_path: str,
        remote_dest: str,
        delete_on_success: bool = False,
    ) -> List[str]:
        """Build the rclone command array."""
        binary = self.config.resolve_binary() or self.config.rclone_binary
        action = "move" if delete_on_success else "copy"
        
        cmd = [
            binary,
            action,
            local_path,
            remote_dest,
            "--use-json-log",
            "--stats", "1s",
            "-v",
            "--timeout", "30s",
            "--contimeout", "15s",
            "--low-speed-limit", "100k",
            "--low-speed-time", "30s",
            "--retries", "10",
            "--retries-sleep", "2s",
            "--drive-chunk-size", "32M",
        ]
        config_path = self.config.resolve_config_path()
        if config_path:
            cmd.extend(["--config", config_path])
        if self.config.bwlimit:
            cmd.extend(["--bwlimit", self.config.bwlimit])
            
        return cmd

    def upload(
        self,
        local_path: str,
        remote_dir: str,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        is_cancelled: Optional[Callable[[], bool]] = None,
        delete_on_success: bool = False,
        task_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Upload local file to Google Drive / rclone remote.
        """
        if not os.path.exists(local_path):
            return {"success": False, "error": f"本地文件不存在: {local_path}"}

        binary_path = self.config.resolve_binary()
        if not binary_path:
            return {
                "success": False,
                "error": f"找不到 rclone 可执行文件: '{self.config.rclone_binary}'。请在宿主或容器安装 rclone",
            }

        remote_target = self.config.format_remote_path(remote_dir)
        cmd = self.build_command(local_path, remote_target, delete_on_success=delete_on_success)
        key = task_key or local_path

        logger.info(f"开始 rclone 上传: {' '.join(cmd)}")
        error_lines = []
        last_progress: Dict[str, Any] = {
            "percentage": 0.0,
            "speed": "0 B/s",
            "bytes": 0,
            "total_bytes": os.path.getsize(local_path) if os.path.isfile(local_path) else 0,
        }

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            return {"success": False, "error": f"启动 rclone 失败: {exc}"}

        with self._lock:
            self._active_processes[key] = process

        def read_stderr():
            try:
                for line in process.stderr:
                    line = line.strip()
                    if not line:
                        continue
                    parsed = parse_rclone_json_line(line)
                    if parsed:
                        last_progress.update(parsed)
                        if progress_callback:
                            try:
                                progress_callback(dict(last_progress))
                            except Exception:
                                pass
                    else:
                        # Collect possible error line
                        try:
                            item = json.loads(line)
                            if item.get("level") in ("error", "fatal"):
                                error_lines.append(item.get("msg", line))
                        except Exception:
                            error_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        stall_timeout = 90.0
        last_byte_change_time = time.time()
        last_bytes = 0

        try:
            while process.poll() is None:
                if is_cancelled and is_cancelled():
                    self._terminate_process(process)
                    return {"success": False, "error": "上传已取消", "cancelled": True}

                cur_bytes = last_progress.get("bytes", 0)
                now = time.time()
                if cur_bytes > last_bytes:
                    last_bytes = cur_bytes
                    last_byte_change_time = now
                elif now - last_byte_change_time > stall_timeout:
                    logger.warning(f"上传停滞超时 ({stall_timeout}s 无数据增长)，强制终止重试: {local_path}")
                    self._terminate_process(process)
                    return {"success": False, "error": f"上传连接停滞 ({int(stall_timeout)}秒无数据)，已自动切断"}

                time.sleep(0.5)
            stderr_thread.join(timeout=3)
            returncode = process.returncode

            if is_cancelled and is_cancelled():
                return {"success": False, "error": "上传已取消", "cancelled": True}

            if returncode == 0:
                logger.info(f"rclone 上传成功: {local_path} -> {remote_target}")
                return {
                    "success": True,
                    "error": "",
                    "remote_target": remote_target,
                    "deleted_local": delete_on_success,
                }
            else:
                err_msg = "; ".join(error_lines[-3:]) if error_lines else f"退出码 {returncode}"
                logger.error(f"rclone 上传失败 (code {returncode}): {err_msg}")
                return {"success": False, "error": err_msg, "returncode": returncode}

        finally:
            with self._lock:
                self._active_processes.pop(key, None)
            if process.stderr:
                try:
                    process.stderr.close()
                except Exception:
                    pass
    def cancel(self, task_key: str):
        """Cancel an active upload process by task key."""
        with self._lock:
            process = self._active_processes.get(task_key)
            if process:
                self._terminate_process(process)

    def _terminate_process(self, process: subprocess.Popen):
        try:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        except OSError:
            pass

    def test_connection(self) -> Dict[str, Any]:
        """Test rclone availability and connection to the configured remote."""
        binary = self.config.resolve_binary()
        if not binary:
            return {
                "ok": False,
                "message": f"未安装 rclone 二进制文件 (查找路径: {self.config.rclone_binary})",
                "details": {"installed": False},
            }

        # Check rclone version
        try:
            ver_res = subprocess.run([binary, "version"], capture_output=True, text=True, timeout=5)
            version_line = ver_res.stdout.splitlines()[0] if ver_res.stdout else "unknown"
        except Exception as exc:
            return {
                "ok": False,
                "message": f"执行 rclone version 失败: {exc}",
                "details": {"installed": True, "error": str(exc)},
            }

        # Check remote connection
        remote = self.config.remote_name
        cmd = [binary, "about", f"{remote}:"]
        config_path = self.config.resolve_config_path()
        if config_path:
            cmd.extend(["--config", config_path])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                return {
                    "ok": True,
                    "message": f"成功连接至 Google Drive remote '{remote}:'",
                    "details": {
                        "installed": True,
                        "version": version_line,
                        "remote": remote,
                        "about": res.stdout.strip(),
                    },
                }
            else:
                cmd_fallback = [binary, "lsd", f"{remote}:", "--max-depth", "1"]
                if config_path:
                    cmd_fallback.extend(["--config", config_path])
                res2 = subprocess.run(cmd_fallback, capture_output=True, text=True, timeout=10)
                if res2.returncode == 0:
                    return {
                        "ok": True,
                        "message": f"成功连接至 Google Drive remote '{remote}:'",
                        "details": {"installed": True, "version": version_line, "remote": remote},
                    }
                err_text = res.stderr.strip() or res2.stderr.strip() or f"退出码 {res.returncode}"
                return {
                    "ok": False,
                    "message": f"无法连接到 remote '{remote}:': {err_text}",
                    "details": {"installed": True, "version": version_line, "remote": remote, "error": err_text},
                }
        except Exception as exc:
            return {
                "ok": False,
                "message": f"连接测试超时或异常: {exc}",
                "details": {"installed": True, "version": version_line, "error": str(exc)},
            }
