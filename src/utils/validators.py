"""
工具函数模块 - 验证器
"""
import os
import re
from ipaddress import ip_address
from typing import Optional


def is_valid_path(path: str, base_dir: str) -> bool:
    """
    验证路径安全性（防目录遍历）

    Args:
        path: 要验证的路径
        base_dir: 基础目录

    Returns:
        路径是否安全
    """
    try:
        real_path = os.path.realpath(path)
        real_base = os.path.realpath(base_dir)
        return os.path.commonpath([real_base, real_path]) == real_base
    except (ValueError, TypeError):
        return False


def is_local_ip(ip: str) -> bool:
    """
    检查是否为本地 IP

    Args:
        ip: IP 地址字符串

    Returns:
        是否为本地 IP
    """
    try:
        addr = ip_address(ip)
        return addr.is_loopback or addr.is_private
    except:
        return False


def validate_task_id(task_id: str) -> bool:
    """
    验证任务 ID 格式

    Args:
        task_id: 任务 ID

    Returns:
        格式是否有效
    """
    if not task_id:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_-]+$', task_id))


def validate_entity_id(entity_id: int) -> bool:
    """
    验证 Telegram 实体 ID

    Args:
        entity_id: 实体 ID

    Returns:
        是否有效
    """
    return isinstance(entity_id, int) and entity_id != 0


def validate_message_id(message_id: int) -> bool:
    """
    验证消息 ID

    Args:
        message_id: 消息 ID

    Returns:
        是否有效
    """
    return isinstance(message_id, int) and message_id > 0


def sanitize_path_component(component: str) -> str:
    """
    清理路径组件中的非法字符

    Args:
        component: 路径组件（文件名或目录名）

    Returns:
        清理后的字符串
    """
    # 移除或替换非法字符
    illegal_chars = r'[<>:"/\\|?*\x00-\x1f]'
    sanitized = re.sub(illegal_chars, '_', component)

    # 移除前后空白和点
    sanitized = sanitized.strip('. ')

    # 避免空字符串
    if not sanitized:
        sanitized = "unnamed"

    return sanitized
