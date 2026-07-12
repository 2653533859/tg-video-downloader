# 代码模块化重构指南

## 概述

本文档提供将 `app.py` (4000+ 行) 拆分成模块化架构的完整方案。采用**渐进式重构**策略，确保每一步都可独立完成和测试。

---

## 目标架构

```
src/
├── __init__.py
├── app.py                  # 主入口（精简版）
├── config.py               # 配置管理
│
├── telegram/               # Telegram 相关
│   ├── __init__.py
│   ├── client.py          # 客户端管理
│   ├── health_checker.py  # 健康检查
│   └── message_handler.py # 消息处理
│
├── download/               # 下载相关
│   ├── __init__.py
│   ├── manager.py         # 下载管理器
│   ├── queue.py           # 队列处理
│   ├── watchdog.py        # 看门狗
│   └── methods.py         # 下载方法（Telethon/aria2/tdl）
│
├── routes/                 # Flask 路由
│   ├── __init__.py
│   ├── api.py             # 通用 API
│   ├── telegram_api.py    # Telegram 相关 API
│   ├── download_api.py    # 下载相关 API
│   └── admin_api.py       # 管理 API
│
├── utils/                  # 工具函数
│   ├── __init__.py
│   ├── formatting.py      # 格式化工具
│   ├── validators.py      # 验证器
│   └── cache.py           # 缓存管理
│
└── models/                 # 数据模型
    ├── __init__.py
    ├── task.py            # 任务模型
    └── state.py           # 状态管理
```

---

## 阶段 1: 工具函数提取（已完成示例）

### 1.1 格式化函数

✅ **已创建**: `src/utils/formatting.py`

包含函数：
- `format_size()` - 文件大小格式化
- `format_speed()` - 速度格式化  
- `format_time()` - 时间格式化
- `make_excerpt()` - 文本摘要
- `sanitize_filename()` - 文件名清理
- `format_user_display()` - 用户信息格式化
- `parse_message_text()` - 消息文本提取

**迁移步骤**：
```python
# 在 app.py 中
from src.utils.formatting import format_size, format_speed, format_time

# 替换所有内部定义
# 旧：def format_size(size_bytes): ...
# 新：from src.utils.formatting import format_size
```

### 1.2 验证器函数

**创建文件**: `src/utils/validators.py`

```python
"""验证器函数"""
import os
import re
from typing import Optional

def is_valid_path(path: str, base_dir: str) -> bool:
    """验证路径安全性（防目录遍历）"""
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(base_dir)
    return os.path.commonpath([real_base, real_path]) == real_base

def is_local_ip(ip: str) -> bool:
    """检查是否为本地 IP"""
    from ipaddress import ip_address
    try:
        addr = ip_address(ip)
        return addr.is_loopback or addr.is_private
    except:
        return False

def validate_task_id(task_id: str) -> bool:
    """验证任务 ID 格式"""
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', task_id))
```

---

## 阶段 2: 监控器模块化（已完成示例）

### 2.1 下载看门狗

✅ **已创建**: `src/download/watchdog.py`

**特性**：
- 独立的 `DownloadWatchdog` 类
- 回调函数注入（解耦合）
- 完整的日志和错误处理

**集成到 app.py**：
```python
from src.download.watchdog import DownloadWatchdog

# 创建实例
watchdog = DownloadWatchdog(
    check_interval=60,
    stall_timeout=300,
    get_tasks_callback=lambda: download_tasks,
    restart_task_callback=restart_task_handler
)

# 启动
watchdog.start()
```

### 2.2 Telegram 健康检查器

**创建文件**: `src/telegram/health_checker.py`

```python
"""Telegram 连接健康检查"""
import asyncio
import threading
import logging
from typing import Optional, Callable

logger = logging.getLogger("tg_downloader.health_checker")

class TelegramHealthChecker:
    def __init__(
        self,
        client,
        loop,
        check_interval: int = 120,
        max_retry: int = 3,
        on_failure_callback: Optional[Callable] = None
    ):
        self.client = client
        self.loop = loop
        self.check_interval = check_interval
        self.max_retry = max_retry
        self.on_failure_callback = on_failure_callback
        
        self._running = False
        self._thread = None
        self._failure_count = 0

    def start(self):
        """启动健康检查"""
        if self._running:
            return
        
        self._running = True
        self._thread = threading.Thread(target=self._check_loop, daemon=True)
        self._thread.start()
        logger.info(f"Telegram 健康检查已启动 (间隔: {self.check_interval}s)")

    # ... 其他方法
```

---

## 阶段 3: Telegram 客户端模块

### 3.1 客户端管理器

**创建文件**: `src/telegram/client.py`

