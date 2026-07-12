# WebSocket 实时更新实现指南

## 概述

本文档提供使用 WebSocket 替代轮询，实现下载进度实时推送的完整方案。

---

## 当前问题：轮询机制

### 前端轮询代码
```javascript
// 每秒轮询一次
setInterval(async () => {
    const response = await fetch('/api/download_status');
    const data = await response.json();
    updateUI(data);
}, 1000);
```

### 问题
1. **资源浪费**: 无变化时也要发送请求
2. **延迟**: 最多 1 秒的延迟
3. **服务器压力**: 大量无效请求
4. **网络流量**: 持续占用带宽

---

## 目标：WebSocket 推送

### 架构对比

**轮询模式**:
```
客户端 ─────────→ 服务器   (每秒请求)
       ←─────────           (返回状态)
       ─────────→           (再次请求)
       ←─────────           (返回状态)
```

**WebSocket 模式**:
```
客户端 ═════════╗           (建立连接)
               ║
服务器 ════════╝
       ←─────────           (仅在变化时推送)
       ←─────────           (进度更新)
       ←─────────           (完成通知)
```

---

## 技术选型

### Socket.IO vs 原生 WebSocket

| 特性 | Socket.IO | 原生 WebSocket |
|------|-----------|----------------|
| 浏览器兼容 | 优秀（自动降级） | 良好 |
| 断线重连 | 自动 | 需手动实现 |
| 房间/命名空间 | 内置 | 需手动实现 |
| 事件系统 | 是 | 否（需封装） |
| 二进制支持 | 是 | 是 |

**选择**: Socket.IO（更完善的功能）

---

## 阶段 1: 安装依赖

### 后端依赖

**更新 requirements.txt**:
```txt
# WebSocket
python-socketio>=5.10.0
python-engineio>=4.8.0

# 如果使用 Quart
quart-socketio>=0.6.0

# 如果使用 Flask
flask-socketio>=5.3.0
```

### 前端依赖

**HTML 中引入**:
```html
<script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
```

**或 npm 安装**:
```bash
npm install socket.io-client
```

---

## 阶段 2: 后端实现（Quart 版本）

### 2.1 WebSocket 服务器设置

**创建文件**: `src/websocket/manager.py`

