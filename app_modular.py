#!/usr/bin/env python3
"""
Telegram 视频下载器 - 模块化版本
这是 app.py 的模块化重构版本
"""

# TODO: 这是一个框架文件，展示目标架构
# 完整实现需要将 app.py 中的所有功能逐一迁移

import os
import sys
import logging
from flask import Flask

# 导入配置
from config import *

# 导入新模块
from src.utils import format_size, format_speed, validate_task_id
from src.download import DownloadQueue
from src.state.manager import TaskStateManager
from src.telegram import TelegramHealthChecker

# 初始化 Flask
app = Flask(__name__)

# 全局组件
download_queue = DownloadQueue(max_concurrent=1)
state_manager = TaskStateManager()

# 日志配置
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("tg_downloader")
logger.setLevel(logging.INFO)

# TODO: 配置日志处理器

# ==================== 路由注册 ====================
# 注意：这些路由蓝图还需要实现
# from src.routes.main import bp as main_bp
# from src.routes.telegram import bp as telegram_bp
# from src.routes.download import bp as download_bp

# app.register_blueprint(main_bp)
# app.register_blueprint(telegram_bp, url_prefix='/api')
# app.register_blueprint(download_bp, url_prefix='/api')

# ==================== 临时路由（示例）====================
@app.route("/")
def index():
    return """
    <h1>Telegram 视频下载器 - 模块化版本</h1>
    <p>这是模块化重构版本的框架。</p>
    <p><strong>注意</strong>: 完整功能请使用 <code>python3 app.py</code></p>
    <p>当前状态: 框架已就位，功能迁移进行中...</p>
    <h2>已完成的模块</h2>
    <ul>
        <li>✅ src/utils/ - 工具函数</li>
        <li>✅ src/download/queue.py - 队列管理</li>
        <li>✅ src/state/manager.py - 状态管理</li>
        <li>✅ src/telegram/health_checker.py - 健康检查</li>
    </ul>
    <h2>待迁移的功能</h2>
    <ul>
        <li>⏳ 31 个路由</li>
        <li>⏳ Telegram 客户端封装</li>
        <li>⏳ 下载管理器</li>
        <li>⏳ 其他辅助功能</li>
    </ul>
    """

@app.route("/api/status")
def api_status():
    """系统状态 API"""
    return {
        "version": "modular-preview",
        "status": "framework-ready",
        "message": "模块化架构框架已就位，功能迁移进行中",
        "queue": download_queue.get_status(),
        "state": state_manager.get_stats()
    }


if __name__ == "__main__":
    print("=" * 60)
    print("Telegram 视频下载器 - 模块化版本 (预览)")
    print("=" * 60)
    print()
    print("⚠️  注意: 这是模块化重构的框架版本")
    print("⚠️  完整功能请使用: python3 app.py")
    print()
    print("当前状态:")
    print("  ✅ 核心模块已完成")
    print("  ⏳ 路由迁移进行中")
    print()
    print(f"启动地址: http://{WEB_BIND_HOST}:{WEB_BIND_PORT}")
    print("=" * 60)
    print()

    app.run(
        host=WEB_BIND_HOST,
        port=WEB_BIND_PORT + 1,  # 使用不同端口避免冲突
        debug=False
    )
