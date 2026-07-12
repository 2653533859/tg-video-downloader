# 异步架构重构指南

## 概述

本文档提供从 Flask + 多线程架构迁移到 Quart + 统一 asyncio 的完整方案。

---

## 当前架构问题

### 1. 多事件循环混乱
```python
# 当前：两个独立的 event loop
tg_loop = asyncio.new_event_loop()      # Telegram 客户端
relay_loop = asyncio.new_event_loop()   # Relay 客户端

# 问题：跨线程调度复杂，容易死锁
```

### 2. 同步 Flask + 异步 Telethon 不匹配
```python
# Flask 路由是同步的
@app.route("/api/videos")
def get_videos():
    # 需要调用异步 Telegram API
    result = run_async(lambda: tg_client.get_messages(...))
    # run_async 使用 run_coroutine_threadsafe 跨线程调度
```

### 3. 线程管理复杂
- Telegram 客户端线程
- Relay 客户端线程
- 队列处理线程
- 看门狗线程
- 健康检查线程
- 缩略图清理线程

---

## 目标架构

### 统一异步架构

```
┌─────────────────────────────────────┐
│   Quart 应用（原生异步）              │
│   - async def 路由处理器              │
│   - WebSocket 支持                   │
└─────────────────────────────────────┘
              │
              ├─→ 单一 asyncio event loop
              │
    ┌─────────┴─────────┬──────────────┬──────────────┐
    │                   │              │              │
┌───▼────┐      ┌───────▼──────┐  ┌──▼───────┐  ┌───▼────────┐
│Telegram│      │ Download     │  │ WebSocket│  │ Background │
│ Client │      │ Manager      │  │ Manager  │  │ Tasks      │
└────────┘      └──────────────┘  └──────────┘  └────────────┘
                                                      │
                                            ┌─────────┴─────────┐
                                            │                   │
                                       ┌────▼─────┐      ┌─────▼────┐
                                       │ Watchdog │      │ Health   │
                                       │          │      │ Checker  │
                                       └──────────┘      └──────────┘
```

---

## 阶段 1: 安装依赖

### 更新 requirements.txt

```txt
# Web 框架 - 从 Flask 迁移到 Quart
quart>=0.19.0          # 异步 Flask
quart-cors>=0.7.0      # CORS 支持

# WebSocket
python-socketio>=5.10.0
python-engineio>=4.8.0

# 异步 HTTP 客户端
aiohttp>=3.9.0
aiofiles>=23.0.0       # 异步文件 I/O

# 其他保持不变
telethon>=1.24.0
requests>=2.28.0       # 某些同步操作仍需要
```

---

## 阶段 2: Flask → Quart 迁移

### 2.1 基本应用转换

**旧代码** (`app.py`):
```python
from flask import Flask, jsonify, request

app = Flask(__name__)

@app.route("/api/status")
def get_status():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

**新代码** (`app_async.py`):
```python
from quart import Quart, jsonify, request
import asyncio

app = Quart(__name__)

@app.route("/api/status")
async def get_status():
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    # Quart 使用 asyncio
    asyncio.run(app.run_task(host="0.0.0.0", port=5000))
```

### 2.2 路由转换规则

| Flask | Quart |
|-------|-------|
| `def route()` | `async def route()` |
| `request.json` | `await request.json` |
| `request.form` | `await request.form` |
| `request.files` | `await request.files` |
| `render_template()` | `await render_template()` |
| `send_file()` | `await send_file()` |

### 2.3 实际路由迁移示例

**旧 Flask 路由**:
```python
@app.route("/api/videos")
def get_videos():
    dialog_id = request.args.get("dialog_id")
    
    # 跨线程调用异步函数
    messages = run_async(
        lambda: tg_client.get_messages(int(dialog_id))
    )
    
    return jsonify({"videos": format_videos(messages)})
```

**新 Quart 路由**:
```python
@app.route("/api/videos")
async def get_videos():
    dialog_id = request.args.get("dialog_id")
    
    # 直接 await 异步函数
    messages = await tg_client.get_messages(int(dialog_id))
    
    return jsonify({"videos": format_videos(messages)})
