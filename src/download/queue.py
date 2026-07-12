"""
下载队列管理器
"""
import threading
import time
import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger("tg_downloader.queue")


class DownloadQueue:
    """下载队列管理器"""

    def __init__(self, max_concurrent: int = 1):
        """
        初始化队列管理器

        Args:
            max_concurrent: 最大并发下载数
        """
        self.max_concurrent = max_concurrent
        self.queue: List[Dict[str, Any]] = []
        self.active_downloads = 0
        self.scheduled_task_ids = set()
        self.lock = threading.RLock()

    def add_task(self, task: Dict[str, Any]) -> bool:
        """
        添加任务到队列

        Args:
            task: 任务字典，必须包含 task_id

        Returns:
            是否添加成功
        """
        with self.lock:
            task_id = task.get("task_id")
            if not task_id:
                logger.warning("任务缺少 task_id")
                return False

            # 检查是否已存在
            if task_id in self.scheduled_task_ids:
                logger.info(f"[{task_id}] 已在队列中")
                return False

            if any(item.get("task_id") == task_id for item in self.queue):
                logger.info(f"[{task_id}] 已在队列中")
                return False

            # 添加到队列
            self.scheduled_task_ids.add(task_id)
            self.queue.append(task)
            logger.info(f"[{task_id}] 已加入队列 (位置: {len(self.queue)})")
            return True

    def get_next_task(self) -> Optional[Dict[str, Any]]:
        """
        获取下一个待执行的任务

        Returns:
            任务字典或 None
        """
        with self.lock:
            if self.queue and self.active_downloads < self.max_concurrent:
                self.active_downloads += 1
                task = self.queue.pop(0)
                logger.info(
                    f"[{task.get('task_id')}] 从队列取出 "
                    f"(活跃: {self.active_downloads}/{self.max_concurrent})"
                )
                return task
            return None

    def release_slot(self, task_id: Optional[str] = None):
        """
        释放一个下载槽位

        Args:
            task_id: 任务 ID（用于日志）
        """
        with self.lock:
            if self.active_downloads > 0:
                self.active_downloads -= 1
                if task_id:
                    self.scheduled_task_ids.discard(task_id)
                    logger.info(
                        f"[{task_id}] 释放槽位 "
                        f"(活跃: {self.active_downloads}/{self.max_concurrent})"
                    )

    def get_status(self) -> Dict[str, Any]:
        """
        获取队列状态

        Returns:
            状态字典
        """
        with self.lock:
            return {
                "queue_length": len(self.queue),
                "active_downloads": self.active_downloads,
                "max_concurrent": self.max_concurrent,
                "scheduled_tasks": len(self.scheduled_task_ids)
            }

    def get_queue_position(self, task_id: str) -> Optional[int]:
        """
        获取任务在队列中的位置

        Args:
            task_id: 任务 ID

        Returns:
            位置（1-based）或 None
        """
        with self.lock:
            for idx, task in enumerate(self.queue, start=1):
                if task.get("task_id") == task_id:
                    return idx
            return None

    def remove_task(self, task_id: str) -> bool:
        """
        从队列中移除任务

        Args:
            task_id: 任务 ID

        Returns:
            是否移除成功
        """
        with self.lock:
            initial_length = len(self.queue)
            self.queue = [task for task in self.queue if task.get("task_id") != task_id]
            self.scheduled_task_ids.discard(task_id)

            removed = len(self.queue) < initial_length
            if removed:
                logger.info(f"[{task_id}] 已从队列移除")
            return removed

    def clear_queue(self):
        """清空队列"""
        with self.lock:
            count = len(self.queue)
            self.queue.clear()
            self.scheduled_task_ids.clear()
            logger.info(f"队列已清空 ({count} 个任务)")

    def is_task_queued(self, task_id: str) -> bool:
        """
        检查任务是否在队列中

        Args:
            task_id: 任务 ID

        Returns:
            是否在队列中
        """
        with self.lock:
            return any(task.get("task_id") == task_id for task in self.queue)

    def get_all_tasks(self) -> List[Dict[str, Any]]:
        """
        获取队列中的所有任务

        Returns:
            任务列表（副本）
        """
        with self.lock:
            return self.queue.copy()
