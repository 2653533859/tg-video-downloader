"""
Telegram 健康检查器
定期检查 Telegram 连接状态并自动重连
"""
import threading
import logging
import asyncio
from typing import Optional, Callable

logger = logging.getLogger("tg_downloader.health_checker")


class TelegramHealthChecker:
    """
    Telegram 连接健康检查器
    定期检查连接状态，失败时自动重连
    """

    def __init__(
        self,
        client,
        loop,
        check_interval: int = 120,
        max_retry: int = 3,
        on_reconnect_callback: Optional[Callable] = None,
        log_info: Optional[Callable[[str], None]] = None,
        log_warning: Optional[Callable[[str], None]] = None,
        log_error: Optional[Callable[[str], None]] = None,
    ):
        """
        初始化健康检查器

        Args:
            client: Telegram 客户端实例
            loop: asyncio 事件循环
            check_interval: 检查间隔（秒）
            max_retry: 最大重试次数
            on_reconnect_callback: 重连成功后的回调函数
        """
        self.client = client
        self.loop = loop
        self.check_interval = check_interval
        self.max_retry = max_retry
        self.on_reconnect_callback = on_reconnect_callback
        self._log_info = log_info or logger.info
        self._log_warning = log_warning or logger.warning
        self._log_error = log_error or logger.error

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._failure_count = 0
        self._last_check_ok = True

    def start(self):
        """启动健康检查"""
        if self._running:
            logger.warning("健康检查已在运行")
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        self._log_info(f"[tg-health] Telegram 连接健康检查已启动 (间隔:{self.check_interval}s)")

    def stop(self):
        """停止健康检查（可中断等待，立即生效）"""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._log_info("[tg-health] Telegram 连接健康检查已停止")

    def _check_loop(self):
        """检查循环"""
        while self._running:
            # 可中断等待：stop() 触发后立即退出，不必等满一个 check_interval
            if self._stop_event.wait(self.check_interval):
                break
            try:
                self._perform_check()
            except Exception as e:
                self._log_error(f"[tg-health] 健康检查异常: {e}")

    def _perform_check(self):
        """执行健康检查"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_check(),
                self.loop
            )
            result = future.result(timeout=30)

            if result:
                if not self._last_check_ok:
                    self._log_info("[tg-health] Telegram 连接已恢复正常")
                self._last_check_ok = True
                self._failure_count = 0
            else:
                self._handle_check_failure()

        except Exception as e:
            self._log_error(f"[tg-health] 健康检查执行失败: {e}")
            self._handle_check_failure()

    async def _async_check(self):
        """异步健康检查（轻量级操作）"""
        try:
            if not self.client.is_connected():
                self._log_warning("[tg-health] 客户端未连接")
                return False

            await asyncio.wait_for(
                self.client.get_dialogs(limit=1),
                timeout=10.0,
            )
            return True
        except asyncio.TimeoutError:
            self._log_warning("[tg-health] 健康检查超时")
            return False
        except Exception as e:
            self._log_warning(f"[tg-health] 健康检查失败: {e}")
            return False

    def _handle_check_failure(self):
        """处理检查失败"""
        self._failure_count += 1
        self._last_check_ok = False

        self._log_warning(
            f"[tg-health] Telegram 连接异常 "
            f"(连续失败: {self._failure_count}/{self.max_retry})"
        )

        if self._failure_count >= self.max_retry:
            self._log_warning("[tg-health] 触发 Telegram 重连")
            self._attempt_reconnect()
            self._failure_count = 0

    def _attempt_reconnect(self):
        """尝试重新连接"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_reconnect(),
                self.loop
            )
            future.result(timeout=30)
            if self.on_reconnect_callback:
                try:
                    self.on_reconnect_callback()
                except Exception as e:
                    self._log_error(f"[tg-health] 重连回调执行失败: {e}")

        except Exception as e:
            self._log_error(f"[tg-health] 重连失败: {e}")

    async def _async_reconnect(self):
        """异步重连"""
        try:
            self._log_info("[tg-health] 正在断开连接...")
            await self.client.disconnect()
            await asyncio.sleep(5)

            self._log_info("[tg-health] 正在重新连接...")
            await self.client.connect()

            if self.client.is_connected():
                self._log_info("[tg-health] Telegram 重连成功")
            else:
                self._log_error("[tg-health] Telegram 重连失败")

        except Exception as e:
            self._log_error(f"[tg-health] 重连过程异常: {e}")

    def get_stats(self) -> dict:
        """
        获取健康检查统计信息

        Returns:
            统计信息字典
        """
        return {
            "running": self._running,
            "check_interval": self.check_interval,
            "max_retry": self.max_retry,
            "failure_count": self._failure_count,
            "status": "healthy" if self._failure_count == 0 else "degraded"
        }
