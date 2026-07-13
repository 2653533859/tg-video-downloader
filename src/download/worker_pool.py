"""Fixed-size daemon worker pool for download dispatch.

替代「每任务新建 threading.Thread(daemon=True)」的模型：固定数量的常驻 daemon
线程从内部队列取任务并执行，线程复用、数量恒定。

为何不用 ThreadPoolExecutor：其 worker 线程是非 daemon 且注册了 atexit join，
下载进行中收到停机信号会阻塞进程退出，与 GracefulShutdown 的即时退出语义冲突。
本实现用 daemon 线程保证进程退出即时结束（优雅停止由外部 stop_event + worker 内
取消检查负责），并提供 stop() 便于并入 GracefulShutdown 的 stoppables。
"""

import queue
import threading


class DownloadWorkerPool:
    def __init__(self, size, worker, *, name="dl-worker"):
        self._size = max(1, int(size))
        self._worker = worker  # callable(task_items, dialog_name)
        self._queue = queue.Queue()
        self._threads = []
        self._started = False
        self._start_lock = threading.Lock()
        self._name = name

    def start(self):
        with self._start_lock:
            if self._started:
                return
            self._started = True
            for i in range(self._size):
                thread = threading.Thread(
                    target=self._loop, name=f"{self._name}-{i}", daemon=True
                )
                thread.start()
                self._threads.append(thread)

    def submit(self, task_items, dialog_name):
        # 懒启动：首次提交时才拉起 worker，避免 import 期就创建线程
        if not self._started:
            self.start()
        self._queue.put((task_items, dialog_name))

    def _loop(self):
        while True:
            item = self._queue.get()
            try:
                if item is None:  # 退出哨兵
                    return
                task_items, dialog_name = item
                try:
                    self._worker(task_items, dialog_name)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    def stop(self):
        # 放入哨兵让空闲 worker 自然退出；daemon 线程即使正忙也不阻塞进程退出
        for _ in self._threads:
            self._queue.put(None)

    def qsize(self):
        return self._queue.qsize()