```python
"""
WebSocket 管理器
"""
import asyncio
import logging
from typing import Dict, Any, Set
from quart import Quart
from quart_socketio import SocketIO

logger = logging.getLogger("tg_downloader.websocket")

class WebSocketManager:
    """WebSocket 连接和消息管理"""
    
    def __init__(self, app: Quart):
        self.app = app
        self.socketio = SocketIO(app, cors_allowed_origins="*")
        
        # 连接管理
        self.connected_clients: Set[str] = set()
        self.task_subscribers: Dict[str, Set[str]] = {}  # task_id -> {sid, sid, ...}
        
        # 注册事件处理器
        self._register_handlers()
    
    def _register_handlers(self):
        """注册 Socket.IO 事件处理器"""
        
        @self.socketio.on('connect')
        async def handle_connect():
            """客户端连接"""
            from flask_socketio import request
            sid = request.sid
            self.connected_clients.add(sid)
            logger.info(f"客户端连接: {sid} (总计: {len(self.connected_clients)})")
            
            await self.socketio.emit('connected', {
                'message': '连接成功',
                'sid': sid
            }, room=sid)
        
        @self.socketio.on('disconnect')
        async def handle_disconnect():
            """客户端断开"""
            from flask_socketio import request
            sid = request.sid
            
            # 移除订阅
            for task_id, subscribers in list(self.task_subscribers.items()):
                if sid in subscribers:
                    subscribers.remove(sid)
                    if not subscribers:
                        del self.task_subscribers[task_id]
            
            self.connected_clients.discard(sid)
            logger.info(f"客户端断开: {sid} (剩余: {len(self.connected_clients)})")
        
        @self.socketio.on('subscribe_task')
        async def handle_subscribe(data):
            """订阅任务进度"""
            from flask_socketio import request
            sid = request.sid
            task_id = data.get('task_id')
            
            if not task_id:
                await self.socketio.emit('error', {
                    'message': '缺少 task_id'
                }, room=sid)
                return
            
            if task_id not in self.task_subscribers:
                self.task_subscribers[task_id] = set()
            
            self.task_subscribers[task_id].add(sid)
            logger.info(f"客户端 {sid} 订阅任务 {task_id}")
            
            await self.socketio.emit('subscribed', {
                'task_id': task_id
            }, room=sid)
        
        @self.socketio.on('unsubscribe_task')
        async def handle_unsubscribe(data):
            """取消订阅任务"""
            from flask_socketio import request
            sid = request.sid
            task_id = data.get('task_id')
            
            if task_id and task_id in self.task_subscribers:
                self.task_subscribers[task_id].discard(sid)
                logger.info(f"客户端 {sid} 取消订阅任务 {task_id}")
    
    async def emit_progress(self, task_id: str, data: Dict[str, Any]):
        """
        发送进度更新到订阅者
        
        Args:
            task_id: 任务 ID
            data: 进度数据
        """
        subscribers = self.task_subscribers.get(task_id, set())
        
        if not subscribers:
            return
        
        await self.socketio.emit('progress', {
            'task_id': task_id,
            **data
        }, room=list(subscribers))
        
        logger.debug(f"[{task_id}] 推送进度到 {len(subscribers)} 个客户端")
    
    async def emit_task_status(self, task_id: str, status: str, **kwargs):
        """
        发送任务状态变更
        
        Args:
            task_id: 任务 ID
            status: 新状态 (queued/downloading/completed/failed/cancelled)
            **kwargs: 额外数据
        """
        subscribers = self.task_subscribers.get(task_id, set())
        
        if not subscribers:
            return
        
        await self.socketio.emit('task_status', {
            'task_id': task_id,
            'status': status,
            **kwargs
        }, room=list(subscribers))
        
        logger.info(f"[{task_id}] 状态变更: {status}")
    
    async def broadcast(self, event: str, data: Dict[str, Any]):
        """
        广播消息到所有连接的客户端
        
        Args:
            event: 事件名称
            data: 数据
        """
        await self.socketio.emit(event, data)
        logger.debug(f"广播事件: {event} 到 {len(self.connected_clients)} 个客户端")
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'connected_clients': len(self.connected_clients),
            'subscribed_tasks': len(self.task_subscribers),
            'total_subscriptions': sum(len(subs) for subs in self.task_subscribers.values())
        }
```

### 2.2 集成到主应用

**修改主应用入口**（注：本指南原基于已移除的 Quart 实验入口 `app_async.py` 编写，实施时需适配当前 Flask 入口 `app_new.py`，参见 Task.md 关于全异步架构的说明）:

```python
from src.websocket.manager import WebSocketManager

# 创建 WebSocket 管理器
ws_manager = None

@app.before_serving
async def startup():
    global ws_manager
    
    # 初始化其他组件...
    
    # 初始化 WebSocket
    ws_manager = WebSocketManager(app)
    logger.info("WebSocket 管理器已初始化")

# 在下载进度回调中使用
async def update_progress(task_id, downloaded, total):
    """更新进度并推送"""
    progress = int(downloaded * 100 / total) if total > 0 else 0
    speed = calculate_speed(task_id, downloaded)  # 计算速度
    
    # 推送到 WebSocket
    await ws_manager.emit_progress(task_id, {
        'progress': progress,
        'downloaded': downloaded,
        'total': total,
        'speed': speed,
        'speed_formatted': format_speed(speed)
    })

async def handle_download(task):
    """处理下载任务"""
    task_id = task["task_id"]
    
    # 通知开始下载
    await ws_manager.emit_task_status(task_id, 'downloading')
    
    try:
        # 执行下载...
        success = await download_manager.download_with_resume(
            task_id=task_id,
            message=message,
            file_path=file_path,
            progress_callback=update_progress
        )
        
        if success:
            await ws_manager.emit_task_status(task_id, 'completed', 
                file_path=file_path)
        else:
            await ws_manager.emit_task_status(task_id, 'failed', 
                error='下载未完成')
            
    except Exception as e:
        await ws_manager.emit_task_status(task_id, 'failed', 
            error=str(e))
```

### 2.3 添加 WebSocket 状态端点

```python
@app.route("/api/websocket/stats")
async def websocket_stats():
    """获取 WebSocket 统计信息"""
    return jsonify(ws_manager.get_stats() if ws_manager else {})
```

---

## 阶段 3: 前端实现

### 3.1 WebSocket 客户端类