**职责**：
- Telegram 客户端初始化和连接
- 连接状态管理
- 重连逻辑
- 异步操作封装

```python
"""Telegram 客户端管理"""
import asyncio
from telethon import TelegramClient
from typing import Optional, Callable, Any

class TelegramClientManager:
    def __init__(self, api_id, api_hash, session_name, proxy=None):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.proxy = proxy
        
        self.loop = asyncio.new_event_loop()
        self.client = TelegramClient(
            session_name, 
            api_id, 
            api_hash, 
            loop=self.loop, 
            proxy=proxy
        )
        
        self.connected = False
        self.user_info = None

    async def connect(self):
        """连接到 Telegram"""
        await self.client.connect()
        
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram 未登录")
        
        me = await self.client.get_me()
        self.user_info = self._format_user(me)
        self.connected = True
        
        return self.user_info

    def run_async(self, coro_factory, timeout=600):
        """在客户端的 event loop 中运行协程"""
        future = asyncio.run_coroutine_threadsafe(
            coro_factory(), 
            self.loop
        )
        return future.result(timeout=timeout)

    # ... 其他方法
```

### 3.2 消息处理器

**创建文件**: `src/telegram/message_handler.py`

**职责**：
- 获取对话列表
- 搜索消息
- 获取视频信息
- 缩略图处理

---

## 阶段 4: 下载管理模块

### 4.1 下载管理器

**创建文件**: `src/download/manager.py`

**职责**：
- 统一下载接口
- 进度跟踪
- 错误处理
- 取消机制

```python
"""下载管理器"""
from typing import Dict, Any, Optional, Callable
import logging

logger = logging.getLogger("tg_downloader.download_manager")

class DownloadManager:
    def __init__(self):
        self.active_downloads: Dict[str, Any] = {}
        self.cancelled_downloads: set = set()
        
    def start_download(
        self, 
        task_id: str, 
        method: str,
        progress_callback: Optional[Callable] = None,
        **kwargs
    ):
        """
        启动下载
        
        Args:
            task_id: 任务 ID
            method: 下载方法 (telethon/aria2/tdl)
            progress_callback: 进度回调
            **kwargs: 下载参数
        """
        if method == "telethon":
            return self._download_telethon(task_id, progress_callback, **kwargs)
        elif method == "aria2":
            return self._download_aria2(task_id, **kwargs)
        elif method == "tdl":
            return self._download_tdl(task_id, **kwargs)
        else:
            raise ValueError(f"未知下载方法: {method}")

    def cancel_download(self, task_id: str):
        """取消下载"""
        self.cancelled_downloads.add(task_id)
        logger.info(f"[{task_id}] 已标记取消")

    def is_cancelled(self, task_id: str) -> bool:
        """检查是否已取消"""
        return task_id in self.cancelled_downloads

    # ... 实现各种下载方法
```

### 4.2 队列处理器

**创建文件**: `src/download/queue.py`

**职责**：
- 队列管理
- 任务调度
- 并发控制

```python
"""下载队列管理"""
import queue
import threading
from typing import Dict, Any, Optional

class DownloadQueue:
    def __init__(self, max_concurrent: int = 3):
        self.queue = queue.Queue()
        self.max_concurrent = max_concurrent
        self.active_count = 0
        self.lock = threading.Lock()
        
    def add_task(self, task: Dict[str, Any]):
        """添加任务到队列"""
        self.queue.put(task)
        
    def get_next_task(self) -> Optional[Dict[str, Any]]:
        """获取下一个任务"""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None
            
    def get_status(self) -> Dict[str, Any]:
        """获取队列状态"""
        return {
            "queue_length": self.queue.qsize(),
            "active_downloads": self.active_count,
            "max_concurrent": self.max_concurrent
        }
```

### 4.3 下载方法实现

**创建文件**: `src/download/methods.py`

**职责**：
- Telethon 直接下载
- aria2 下载
- tdl 下载

---

## 阶段 5: 路由模块化

### 5.1 路由拆分策略

按功能域拆分路由：

**文件**: `src/routes/telegram_api.py`
- `/api/dialogs` - 对话列表
- `/api/videos` - 视频列表
- `/api/search` - 搜索
- `/api/thumb/<msg_id>` - 缩略图

**文件**: `src/routes/download_api.py`
- `/api/download` - 开始下载
- `/api/cancel` - 取消下载
- `/api/retry` - 重试下载
- `/api/download_status` - 下载状态

**文件**: `src/routes/admin_api.py`
- `/api/status` - 系统状态
- `/api/health` - 健康检查
- `/api/history` - 历史记录
- `/api/recovery_candidates` - 恢复候选

