"""Thread-safe download queue scheduler."""

import threading
from typing import Callable, Dict, Iterable, Optional


class DownloadScheduler:
    """Queue plus active-slot tracking used by the legacy downloader."""

    def __init__(self, max_concurrent: int = 1):
        self.max_concurrent = max_concurrent
        self.queue = []
        self.active_downloads = 0
        self.scheduled_task_ids = set()
        self.released_stalled_task_ids = {}
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

    def release_tasks(self, tasks: Iterable[Dict]):
        with self.lock:
            tasks = list(tasks)
            already_released = False
            for task in tasks:
                task_id = task.get("task_id")
                released_count = self.released_stalled_task_ids.get(task_id, 0)
                if released_count > 0:
                    already_released = True
                    if released_count == 1:
                        self.released_stalled_task_ids.pop(task_id, None)
                    else:
                        self.released_stalled_task_ids[task_id] = released_count - 1

            if not already_released:
                self.active_downloads = max(0, self.active_downloads - 1)
                for task in tasks:
                    self.scheduled_task_ids.discard(task.get("task_id"))

    def release_scheduled_task(self, task_id):
        with self.lock:
            was_scheduled = task_id in self.scheduled_task_ids
            self.scheduled_task_ids.discard(task_id)
            if was_scheduled:
                self.active_downloads = max(0, self.active_downloads - 1)
                self.released_stalled_task_ids[task_id] = self.released_stalled_task_ids.get(task_id, 0) + 1
            return was_scheduled

    def update_positions(self, update_task: Callable[[str, int, int], None]):
        with self.lock:
            queue_length = len(self.queue)
            for index, task in enumerate(self.queue, start=1):
                task_id = task.get("task_id")
                if task_id:
                    update_task(task_id, index, queue_length)