**创建文件**: `static/js/websocket_client.js`

```javascript
/**
 * WebSocket 客户端管理
 */
class WebSocketClient {
    constructor(serverUrl) {
        this.serverUrl = serverUrl || window.location.origin;
        this.socket = null;
        this.connected = false;
        this.subscribedTasks = new Set();
        
        // 事件回调
        this.onConnect = null;
        this.onDisconnect = null;
        this.onProgress = null;
        this.onTaskStatus = null;
        this.onError = null;
    }
    
    /**
     * 连接到服务器
     */
    connect() {
        console.log('正在连接 WebSocket...');
        
        this.socket = io(this.serverUrl, {
            transports: ['websocket', 'polling'],
            reconnection: true,
            reconnectionDelay: 1000,
            reconnectionAttempts: 10
        });
        
        // 连接事件
        this.socket.on('connect', () => {
            console.log('WebSocket 已连接');
            this.connected = true;
            
            if (this.onConnect) {
                this.onConnect();
            }
            
            // 重新订阅任务
            this.subscribedTasks.forEach(taskId => {
                this._subscribe(taskId);
            });
        });
        
        // 断开事件
        this.socket.on('disconnect', (reason) => {
            console.log('WebSocket 断开:', reason);
            this.connected = false;
            
            if (this.onDisconnect) {
                this.onDisconnect(reason);
            }
        });
        
        // 进度更新
        this.socket.on('progress', (data) => {
            console.log('收到进度更新:', data);
            
            if (this.onProgress) {
                this.onProgress(data);
            }
        });
        
        // 任务状态变更
        this.socket.on('task_status', (data) => {
            console.log('任务状态变更:', data);
            
            if (this.onTaskStatus) {
                this.onTaskStatus(data);
            }
        });
        
        // 错误处理
        this.socket.on('error', (error) => {
            console.error('WebSocket 错误:', error);
            
            if (this.onError) {
                this.onError(error);
            }
        });
        
        // 连接确认
        this.socket.on('connected', (data) => {
            console.log('连接确认:', data);
        });
        
        // 订阅确认
        this.socket.on('subscribed', (data) => {
            console.log('订阅确认:', data.task_id);
        });
    }
    
    /**
     * 断开连接
     */
    disconnect() {
        if (this.socket) {
            this.socket.disconnect();
            this.socket = null;
            this.connected = false;
        }
    }
    
    /**
     * 订阅任务进度
     */
    subscribeTask(taskId) {
        this.subscribedTasks.add(taskId);
        
        if (this.connected) {
            this._subscribe(taskId);
        }
    }
    
    /**
     * 取消订阅任务
     */
    unsubscribeTask(taskId) {
        this.subscribedTasks.delete(taskId);
        
        if (this.connected && this.socket) {
            this.socket.emit('unsubscribe_task', { task_id: taskId });
        }
    }
    
    /**
     * 内部订阅方法
     */
    _subscribe(taskId) {
        if (this.socket) {
            this.socket.emit('subscribe_task', { task_id: taskId });
        }
    }
    
    /**
     * 获取连接状态
     */
    isConnected() {
        return this.connected;
    }
}

// 全局实例
let wsClient = null;

/**
 * 初始化 WebSocket 客户端
 */
function initWebSocket() {
    wsClient = new WebSocketClient();
    
    // 设置回调
    wsClient.onConnect = () => {
        updateConnectionStatus(true);
    };
    
    wsClient.onDisconnect = () => {
        updateConnectionStatus(false);
    };
    
    wsClient.onProgress = (data) => {
        updateTaskProgress(data);
    };
    
    wsClient.onTaskStatus = (data) => {
        updateTaskStatus(data);
    };
    
    wsClient.onError = (error) => {
        console.error('WebSocket 错误:', error);
        showNotification('WebSocket 错误: ' + error.message, 'error');
    };
    
    // 连接
    wsClient.connect();
}

/**
 * 更新连接状态显示
 */
function updateConnectionStatus(connected) {
    const indicator = document.getElementById('ws-status');
    if (indicator) {
        indicator.className = connected ? 'connected' : 'disconnected';
        indicator.title = connected ? 'WebSocket 已连接' : 'WebSocket 断开';
    }
}

/**
 * 更新任务进度
 */
function updateTaskProgress(data) {
    const taskId = data.task_id;
    const progressBar = document.querySelector(`#task-${taskId} .progress-bar`);
    const progressText = document.querySelector(`#task-${taskId} .progress-text`);
    const speedText = document.querySelector(`#task-${taskId} .speed-text`);
    
    if (progressBar) {
        progressBar.style.width = data.progress + '%';
    }
    
    if (progressText) {
        progressText.textContent = data.progress + '%';
    }
    
    if (speedText && data.speed_formatted) {
        speedText.textContent = data.speed_formatted;
    }
}

