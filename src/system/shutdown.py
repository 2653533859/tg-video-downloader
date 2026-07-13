"""Graceful shutdown orchestration for background runtime threads.

后台线程原本全是 daemon，进程退出直接被杀、无清理路径（下载中的文件与
任务状态可能不一致）。GracefulShutdown 提供有序停止：

    set(stop_event) → 各 stoppable.stop() → 断开 Telegram 客户端 →
    join 后台线程（有界超时）→ 关闭持久化连接

每一步都是防御式的（单步异常不阻塞后续），且整体幂等（重复信号只执行一次）。
"""

import signal
import threading


class GracefulShutdown:
    def __init__(
        self,
        *,
        stop_event,
        stoppables=(),
        disconnect_clients=None,
        close_persistence=None,
        join_threads=(),
        join_timeout=5,
        log_info=None,
    ):
        self.stop_event = stop_event
        self.stoppables = list(stoppables)
        self.disconnect_clients = disconnect_clients
        self.close_persistence = close_persistence
        self.join_threads = list(join_threads)
        self.join_timeout = join_timeout
        self.log_info = log_info or (lambda message: None)
        self._done = threading.Event()

    def shutdown(self, *_args):
        # 幂等：多次信号 / 重复调用只执行一次
        if self._done.is_set():
            return
        self._done.set()
        self.log_info("[shutdown] 开始优雅退出...")

        self.stop_event.set()

        for obj in self.stoppables:
            self._safe(lambda o=obj: o.stop())

        if self.disconnect_clients is not None:
            self._safe(self.disconnect_clients)

        for item in self.join_threads:
            self._safe(lambda it=item: self._join(it))

        if self.close_persistence is not None:
            self._safe(self.close_persistence)

        self.log_info("[shutdown] 优雅退出完成")

    def _join(self, item):
        # 支持传线程对象或返回线程对象的可调用（线程可能延迟创建）
        thread = item() if callable(item) else item
        if thread is not None:
            thread.join(timeout=self.join_timeout)

    @staticmethod
    def _safe(func):
        try:
            func()
        except Exception:
            pass

    def install_signal_handlers(self):
        """在主线程注册 SIGTERM/SIGINT；非主线程或不支持时静默跳过。"""
        installed = []
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self._on_signal)
                installed.append(sig)
            except (ValueError, OSError):
                pass
        return installed

    def _on_signal(self, _signum, _frame):
        self.shutdown()
        raise SystemExit(0)
