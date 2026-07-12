"""
异步下载管理器
使用 asyncio 实现并发下载管理
"""
import asyncio
import time
import logging
from typing import Optional, Callable, Dict, Any
from pathlib import Path

logger = logging.getLogger("tg_downloader.async_manager")


class AsyncDownloadManager:
    """异步下载管理器"""

    def __init__(
        self,
        max_concurrent: int = 3,
        progress_callback: Optional[Callable] = None
    ):
        """
        初始化

        Args:
            max_concurrent: 最大并发下载数
            progress_callback: 进度回调函数
        """
        self.max_concurrent = max_concurrent
        self.progress_callback = progress_callback

        self.active_tasks: Dict[str, asyncio.Task] = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.is_running = False

    async def start(self):
        """启动管理器"""
        self.is_running = True
        logger.info(f"异步下载管理器已启动 (并发: {self.max_concurrent})")

    async def stop(self):
        """停止管理器"""
        self.is_running = False

        # 取消所有活跃任务
        for task_id, task in list(self.active_tasks.items()):
            if not task.done():
                task.cancel()

        # 等待所有任务完成
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)

        logger.info("异步下载管理器已停止")

    async def download_file(
        self,
        task_id: str,
        client,
        message,
        output_path: Path,
        chunk_size: int = 512 * 1024
    ):
        """
        下载文件

        Args:
            task_id: 任务 ID
            client: Telegram 客户端
            message: 消息对象
            output_path: 输出路径
            chunk_size: 块大小
        """
        async with self.semaphore:
            try:
                logger.info(f"[{task_id}] 开始下载")

                # 获取文件大小
                file_size = message.media.document.size if hasattr(message.media, 'document') else 0

                # 下载文件
                downloaded = 0
                start_time = time.time()

                with open(output_path, 'wb') as f:
                    async for chunk in client.iter_download(
                        message.media,
                        chunk_size=chunk_size
                    ):
                        f.write(chunk)
                        downloaded += len(chunk)

                        # 计算进度和速度
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        progress = int(downloaded / file_size * 100) if file_size > 0 else 0

                        # 回调进度
                        if self.progress_callback:
                            await self.progress_callback({
                                "task_id": task_id,
                                "progress": progress,
                                "downloaded": downloaded,
                                "total": file_size,
                                "speed": speed,
                                "status": "downloading"
                            })

                        # 每 1MB 或 1 秒更新一次
                        if downloaded % (1024 * 1024) < chunk_size:
                            await asyncio.sleep(0)  # 让出控制权

                logger.info(f"[{task_id}] 下载完成")

                # 最终回调
                if self.progress_callback:
                    await self.progress_callback({
                        "task_id": task_id,
                        "progress": 100,
                        "downloaded": downloaded,
                        "total": file_size,
                        "speed": 0,
                        "status": "done"
                    })

                return True

            except asyncio.CancelledError:
                logger.info(f"[{task_id}] 下载取消")
                if self.progress_callback:
                    await self.progress_callback({
                        "task_id": task_id,
                        "status": "cancelled",
                        "error": "用户取消"
                    })
                return False

            except Exception as e:
                logger.error(f"[{task_id}] 下载失败: {e}")
                if self.progress_callback:
                    await self.progress_callback({
                        "task_id": task_id,
                        "status": "error",
                        "error": str(e)
                    })
                return False

            finally:
                # 移除活跃任务
                self.active_tasks.pop(task_id, None)

    async def submit_download(
        self,
        task_id: str,
        client,
        message,
        output_path: Path
    ):
        """
        提交下载任务

        Args:
            task_id: 任务 ID
            client: Telegram 客户端
            message: 消息对象
            output_path: 输出路径

        Returns:
            任务对象
        """
        if task_id in self.active_tasks:
            logger.warning(f"[{task_id}] 任务已存在")
            return None

        # 创建任务
        task = asyncio.create_task(
            self.download_file(task_id, client, message, output_path)
        )

        self.active_tasks[task_id] = task
        logger.info(f"[{task_id}] 任务已提交 (活跃: {len(self.active_tasks)})")

        return task

    async def cancel_download(self, task_id: str) -> bool:
        """
        取消下载

        Args:
            task_id: 任务 ID

        Returns:
            是否取消成功
        """
        task = self.active_tasks.get(task_id)

        if not task:
            logger.warning(f"[{task_id}] 任务不存在")
            return False

        if task.done():
            logger.warning(f"[{task_id}] 任务已完成")
            return False

        task.cancel()
        logger.info(f"[{task_id}] 已取消")
        return True

    def get_active_count(self) -> int:
        """获取活跃任务数"""
        return len(self.active_tasks)

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "max_concurrent": self.max_concurrent,
            "active_tasks": len(self.active_tasks),
            "is_running": self.is_running
        }


class AsyncDownloadQueue:
    """异步下载队列"""

    def __init__(self, manager: AsyncDownloadManager):
        """
        初始化

        Args:
            manager: 下载管理器
        """
        self.manager = manager
        self.queue: asyncio.Queue = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None

    async def start(self):
        """启动队列处理器"""
        if self.worker_task and not self.worker_task.done():
            logger.warning("队列处理器已在运行")
            return

        self.worker_task = asyncio.create_task(self._worker())
        logger.info("队列处理器已启动")

    async def stop(self):
        """停止队列处理器"""
        if self.worker_task and not self.worker_task.done():
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass

        logger.info("队列处理器已停止")

    async def _worker(self):
        """队列工作线程"""
        while True:
            try:
                # 从队列获取任务
                item = await self.queue.get()

                task_id = item['task_id']
                client = item['client']
                message = item['message']
                output_path = item['output_path']

                # 提交下载
                await self.manager.submit_download(
                    task_id, client, message, output_path
                )

                # 标记完成
                self.queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列处理错误: {e}")
                await asyncio.sleep(1)

    async def add_task(
        self,
        task_id: str,
        client,
        message,
        output_path: Path
    ):
        """
        添加任务到队列

        Args:
            task_id: 任务 ID
            client: Telegram 客户端
            message: 消息对象
            output_path: 输出路径
        """
        await self.queue.put({
            'task_id': task_id,
            'client': client,
            'message': message,
            'output_path': output_path
        })

        logger.info(f"[{task_id}] 已加入队列 (队列长度: {self.queue.qsize()})")

    def get_queue_size(self) -> int:
        """获取队列大小"""
        return self.queue.qsize()