/**
 * 更新任务状态
 */
function updateTaskStatus(data) {
    const taskId = data.task_id;
    const statusBadge = document.querySelector(`#task-${taskId} .status-badge`);
    
    if (statusBadge) {
        statusBadge.textContent = getStatusText(data.status);
        statusBadge.className = `status-badge status-${data.status}`;
    }
    
    // 根据状态执行特殊操作
    switch (data.status) {
        case 'completed':
            showNotification(`下载完成: ${data.file_path || taskId}`, 'success');
            wsClient.unsubscribeTask(taskId);
            break;
            
        case 'failed':
            showNotification(`下载失败: ${data.error || '未知错误'}`, 'error');
            wsClient.unsubscribeTask(taskId);
            break;
            
        case 'cancelled':
            showNotification(`下载已取消: ${taskId}`, 'info');
            wsClient.unsubscribeTask(taskId);
            break;
    }
}

/**
 * 获取状态文本
 */
function getStatusText(status) {
    const statusMap = {
        'queued': '排队中',
        'downloading': '下载中',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return statusMap[status] || status;
}

/**
 * 显示通知
 */
function showNotification(message, type = 'info') {
    // 实现通知显示逻辑
    console.log(`[${type.toUpperCase()}] ${message}`);
}

// 页面加载时初始化
document.addEventListener('DOMContentLoaded', () => {
    initWebSocket();
});

// 页面卸载时断开连接
window.addEventListener('beforeunload', () => {
    if (wsClient) {
        wsClient.disconnect();
    }
});
```

### 3.2 使用示例

**在主页面中使用**:

```html
<!DOCTYPE html>
<html>
<head>
    <title>Telegram 视频下载器</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script src="/static/js/websocket_client.js"></script>
    <style>
        .ws-status-indicator {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            display: inline-block;
            margin-left: 10px;
        }
        .ws-status-indicator.connected {
            background-color: #4CAF50;
        }
        .ws-status-indicator.disconnected {
            background-color: #f44336;
        }
        .progress-bar {
            height: 20px;
            background-color: #4CAF50;
            transition: width 0.3s ease;
        }
    </style>
</head>
<body>
    <h1>
        Telegram 视频下载器
        <span id="ws-status" class="ws-status-indicator disconnected"></span>
    </h1>
    
    <div id="downloads">
        <!-- 下载任务列表 -->
    </div>
    
    <script>
        // 开始下载时订阅
        async function startDownload(entityId, messageId, fileName) {
            const response = await fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ entity_id: entityId, message_id: messageId, file_name: fileName })
            });
            
            const data = await response.json();
            const taskId = data.task_id;
            
            // 添加任务到 UI
            addTaskToUI(taskId, fileName);
            
            // 订阅进度更新
            if (wsClient && wsClient.isConnected()) {
                wsClient.subscribeTask(taskId);
            }
        }
        
        function addTaskToUI(taskId, fileName) {
            const html = `
                <div id="task-${taskId}" class="task-item">
                    <div class="task-name">${fileName}</div>
                    <div class="progress-container">
                        <div class="progress-bar" style="width: 0%"></div>
                    </div>
                    <div class="task-info">
                        <span class="progress-text">0%</span>
                        <span class="speed-text">--</span>
                        <span class="status-badge status-queued">排队中</span>
                    </div>
                </div>
            `;
            
            document.getElementById('downloads').insertAdjacentHTML('beforeend', html);
        }
    </script>
</body>
</html>
```

---

## 阶段 4: 性能优化

### 4.1 进度推送节流

避免过于频繁的推送：

```python
class ProgressThrottler:
    """进度推送节流器"""
    
    def __init__(self, min_interval: float = 0.5):
        self.min_interval = min_interval
        self.last_push = {}
    
    async def should_push(self, task_id: str) -> bool:
        """判断是否应该推送"""
        current_time = asyncio.get_event_loop().time()
        last_time = self.last_push.get(task_id, 0)
        
        if current_time - last_time >= self.min_interval:
            self.last_push[task_id] = current_time
            return True
        
        return False

