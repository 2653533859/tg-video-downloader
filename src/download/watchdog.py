"""
下载监控看门狗
"""
import time
import threading
import logging
from typing import Dict, Any, Callable, Optional

logger = logging.getLogger("tg_downloader.watchdog")


class DownloadWatchdog:
    """
    下载监控看门狗
    自动检测并重启停滞的下载任务
    """

    def __init__(
        self,
        check_interval: int = 60,
        stall_timeout: int = 300,
        get_tasks_callback: Optional[Callable] = None,
        restart_task_callback: Optional[Callable] = None,
        log_info: Optional[Callable[[str], None]] = None,
        log_warning: Optional[Callable[[str], None]] = None,
        log_error: Optional[Callable[[str], None]] = None,
    ):
        """
        初始化看门狗

        Args:
            check_interval: 检查间隔（秒）
            stall_timeout: 停滞超时时间（秒）
            get_tasks_callback: 获取任务列表的回调函数
            restart_task_callback: 重启任务的回调函数
        """
        self.check_interval = check_interval
        self.stall_timeout = stall_timeout
        self.get_tasks_callback = get_tasks_callback
        self.restart_task_callback = restart_task_callback
        self._log_info = log_info or logger.info
        self._log_warning = log_warning or logger.warning
        self._log_error = log_error or logger.error

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_check_progress: Dict[str, Dict[str, Any]] = {}
        self.last_progress = self._last_check_progress

    def start(self):
        """启动看门狗"""
        if self._running:
            logger.warning("看门狗已在运行")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        self._log_info(
            f"[watchdog] 下载监控已启动 "
            f"(检查间隔:{self.check_interval}s, 超时阈值:{self.stall_timeout}s)"
        )

    def stop(self):
        """停止看门狗（可中断等待，立即生效）"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._log_info("[watchdog] 下载监控已停止")

    def _monitor_loop(self):
        """监控循环"""
        while self._running:
            # 可中断等待：stop() 触发后立即退出，不必等满一个 check_interval
            if self._stop_event.wait(self.check_interval):
                break
            try:
                self._check_all_tasks()
            except Exception as e:
                self._log_error(f"[watchdog] 监控异常: {e}")

    def _check_all_tasks(self):
        """检查所有下载任务"""
        if not self.get_tasks_callback:
            return

        try:
            tasks = self.get_tasks_callback()
        except Exception as e:
            self._log_error(f"[watchdog] 获取任务列表失败: {e}")
            return

        current_time = time.time()
        items = tasks.items() if hasattr(tasks, "items") else tasks

        for task_id, task in list(items):
            status = task.get("status", "")
            if status != "downloading":
                self._last_check_progress.pop(task_id, None)
                continue

            self._check_task(task_id, task, current_time)

    def _check_task(self, task_id: str, task: Dict[str, Any], current_time: float):
        """
        检查单个任务是否停滞

        Args:
            task_id: 任务 ID
            task: 任务状态字典
            current_time: 当前时间戳
        """
        current_bytes = task.get("downloaded_bytes", 0)
        last_record = self._last_check_progress.get(task_id)

        if last_record is None:
            self._last_check_progress[task_id] = {
                "bytes": current_bytes,
                "time": current_time,
            }
            return

        elapsed = current_time - last_record.get("time", current_time)
        bytes_diff = current_bytes - last_record.get("bytes", 0)

        if bytes_diff == 0 and elapsed > self.stall_timeout:
            self._log_warning(
                f"[watchdog] 任务 {task_id} 已停滞 {elapsed:.0f}s "
                f"(进度: {task.get('progress', 0)}%, "
                f"已下载: {task.get('downloaded', '0B')}), "
                f"触发自动重启"
            )
            self._restart_stuck_task(task_id, task)
            self._last_check_progress.pop(task_id, None)
        elif bytes_diff > 0:
            self._last_check_progress[task_id] = {
                "bytes": current_bytes,
                "time": current_time,
            }

    def restart_task(self, task_id: str, task: Dict[str, Any]):
        if not self.restart_task_callback:
            self._log_error(f"[watchdog] 任务 {task_id} 无法重启：未设置 restart_task_callback")
            return
        return self.restart_task_callback(task_id, task)

    def _restart_stuck_task(self, task_id: str, task: Dict[str, Any]):
        """
        重启停滞的任务

        Args:
            task_id: 任务 ID
            task: 任务状态字典
        """
        try:
            entity_id = task.get("entity_id")
            msg_id = task.get("msg_id")

            if not entity_id or not msg_id:
                self._log_error(f"[watchdog] 任务 {task_id} 缺少必要信息，无法重启")
                return

            self._log_info(f"[watchdog] 正在重启任务 {task_id}...")
            result = self.restart_task(task_id, task)

            if isinstance(result, dict):
                if result.get("ok"):
                    self._log_info(f"[watchdog] 任务 {task_id} 重启成功")
                else:
                    self._log_error(
                        f"[watchdog] 任务 {task_id} 重启失败: "
                        f"{result.get('error', 'unknown')}"
                    )

        except Exception as e:
            self._log_error(f"[watchdog] 重启任务 {task_id} 时异常: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """
        获取看门狗统计信息

        Returns:
            统计信息字典
        """
        return {
            "running": self._running,
            "check_interval": self.check_interval,
            "stall_timeout": self.stall_timeout,
            "monitored_tasks": len(self._last_check_progress)
        }
