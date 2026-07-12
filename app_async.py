#!/usr/bin/env python3
"""
Telegram 视频下载器 - Quart 异步版本
使用 Quart + WebSocket 实现实时进度推送
"""

import os
import sys
import asyncio
import logging
from logging.handlers import RotatingFileHandler

from quart import Quart, render_template, jsonify, request, websocket
from quart_cors import cors

from telethon import TelegramClient
from telethon.sessions import StringSession

# 导入配置
from config import (
    API_ID, API_HASH, SESSION_NAME, PROXY_CONFIG,
    WEB_BIND_HOST, WEB_BIND_PORT, DOWNLOAD_DIR,
    ARIA2_RPC_URL, ARIA2_SECRET, RELAY_TOKEN_SECRET,
    TDL_BINARY
)

# 导入核心模块
from aria2_client import Aria2Client
from src.state.manager import TaskStateManager
from src.download.queue import DownloadQueue
from src.utils import format_size

# ==================== 日志配置 ====================
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("tg_downloader")
logger.setLevel(logging.INFO)

fh = RotatingFileHandler(
    os.path.join(LOG_DIR, "app_async.log"),
    maxBytes=10*1024*1024,
    backupCount=30,
    encoding="utf-8"
)
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler())

# ==================== Quart 应用 ====================
app = Quart(__name__)

# 手动 CORS（仅 HTTP，不影响 WebSocket）
@app.after_request
async def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return response

# ==================== 全局状态 ====================
# Telegram 客户端
tg_client = None
tg_connected = False
tg_connect_error = ""
tg_user_info = ""

# 核心管理器
state_manager = TaskStateManager()
download_queue = DownloadQueue(max_concurrent=1)

# Aria2 客户端
aria2 = Aria2Client(ARIA2_RPC_URL, ARIA2_SECRET)

# WebSocket 连接管理
websocket_clients = set()

# 缩略图目录
THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)

# ==================== WebSocket 管理 ====================
async def broadcast_progress(data):
    """
    广播进度更新到所有连接的客户端

    Args:
        data: 进度数据字典
    """
    if not websocket_clients:
        return

    disconnected = set()

    for client in websocket_clients:
        try:
            await client.send_json(data)
        except Exception as e:
            logger.error(f"发送 WebSocket 消息失败: {e}")
            disconnected.add(client)

    # 移除断开的连接
    for client in disconnected:
        websocket_clients.discard(client)


@app.websocket('/ws/progress')
async def ws_progress():
    """WebSocket 端点：实时进度推送"""
    ws = websocket._get_current_object()
    try:
        websocket_clients.add(ws)
        logger.info(f"WebSocket 客户端连接 (当前: {len(websocket_clients)})")

        # 立即发送当前状态
        await ws.send_json({
            "type": "init",
            "data": state_manager.get_all_states()
        })

        # 保持连接
        while True:
            message = await ws.receive()
            if message == "ping":
                await ws.send("pong")

    except asyncio.CancelledError:
        logger.info("WebSocket 连接取消")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
    finally:
        websocket_clients.discard(ws)
        logger.info(f"WebSocket 客户端断开 (当前: {len(websocket_clients)})")


# ==================== 基础路由 ====================
@app.route('/')
async def index():
    """首页"""
    return await render_template('index.html')


@app.route('/api/status')
async def api_status():
    """系统状态"""
    return jsonify({
        "connected": tg_connected,
        "error": tg_connect_error,
        "user": tg_user_info,
        "queue": download_queue.get_status(),
        "websocket_clients": len(websocket_clients)
    })


@app.route('/api/download_status')
async def api_download_status():
    """下载状态"""
    return jsonify(state_manager.get_all_states())


@app.route('/api/download', methods=['POST'])
async def api_download():
    """开始下载"""
    data = await request.get_json()

    # 简化实现示例
    task_id = f"task_{len(state_manager.get_all_states())}"

    state_manager.set_state(task_id, {
        "task_id": task_id,
        "filename": data.get("filename", "test.mp4"),
        "status": "downloading",
        "progress": 0,
        "downloaded": "0B",
        "total": "100MB",
        "speed": "0B/s",
    })

    # 广播新任务
    await broadcast_progress({
        "type": "task_added",
        "task_id": task_id,
        "data": state_manager.get_state(task_id)
    })

    return jsonify({"task_id": task_id, "status": "submitted"})


# ==================== 进度更新任务 ====================
async def progress_updater():
    """
    后台任务：定期更新进度并广播
    """
    while True:
        try:
            # 获取所有任务状态
            states = state_manager.get_all_states()

            if states and websocket_clients:
                # 广播更新
                await broadcast_progress({
                    "type": "progress_update",
                    "data": states
                })

            await asyncio.sleep(1)  # 每秒更新一次

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"进度更新器错误: {e}")
            await asyncio.sleep(5)


# ==================== Telegram 客户端 ====================
async def init_telegram_client():
    """初始化 Telegram 客户端"""
    global tg_client, tg_connected, tg_connect_error, tg_user_info

    try:
        logger.info("正在连接 Telegram...")

        tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, proxy=PROXY_CONFIG)
        await tg_client.connect()

        if not await tg_client.is_user_authorized():
            tg_connect_error = "Telegram 未登录！请先运行 login.py 完成登录。"
            logger.error(tg_connect_error)
            return False

        me = await tg_client.get_me()
        tg_user_info = f"{me.first_name} {me.last_name or ''} (@{me.username or 'N/A'})"
        tg_connected = True
        tg_connect_error = ""

        logger.info(f"Telegram 已连接: {tg_user_info}")
        return True

    except Exception as e:
        tg_connect_error = f"连接失败: {e}"
        logger.error(tg_connect_error)
        return False


# ==================== 应用启动和关闭 ====================
@app.before_serving
async def startup():
    """应用启动时执行"""
    logger.info("应用启动中...")

    # 初始化 Telegram 客户端
    success = await init_telegram_client()
    if not success:
        logger.warning("Telegram 连接失败，但应用继续运行")

    # 启动进度更新器
    app.add_background_task(progress_updater)
    logger.info("进度更新器已启动")

    logger.info("应用启动完成")


@app.after_serving
async def shutdown():
    """应用关闭时执行"""
    logger.info("应用关闭中...")

    # 关闭 Telegram 客户端
    if tg_client:
        await tg_client.disconnect()
        logger.info("Telegram 客户端已断开")

    logger.info("应用已关闭")


# ==================== 主程序 ====================
if __name__ == "__main__":
    print("=" * 70)
    print("Telegram 视频下载器 - Quart 异步版本")
    print("=" * 70)
    print()
    print("✨ 特性:")
    print("  ✅ Quart 异步框架")
    print("  ✅ WebSocket 实时推送")
    print("  ✅ 原生异步支持")
    print("  ✅ 高性能并发")
    print()
    print(f"启动地址: http://{WEB_BIND_HOST}:{WEB_BIND_PORT}")
    print(f"WebSocket: ws://{WEB_BIND_HOST}:{WEB_BIND_PORT}/ws/progress")
    print("=" * 70)
    print()

    # 启动 Quart 应用
    app.run(
        host=WEB_BIND_HOST,
        port=WEB_BIND_PORT,
        debug=False,
        use_reloader=False
    )