# 使用
throttler = ProgressThrottler(min_interval=0.5)  # 最多每 0.5 秒推送一次

async def update_progress(task_id, downloaded, total):
    if await throttler.should_push(task_id):
        await ws_manager.emit_progress(task_id, {
            'progress': int(downloaded * 100 / total),
            'downloaded': downloaded,
            'total': total
        })
```

### 4.2 批量推送

将多个更新合并为一次推送：

```python
class BatchPusher:
    """批量推送器"""
    
    def __init__(self, batch_size: int = 10, batch_interval: float = 1.0):
        self.batch_size = batch_size
        self.batch_interval = batch_interval
        self.pending_updates = []
        self._task = None
    
    async def start(self):
        """启动批量推送任务"""
        self._task = asyncio.create_task(self._push_loop())
    
    async def stop(self):
        """停止批量推送任务"""
        if self._task:
            self._task.cancel()
    
    async def add_update(self, task_id: str, data: dict):
        """添加更新到批次"""
        self.pending_updates.append({'task_id': task_id, **data})
        
        if len(self.pending_updates) >= self.batch_size:
            await self._flush()
    
    async def _push_loop(self):
        """定时推送循环"""
        while True:
            await asyncio.sleep(self.batch_interval)
            await self._flush()
    
    async def _flush(self):
        """推送所有待处理更新"""
        if not self.pending_updates:
            return
        
        updates = self.pending_updates[:]
        self.pending_updates.clear()
        
        # 推送批量更新
        await ws_manager.broadcast('batch_progress', {'updates': updates})
```

---

## 阶段 5: 测试

### 5.1 后端测试

```python
import pytest
from src.websocket.manager import WebSocketManager

@pytest.mark.asyncio
async def test_websocket_connection():
    """测试 WebSocket 连接"""
    # 创建测试客户端
    client = app.test_client()
    
    # 连接 WebSocket
    async with client.websocket('/socket.io') as ws:
        # 发送连接消息
        await ws.send_json({'type': 'connect'})
        
        # 接收响应
        response = await ws.receive_json()
        assert response['type'] == 'connected'

@pytest.mark.asyncio
async def test_progress_push():
    """测试进度推送"""
    # 订阅任务
    await ws_manager.emit_progress('task_123', {
        'progress': 50,
        'downloaded': 500000,
        'total': 1000000
    })
    
    # 验证推送
    # ...
```

### 5.2 前端测试

```javascript
// 使用 Jest
describe('WebSocketClient', () => {
    let client;
    
    beforeEach(() => {
        client = new WebSocketClient('http://localhost:5000');
    });
    
    afterEach(() => {
        client.disconnect();
    });
    
    test('should connect successfully', (done) => {
        client.onConnect = () => {
            expect(client.isConnected()).toBe(true);
            done();
        };
        
        client.connect();
    });
    
    test('should receive progress updates', (done) => {
        client.onProgress = (data) => {
            expect(data.task_id).toBe('task_123');
            expect(data.progress).toBe(50);
            done();
        };
        
        client.connect();
        client.subscribeTask('task_123');
    });
});
```

---

## 对比：轮询 vs WebSocket

| 指标 | 轮询 | WebSocket |
|------|------|-----------|
| 延迟 | 0.5-1秒 | <100ms |
| 服务器请求数 | 60次/分钟 | 1次（连接） |
| 网络流量 | 高 | 低 |
| 服务器负载 | 高 | 低 |
| 实时性 | 中 | 优秀 |
| 实现复杂度 | 简单 | 中等 |

---

## 部署注意事项

### Nginx 配置

```nginx
location /socket.io {
    proxy_pass http://localhost:5000;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    
    # 超时设置
    proxy_connect_timeout 7d;
    proxy_send_timeout 7d;
    proxy_read_timeout 7d;
}
```

### Docker 端口暴露

```yaml
# docker-compose.yml
services:
  tg-downloader:
    ports:
      - "5000:5000"  # HTTP + WebSocket
```

---

**创建日期**: 2026-06-21  
**预计耗时**: 1-2 周  
**前置条件**: 完成异步架构重构
