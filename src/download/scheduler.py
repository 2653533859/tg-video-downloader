"""Thread-safe download queue scheduler.

槽位归属用单调递增的 generation 令牌跟踪：get_next_task 发放当代令牌并占槽，
release_tasks 必须持匹配令牌才真正还槽（陈旧令牌被幂等忽略），
release_scheduled_task（watchdog 撤销）直接作废当代并还槽。
由此消除"正常释放 vs 停滞释放"之间的计数对冲，杜绝槽位泄漏/超发。
"""

import threading
from typing import Callable, Dict, Iterable, Optional

GENERATION_KEY = "_generation"


class DownloadScheduler:
    """Queue plus active-slot tracking used by the legacy downloader."""

    def __init__(self, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self.queue = []
        self.active_downloads = 0
        self.scheduled_task_ids = set()
        # task_id -> 当前占槽的 generation 令牌
        self.active_generations: Dict[str, int] = {}
        self._generation_seq = 0
        self.lock = threading.RLock()

    def is_queued(self, task_id):
        with self.lock:
            return any(task.get("task_id") == task_id for task in self.queue)

    def add_task(self, task, update_positions: Optional[Callable[[], None]] = None):
        with self.lock:
            task_id = task.get("task_id")
            if task_id:
                if task_id in self.scheduled_task_ids or self.is_queued(task_id):
                    if update_positions:
                        update_positions()
                    return False
                self.scheduled_task_ids.add(task_id)
            self.queue.append(task)
            if update_positions:
                update_positions()
            return True

    def get_next_task(self, update_positions: Optional[Callable[[], None]] = None):
        with self.lock:
            if self.queue and self.active_downloads < self.max_concurrent:
                self.active_downloads += 1
                task = self.queue.pop(0)
                task_id = task.get("task_id")
                if task_id:
                    self._generation_seq += 1
                    self.active_generations[task_id] = self._generation_seq
                    task[GENERATION_KEY] = self._generation_seq
                if update_positions:
                    update_positions()
                return task
            return None

    def get_status(self):
        with self.lock:
            return {
                "active": self.active_downloads,
                "queued": len(self.queue),
                "max": self.max_concurrent,
            }

    def remove_task(self, task_id, update_positions: Optional[Callable[[], None]] = None):
        with self.lock:
            for index, task in enumerate(self.queue):
                if task.get("task_id") == task_id:
                    self.queue.pop(index)
                    self.scheduled_task_ids.discard(task_id)
                    if update_positions:
                        update_positions()
                    return True
            return False

    def move_task(self, task_id, action, update_positions: Optional[Callable[[], None]] = None):
        with self.lock:
            index = next((i for i, item in enumerate(self.queue) if item.get("task_id") == task_id), None)
            if index is None:
                return False
            if action == "top":
                target = 0
            elif action == "up":
                target = max(0, index - 1)
            elif action == "down":
                target = min(len(self.queue) - 1, index + 1)
            else:
                return False
            item = self.queue.pop(index)
            self.queue.insert(target, item)
            if update_positions:
                update_positions()
            return True

    def _release_one(self, task_id, generation):
        """内部：在持锁状态下按令牌匹配释放一个占槽。返回是否真正释放。"""
        current = self.active_generations.get(task_id)
        if current is None:
            return False  # 已被释放或作废，幂等忽略
        if generation is not None and generation != current:
            return False  # 陈旧令牌（该代已被 watchdog 作废并重发），忽略
        self.active_downloads = max(0, self.active_downloads - 1)
        del self.active_generations[task_id]
        self.scheduled_task_ids.discard(task_id)
        return True

    def release_tasks(self, tasks: Iterable[Dict]):
        with self.lock:
            for task in tasks:
                self._release_one(task.get("task_id"), task.get(GENERATION_KEY))

    def release_scheduled_task(self, task_id):
        """watchdog/卡死修复撤销当前占槽：作废当代令牌并立即还槽。

        之后原 worker 携带旧令牌来 release 会因不匹配而被忽略，不会重复还槽。
        """
        with self.lock:
            if task_id in self.active_generations:
                self.active_downloads = max(0, self.active_downloads - 1)
                del self.active_generations[task_id]
                self.scheduled_task_ids.discard(task_id)
                return True
            # 尚未占槽（仍在排队）：从队列移除，避免重复入队
            return self.remove_task(task_id)

    def update_positions(self, update_task: Callable[[str, int, int], None]):
        with self.lock:
            queue_length = len(self.queue)
            for index, task in enumerate(self.queue, start=1):
                task_id = task.get("task_id")
                if task_id:
                    update_task(task_id, index, queue_length)
