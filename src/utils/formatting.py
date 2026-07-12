"""
格式化工具函数
"""
import re
from typing import Optional


def format_size(size_bytes: int) -> str:
    """
    格式化文件大小

    Args:
        size_bytes: 字节数

    Returns:
        格式化后的字符串，如 "1.5GB"
    """
    if size_bytes < 0:
        return "0B"

    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size_bytes)
    unit_index = 0

    while size >= 1024.0 and unit_index < len(units) - 1:
        size /= 1024.0
        unit_index += 1

    if unit_index == 0:
        return f"{int(size)}{units[unit_index]}"
    else:
        return f"{size:.2f}{units[unit_index]}"


def format_speed(speed_bps: float) -> str:
    """
    格式化速度

    Args:
        speed_bps: 速度（字节/秒）

    Returns:
        格式化后的字符串，如 "1.5MB/s"
    """
    return format_size(int(speed_bps)) + "/s"


def format_time(seconds: float) -> str:
    """
    格式化时间

    Args:
        seconds: 秒数

    Returns:
        格式化后的字符串，如 "1h 30m 45s"
    """
    if seconds < 0:
        return "0s"

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def make_excerpt(text: Optional[str], limit: int = 180) -> str:
    """
    生成文本摘要

    Args:
        text: 原始文本
        limit: 最大长度

    Returns:
        截断后的文本
    """
    if not text:
        return ""

    # 移除多余的空白
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) <= limit:
        return text

    return text[:limit] + "..."


def sanitize_filename(filename: str) -> str:
    """
    清理文件名中的非法字符

    Args:
        filename: 原始文件名

    Returns:
        清理后的文件名
    """
    # 移除或替换非法字符
    illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'
    sanitized = re.sub(illegal_chars, '_', filename)

    # 移除前后空白
    sanitized = sanitized.strip()

    # 避免空文件名
    if not sanitized:
        sanitized = "unnamed"

    return sanitized


def format_user_display(user) -> str:
    """
    格式化用户显示信息

    Args:
        user: Telegram 用户对象

    Returns:
        格式化后的用户信息
    """
    parts = []

    if hasattr(user, 'first_name') and user.first_name:
        parts.append(user.first_name)

    if hasattr(user, 'last_name') and user.last_name:
        parts.append(user.last_name)

    name = " ".join(parts) if parts else "Unknown"

    if hasattr(user, 'username') and user.username:
        return f"{name} (@{user.username})"
    elif hasattr(user, 'id') and user.id:
        return f"{name} (ID: {user.id})"
    else:
        return name


def parse_message_text(message) -> str:
    """
    提取消息文本内容

    Args:
        message: Telegram 消息对象

    Returns:
        消息文本
    """
    if not message:
        return ""

    text = ""

    if hasattr(message, 'message') and message.message:
        text = message.message
    elif hasattr(message, 'text') and message.text:
        text = message.text

    return text.strip()