**文件**: `src/routes/file_api.py`
- `/api/file/<path>` - 文件下载
- `/api/stream/<path>` - 视频流
- `/relay/<entity_id>/<msg_id>` - Relay 端点

### 5.2 路由注册

**主 app.py** 变为：

```python
from flask import Flask
from src.routes import telegram_api, download_api, admin_api, file_api

app = Flask(__name__)

# 注册蓝图
app.register_blueprint(telegram_api.bp, url_prefix='/api')
app.register_blueprint(download_api.bp, url_prefix='/api')
app.register_blueprint(admin_api.bp, url_prefix='/api')
app.register_blueprint(file_api.bp)

# 全局中间件
@app.before_request
def check_auth():
    # 认证逻辑
    pass

if __name__ == "__main__":
    # 初始化组件
    telegram_client = init_telegram()
    download_manager = init_download_manager()
    
    # 启动监控
    watchdog.start()
    health_checker.start()
    
    # 运行应用
    app.run(host=HOST, port=PORT)
```

---

## 阶段 6: 数据模型

### 6.1 任务模型

**创建文件**: `src/models/task.py`

```python
"""任务数据模型"""
from dataclasses import dataclass, field
from typing import Optional, Dict, Any
from datetime import datetime

@dataclass
class DownloadTask:
    task_id: str
    entity_id: int
    message_id: int
    file_name: str
    total_bytes: int
    
    status: str = "pending"
    progress: int = 0
    downloaded_bytes: int = 0
    speed_bps: float = 0.0
    
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    
    error: Optional[str] = None
    method: str = "telethon"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "task_id": self.task_id,
            "entity_id": self.entity_id,
            "message_id": self.message_id,
            "file_name": self.file_name,
            "total_bytes": self.total_bytes,
            "status": self.status,
            "progress": self.progress,
            "downloaded_bytes": self.downloaded_bytes,
            "speed_bps": self.speed_bps,
            "error": self.error,
            "method": self.method
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DownloadTask":
        """从字典创建"""
        return cls(**{k: v for k, v in data.items() if k in cls.__annotations__})
```

---

## 实施计划

### 第 1 周：基础设施
- [ ] 创建目录结构
- [ ] 提取工具函数 (`utils/`)
- [ ] 编写单元测试
- [ ] 更新导入路径

### 第 2 周：监控模块
- [ ] 迁移 DownloadWatchdog
- [ ] 迁移 TelegramHealthChecker
- [ ] 集成测试
- [ ] 验证功能正常

### 第 3 周：Telegram 模块
- [ ] 创建 TelegramClientManager
- [ ] 迁移消息处理逻辑
- [ ] 迁移缩略图处理
- [ ] 集成测试

### 第 4 周：下载模块
- [ ] 创建 DownloadManager
- [ ] 迁移下载方法
- [ ] 创建 DownloadQueue
- [ ] 集成测试

### 第 5-6 周：路由模块
- [ ] 拆分路由为 Blueprint
- [ ] 迁移中间件和认证
- [ ] 测试所有 API 端点
- [ ] 性能测试

### 第 7 周：数据模型和状态管理
- [ ] 创建数据模型类
- [ ] 重构状态管理
- [ ] 集成测试
- [ ] 文档更新

### 第 8 周：清理和优化
- [ ] 移除旧代码
- [ ] 代码审查
- [ ] 性能优化
- [ ] 完整测试

---

## 迁移检查清单

每个模块迁移后需确认：

- [ ] 所有功能正常工作
- [ ] 单元测试通过
- [ ] 集成测试通过
- [ ] 性能无退化
- [ ] 日志正常输出
- [ ] 错误处理完整
- [ ] 文档已更新

---

## 风险和缓解

### 风险 1：破坏现有功能
**缓解**：
- 每个阶段都保持 app.py 可运行
- 完整的测试覆盖
- 渐进式迁移，而非一次性重写

### 风险 2：性能下降
**缓解**：
- 性能基准测试
- 持续监控关键指标
- 必要时回滚

### 风险 3：导入循环依赖
**缓解**：
- 清晰的模块层次
- 使用依赖注入
- 避免模块间直接引用全局变量

---

## 后续优化

完成模块化后，可继续：

1. **异步架构重构**
   - 迁移到 Quart（异步 Flask）
   - 统一 asyncio 事件循环
   
2. **WebSocket 集成**
   - 实时进度推送
   - 替代轮询机制

3. **缓存优化**
   - Redis 集成
   - 热点数据缓存

---

## 参考资料

- Flask Blueprint 文档
- Python 模块化最佳实践
- 依赖注入模式
- 测试驱动重构

---

**创建日期**: 2026-06-21  
**目标完成**: 8 周  
**维护者**: 开发团队