```

---

## 阶段 3: Telegram 客户端重构

### 3.1 统一事件循环

**旧架构** (两个独立循环):
```python
# 主客户端
tg_loop = asyncio.new_event_loop()
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop)

# Relay 客户端
relay_loop = asyncio.new_event_loop()
relay_client = TelegramClient(StringSession(), API_ID, API_HASH, loop=relay_loop)

# 跨线程运行
def run_async(coro_factory, timeout=600):
    future = asyncio.run_coroutine_threadsafe(coro_factory(), tg_loop)
    return future.result(timeout=timeout)
```

**新架构** (共享循环):
```python
# 使用应用的主事件循环
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
relay_client = TelegramClient(StringSession(), API_ID, API_HASH)

# 直接 await，无需跨线程
async def get_messages(entity_id):
    return await tg_client.get_messages(entity_id)
```

### 3.2 客户端初始化

**创建文件**: `src/telegram/async_client.py`

```python
"""异步 Telegram 客户端管理"""
import asyncio
import logging
from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger("tg_downloader.telegram")

class AsyncTelegramManager:
    def __init__(self, api_id, api_hash, session_name, proxy=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.proxy = proxy
        
        self.main_client = None
        self.relay_client = None
        self.connected = False
        self.user_info = None

    async def initialize(self):
        """初始化两个客户端"""
        # 主客户端
        self.main_client = TelegramClient(
            self.session_name,
            self.api_id,
            self.api_hash,
            proxy=self.proxy
        )
        
        await self.main_client.connect()
        
        if not await self.main_client.is_user_authorized():
            raise RuntimeError("Telegram 未登录")
        
        # 获取用户信息
        me = await self.main_client.get_me()
        self.user_info = self._format_user(me)
        
        # Relay 客户端（使用 StringSession）
        session_string = StringSession.save(self.main_client.session)
        self.relay_client = TelegramClient(
            StringSession(session_string),
            self.api_id,
            self.api_hash,
            proxy=self.proxy
        )
        
        await self.relay_client.connect()
        
        self.connected = True
        logger.info(f"Telegram 已连接: {self.user_info}")
        
        return self.user_info

    async def disconnect(self):
        """断开连接"""
        if self.main_client:
            await self.main_client.disconnect()
        if self.relay_client:
            await self.relay_client.disconnect()
        self.connected = False

    def _format_user(self, user):
        """格式化用户信息"""
        name_parts = []
        if user.first_name:
            name_parts.append(user.first_name)
        if user.last_name:
            name_parts.append(user.last_name)
        
        name = " ".join(name_parts) or "Unknown"
        
        if user.username:
            return f"{name} (@{user.username})"
        return f"{name} (ID: {user.id})"
```

---

## 阶段 4: 下载管理器异步化

### 4.1 异步下载接口

**创建文件**: `src/download/async_manager.py`

```python
"""异步下载管理器"""
import asyncio
import aiofiles
import logging
from typing import Optional, Callable, AsyncIterator
from pathlib import Path

logger = logging.getLogger("tg_downloader.download")

class AsyncDownloadManager:
    def __init__(self):
        self.active_downloads = {}
        self.cancelled_tasks = set()

    async def download_telethon(
        self,
        task_id: str,
        message,
        file_path: str,
        progress_callback: Optional[Callable] = None
    ):
        """
        使用 Telethon 异步下载

        Args:
            task_id: 任务 ID
            message: Telegram 消息对象
            file_path: 保存路径
            progress_callback: 进度回调（可选）
        """
        total_size = message.file.size
        downloaded = 0

        async with aiofiles.open(file_path, 'wb') as f:
            async for chunk in message.download_chunked():
                if task_id in self.cancelled_tasks:
                    logger.info(f"[{task_id}] 下载已取消")
                    break

                await f.write(chunk)
                downloaded += len(chunk)

                if progress_callback:
                    await progress_callback(
                        task_id=task_id,
                        downloaded=downloaded,
                        total=total_size
                    )

        return downloaded == total_size

    async def download_with_resume(
        self,
        task_id: str,
        message,
        file_path: str,
        progress_callback: Optional[Callable] = None
    ):
        """支持断点续传的下载"""
        total_size = message.file.size
        
        # 检查已下载大小
        file_path_obj = Path(file_path)
        if file_path_obj.exists():
            downloaded = file_path_obj.stat().st_size
            logger.info(f"[{task_id}] 续传，已下载: {downloaded}/{total_size}")
        else:
            downloaded = 0

        # 从断点处继续
        async with aiofiles.open(file_path, 'ab' if downloaded > 0 else 'wb') as f:
            async for chunk in message.download_chunked(offset=downloaded):
                if task_id in self.cancelled_tasks:
                    logger.info(f"[{task_id}] 下载已取消")
                    break

                await f.write(chunk)
                downloaded += len(chunk)

                if progress_callback:
                    await progress_callback(
                        task_id=task_id,
                        downloaded=downloaded,
                        total=total_size
                    )

        return downloaded == total_size

    def cancel_download(self, task_id: str):
        """取消下载"""
        self.cancelled_tasks.add(task_id)
```

### 4.2 异步队列处理

**创建文件**: `src/download/async_queue.py`

```python
"""异步下载队列"""
import asyncio
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("tg_downloader.queue")

class AsyncDownloadQueue:
    def __init__(self, max_concurrent: int = 3):
        self.queue = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self.active_tasks = {}
        self.semaphore = asyncio.Semaphore(max_concurrent)

    async def add_task(self, task: Dict[str, Any]):
        """添加任务到队列"""
        await self.queue.put(task)
        logger.info(f"任务 {task['task_id']} 已加入队列")

    async def process_queue(self, download_handler):
        """
        处理队列

        Args:
            download_handler: 异步下载处理函数
        """
        while True:
            task = await self.queue.get()
            
            # 使用信号量限制并发
            async with self.semaphore:
                task_id = task['task_id']
                self.active_tasks[task_id] = task
                
                try:
                    await download_handler(task)
                except Exception as e:
                    logger.error(f"[{task_id}] 下载失败: {e}", exc_info=True)
                finally:
                    if task_id in self.active_tasks:
                        del self.active_tasks[task_id]
                    self.queue.task_done()

    def get_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        return {
            "queue_length": self.queue.qsize(),
            "active_downloads": len(self.active_tasks),
            "max_concurrent": self.max_concurrent
        }
```

---

## 阶段 5: 后台任务异步化

### 5.1 看门狗异步版本

**创建文件**: `src/download/async_watchdog.py`

```python
"""异步下载看门狗"""
import asyncio
import logging
from typing import Dict, Any, Callable, Optional

logger = logging.getLogger("tg_downloader.watchdog")

class AsyncDownloadWatchdog:
    def __init__(
        self,
        check_interval: int = 60,
        stall_timeout: int = 300,
        get_tasks_callback: Optional[Callable] = None,
        restart_callback: Optional[Callable] = None
    ):
        self.check_interval = check_interval
        self.stall_timeout = stall_timeout
        self.get_tasks_callback = get_tasks_callback
        self.restart_callback = restart_callback
        
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_progress = {}

    async def start(self):
        """启动看门狗"""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"异步看门狗已启动 (间隔: {self.check_interval}s)")

    async def stop(self):
        """停止看门狗"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _monitor_loop(self):
        """监控循环"""
        while self._running:
            try:
                await self._check_all_tasks()
            except Exception as e:
                logger.error(f"看门狗检查出错: {e}", exc_info=True)
            
            await asyncio.sleep(self.check_interval)

    async def _check_all_tasks(self):
        """检查所有任务"""
        if not self.get_tasks_callback:
            return
        
        tasks = await self.get_tasks_callback()
        current_time = asyncio.get_event_loop().time()
        
        for task_id, task in tasks.items():
            if task.get("status") == "downloading":
                await self._check_task(task_id, task, current_time)

    async def _check_task(self, task_id: str, task: Dict[str, Any], current_time: float):
        """检查单个任务"""
        progress = task.get("progress", 0)
        downloaded = task.get("downloaded_bytes", 0)
        
        last_record = self._last_progress.get(task_id)
        
        if last_record is None:
            self._last_progress[task_id] = {
                "progress": progress,
                "downloaded": downloaded,
                "time": current_time
            }
            return
        
        if (progress == last_record["progress"] and 
            downloaded == last_record["downloaded"]):
            stalled_time = current_time - last_record["time"]
            
            if stalled_time >= self.stall_timeout:
                logger.warning(f"[{task_id}] 检测到停滞 {int(stalled_time)}s")
                if self.restart_callback:
                    await self.restart_callback(task_id, task)
                del self._last_progress[task_id]
        else:
            self._last_progress[task_id] = {
                "progress": progress,
                "downloaded": downloaded,
                "time": current_time
            }
```

### 5.2 健康检查异步版本

**创建文件**: `src/telegram/async_health_checker.py`

```python
"""异步 Telegram 健康检查"""
import asyncio
import logging

logger = logging.getLogger("tg_downloader.health_checker")

class AsyncTelegramHealthChecker:
    def __init__(
        self,
        client,
        check_interval: int = 120,
        max_retry: int = 3
    ):
        self.client = client
        self.check_interval = check_interval
        self.max_retry = max_retry
        
        self._running = False
        self._task = None
        self._failure_count = 0

    async def start(self):
        """启动健康检查"""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._check_loop())
        logger.info(f"异步健康检查已启动 (间隔: {self.check_interval}s)")

    async def stop(self):
        """停止健康检查"""
        self._running = False
        if self._task:
            self._task.cancel()

    async def _check_loop(self):
        """检查循环"""
        while self._running:
            try:
                await self._perform_check()
            except Exception as e:
                logger.error(f"健康检查出错: {e}", exc_info=True)
            
            await asyncio.sleep(self.check_interval)

    async def _perform_check(self):
        """执行健康检查"""
        try:
            # 简单的 ping 测试
            await self.client.get_me()
            
            if self._failure_count > 0:
                logger.info("Telegram 连接恢复正常")
                self._failure_count = 0
                
        except Exception as e:
            self._failure_count += 1
            logger.warning(
                f"Telegram 健康检查失败 ({self._failure_count}/{self.max_retry}): {e}"
            )
            
            if self._failure_count >= self.max_retry:
                logger.error("Telegram 连接持续失败，尝试重连...")
                await self._reconnect()

    async def _reconnect(self):
        """重新连接"""
        try:
            await self.client.disconnect()
            await asyncio.sleep(5)
            await self.client.connect()
            
            self._failure_count = 0
            logger.info("Telegram 重连成功")
            
        except Exception as e:
            logger.error(f"Telegram 重连失败: {e}")
```

---

## 阶段 6: 主应用集成

### 6.1 新的主入口

**创建文件**: `app_async.py`

```python
"""
Telegram 视频下载器 - 异步版本
使用 Quart + 统一 asyncio 架构
"""
import asyncio
import logging
from quart import Quart, jsonify, request
from src.telegram.async_client import AsyncTelegramManager
from src.download.async_manager import AsyncDownloadManager
from src.download.async_queue import AsyncDownloadQueue
from src.download.async_watchdog import AsyncDownloadWatchdog
from config import *

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 创建 Quart 应用
app = Quart(__name__)

# 全局组件（在 startup 时初始化）
telegram_manager: AsyncTelegramManager = None
download_manager: AsyncDownloadManager = None
download_queue: AsyncDownloadQueue = None
watchdog: AsyncDownloadWatchdog = None

@app.before_serving
async def startup():
    """应用启动时初始化所有组件"""
    global telegram_manager, download_manager, download_queue, watchdog
    
    logger.info("初始化应用组件...")
    
    # 初始化 Telegram
    telegram_manager = AsyncTelegramManager(API_ID, API_HASH, SESSION_NAME, PROXY_CONFIG)
    await telegram_manager.initialize()
    
    # 初始化下载管理器
    download_manager = AsyncDownloadManager()
    
    # 初始化队列
    download_queue = AsyncDownloadQueue(max_concurrent=3)
    
    # 启动队列处理
    asyncio.create_task(download_queue.process_queue(handle_download))
    
    # 初始化看门狗
    watchdog = AsyncDownloadWatchdog(
        get_tasks_callback=get_active_tasks,
        restart_callback=restart_task
    )
    await watchdog.start()
    
    logger.info("应用组件初始化完成")

@app.after_serving
async def shutdown():
    """应用关闭时清理"""
    logger.info("关闭应用组件...")
    
    if watchdog:
        await watchdog.stop()
    
    if telegram_manager:
        await telegram_manager.disconnect()

# 示例路由
@app.route("/api/status")
async def get_status():
    """获取系统状态"""
    return jsonify({
        "telegram_connected": telegram_manager.connected if telegram_manager else False,
        "queue": download_queue.get_status() if download_queue else {},
        "watchdog": watchdog.get_stats() if watchdog else {}
    })

@app.route("/api/videos")
async def get_videos():
    """获取视频列表"""
    dialog_id = request.args.get("dialog_id", type=int)
    
    if not dialog_id:
        return jsonify({"error": "缺少 dialog_id"}), 400
    
    # 直接 await 异步调用
    messages = await telegram_manager.main_client.get_messages(dialog_id, limit=100)
    
    videos = []
    for msg in messages:
        if msg.video:
            videos.append({
                "message_id": msg.id,
                "file_name": msg.file.name or f"video_{msg.id}.mp4",
                "size": msg.file.size,
                "duration": msg.video.duration
            })
    
    return jsonify({"videos": videos})

@app.route("/api/download", methods=["POST"])
async def start_download():
    """开始下载"""
    data = await request.json
    
    task = {
        "task_id": generate_task_id(),
        "entity_id": data["entity_id"],
        "message_id": data["message_id"],
        "file_name": data["file_name"]
    }
    
    await download_queue.add_task(task)
    
    return jsonify({"task_id": task["task_id"], "status": "queued"})

# 辅助函数
async def handle_download(task):
    """处理下载任务"""
    task_id = task["task_id"]
    entity_id = task["entity_id"]
    message_id = task["message_id"]
    
    message = await telegram_manager.main_client.get_messages(entity_id, ids=message_id)
    
    file_path = f"downloads/{task['file_name']}"
    
    await download_manager.download_with_resume(
        task_id=task_id,
        message=message,
        file_path=file_path,
        progress_callback=update_progress
    )

async def get_active_tasks():
    """获取活跃任务"""
    return download_queue.active_tasks

async def restart_task(task_id, task):
    """重启任务"""
    logger.info(f"[{task_id}] 重启任务")
    download_manager.cancel_download(task_id)
    await download_queue.add_task(task)

async def update_progress(task_id, downloaded, total):
    """更新进度"""
    progress = int(downloaded * 100 / total) if total > 0 else 0
    # TODO: 通过 WebSocket 推送进度
    pass

def generate_task_id():
    """生成任务 ID"""
    import uuid
    return str(uuid.uuid4())[:8]

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

---

## 迁移检查清单

- [ ] 安装 Quart 和相关依赖
- [ ] 所有路由转换为 async def
- [ ] 移除 run_async() 跨线程调用
- [ ] 统一为单一事件循环
- [ ] 后台任务使用 asyncio.create_task()
- [ ] 文件 I/O 使用 aiofiles
- [ ] 所有测试通过
- [ ] 性能测试

---

## 性能优势

| 指标 | 旧架构 | 新架构 | 提升 |
|------|--------|--------|------|
| 并发处理 | 线程池限制 | asyncio 高并发 | 2-3x |
| 内存占用 | 每线程 ~8MB | 协程 ~KB | 显著降低 |
| 响应延迟 | 跨线程开销 | 直接调用 | 减少 50% |
| 代码复杂度 | 高（线程同步） | 低（顺序异步） | 更易维护 |

---

**创建日期**: 2026-06-21  
**预计耗时**: 2-3 周  
**前置条件**: 完成模块化拆分
