"""
状态管理模块
管理下载任务的状态
"""
import threading
import time
import json
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("tg_downloader.state")

# 终态
TERMINAL_STATES = {"done", "error", "cancelled"}


class TaskStateManager:
    """任务状态管理器"""

    def __init__(self):
        """初始化状态管理器"""
        self.states: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.cancelled_tasks = set()

    def set_state(self, task_id: str, state: Dict[str, Any]):
        """
        设置任务状态

        Args:
            task_id: 任务 ID
            state: 状态字典
        """
        with self.lock:
            state["updated_at"] = time.time()
            self.states[task_id] = state
            logger.debug(f"[{task_id}] 状态更新: {state.get('status')}")

    def update_state(self, task_id: str, **updates):
        """
        更新任务状态

        Args:
            task_id: 任务 ID
            **updates: 要更新的字段
        """
        with self.lock:
            if task_id not in self.states:
                logger.warning(f"[{task_id}] 状态不存在，无法更新")
                return

            updates["updated_at"] = time.time()
            self.states[task_id].update(updates)
            logger.debug(f"[{task_id}] 状态更新: {updates}")

    def get_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """
        获取任务状态

        Args:
            task_id: 任务 ID

        Returns:
            状态字典或 None
        """
        with self.lock:
            return self.states.get(task_id, {}).copy() if task_id in self.states else None

    def get_all_states(self) -> Dict[str, Dict[str, Any]]:
        """
        获取所有任务状态

        Returns:
            状态字典（副本）
        """
        with self.lock:
            return {k: v.copy() for k, v in self.states.items()}

    def remove_state(self, task_id: str) -> bool:
        """
        移除任务状态

        Args:
            task_id: 任务 ID

        Returns:
            是否移除成功
        """
        with self.lock:
            if task_id in self.states:
                del self.states[task_id]
                self.cancelled_tasks.discard(task_id)
                logger.info(f"[{task_id}] 状态已移除")
                return True
            return False

    def mark_cancelled(self, task_id: str):
        """
        标记任务为已取消

        Args:
            task_id: 任务 ID
        """
        with self.lock:
            self.cancelled_tasks.add(task_id)
            logger.info(f"[{task_id}] 已标记为取消")

    def is_cancelled(self, task_id: str) -> bool:
        """
        检查任务是否已取消

        Args:
            task_id: 任务 ID

        Returns:
            是否已取消
        """
        with self.lock:
            return task_id in self.cancelled_tasks

    def clear_cancelled(self, task_id: str):
        """
        清除取消标记

        Args:
            task_id: 任务 ID
        """
        with self.lock:
            self.cancelled_tasks.discard(task_id)

    def get_tasks_by_status(self, status: str) -> Dict[str, Dict[str, Any]]:
        """
        按状态获取任务

        Args:
            status: 状态值

        Returns:
            符合条件的任务字典
        """
        with self.lock:
            return {
                k: v.copy()
                for k, v in self.states.items()
                if v.get("status") == status
            }

    def cleanup_terminal_states(self, max_age_seconds: int = 3600):
        """
        清理过期的终态任务

        Args:
            max_age_seconds: 最大保留时间（秒）
        """
        with self.lock:
            now = time.time()
            to_remove = []

            for task_id, state in self.states.items():
                if state.get("status") in TERMINAL_STATES:
                    finish_time = state.get("finish_time", state.get("updated_at", 0))
                    if now - finish_time > max_age_seconds:
                        to_remove.append(task_id)

            for task_id in to_remove:
                del self.states[task_id]
                self.cancelled_tasks.discard(task_id)

            if to_remove:
                logger.info(f"清理了 {len(to_remove)} 个过期的终态任务")

    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息

        Returns:
            统计信息字典
        """
        with self.lock:
            stats = {
                "total": len(self.states),
                "cancelled": len(self.cancelled_tasks)
            }

            # 按状态统计
            status_counts = {}
            for state in self.states.values():
                status = state.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            stats["by_status"] = status_counts
            return stats
