"""
核心辅助函数
从 app.py 提取的通用辅助函数
"""
import os
import time
import hashlib


def make_task_id(entity_id, msg_id):
    """
    生成任务 ID

    Args:
        entity_id: 实体 ID
        msg_id: 消息 ID

    Returns:
        任务 ID 字符串
    """
    if entity_id is None or msg_id is None:
        return None
    return f"{entity_id}_{msg_id}"


def make_excerpt(text, max_length=100):
    """
    创建文本摘要

    Args:
        text: 原始文本
        max_length: 最大长度

    Returns:
        摘要文本
    """
    if not text:
        return ""

    text = text.strip()
    if len(text) <= max_length:
        return text

    return text[:max_length] + "..."


def sanitize_filename(filename):
    """
    清理文件名中的非法字符

    Args:
        filename: 原始文件名

    Returns:
        清理后的文件名
    """
    import re

    # 移除或替换非法字符
    illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'
    sanitized = re.sub(illegal_chars, '_', filename)

    # 移除前后空白和点
    sanitized = sanitized.strip('. ')

    # 避免空字符串
    if not sanitized:
        sanitized = "unnamed"

    return sanitized


def resolve_download_path(*parts, must_exist=False, base_dir=None):
    """
    解析下载路径（安全）

    Args:
        *parts: 路径组件
        must_exist: 是否必须存在
        base_dir: 基础目录

    Returns:
        解析后的完整路径

    Raises:
        ValueError: 路径不安全
        FileNotFoundError: 路径不存在（当 must_exist=True 时）
    """
    if base_dir is None:
        from config import DOWNLOAD_DIR
        base_dir = DOWNLOAD_DIR

    # 构建路径
    path = os.path.join(base_dir, *parts)

    # 安全检查：防止目录遍历
    real_path = os.path.realpath(path)
    real_base = os.path.realpath(base_dir)

    if os.path.commonpath([real_base, real_path]) != real_base:
        raise ValueError("非法路径：目录遍历")

    if must_exist and not os.path.exists(real_path):
        raise FileNotFoundError(f"路径不存在: {real_path}")

    return real_path


def format_user_display(user):
    """
    格式化用户显示信息

    Args:
        user: Telegram User 对象

    Returns:
        格式化的用户信息字符串
    """
    if not user:
        return "Unknown"

    parts = []

    # 名字
    if hasattr(user, 'first_name') and user.first_name:
        parts.append(user.first_name)

    if hasattr(user, 'last_name') and user.last_name:
        parts.append(user.last_name)

    # 用户名
    if hasattr(user, 'username') and user.username:
        parts.append(f"@{user.username}")

    # ID
    if hasattr(user, 'id'):
        parts.append(f"(ID: {user.id})")

    return " ".join(parts) if parts else "Unknown"


def message_text(message):
    """
    提取消息文本

    Args:
        message: Telegram Message 对象

    Returns:
        消息文本
    """
    if not message:
        return ""

    if hasattr(message, 'text') and message.text:
        return message.text

    if hasattr(message, 'message') and message.message:
        return message.message

    return ""


def calc_download_timeout(file_size_bytes):
    """
    根据文件大小计算下载超时时间

    Args:
        file_size_bytes: 文件大小（字节）

    Returns:
        超时时间（秒）
    """
    if not file_size_bytes or file_size_bytes <= 0:
        return 1800  # 默认 30 分钟

    # 按 100KB/s 最低速率估算，加 5 分钟余量
    seconds = max(600, int(file_size_bytes / (100 * 1024)) + 300)

    # 上限 12 小时
    return min(seconds, 43200)


def request_ip_is_local():
    """
    检查请求是否来自本地

    Returns:
        是否为本地请求
    """
    from flask import request
    from src.utils import is_local_ip

    remote_addr = request.remote_addr

    if not remote_addr:
        return False

    return is_local_ip(remote_addr)


def is_local_bind_only():
    """
    检查是否仅绑定本地

    Returns:
        是否仅本地绑定
    """
    from config import WEB_BIND_HOST

    return WEB_BIND_HOST in ("127.0.0.1", "localhost", "::1")


def abort_if_debug_disabled():
    """
    如果调试功能未启用，返回错误响应

    Returns:
        Flask Response 或 None
    """
    from flask import jsonify
    from config import DEBUG_API_ENABLED

    if not DEBUG_API_ENABLED:
        return jsonify({"error": "调试 API 未启用"}), 403

    return None


def require_web_auth():
    """
    检查 Web 认证

    Returns:
        Flask Response 或 None
    """
    from flask import request, jsonify
    from config import WEB_AUTH_USERNAME, WEB_AUTH_PASSWORD

    # 如果未配置认证，直接通过
    if not WEB_AUTH_USERNAME or not WEB_AUTH_PASSWORD:
        return None

    # 检查 Basic Auth
    auth = request.authorization

    if not auth or auth.username != WEB_AUTH_USERNAME or auth.password != WEB_AUTH_PASSWORD:
        return jsonify({"error": "需要认证"}), 401

    return None
