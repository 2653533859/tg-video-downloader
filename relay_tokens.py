"""
Relay Token 生成和验证
用于生成带签名的 relay URL token，确保下载请求的安全性
"""
import hmac
import hashlib
from typing import Optional


def build_relay_token(
    secret: str,
    entity_id: int,
    message_id: int,
    file_name: str,
    expire_at: int
) -> str:
    """
    生成 relay token（带签名）

    Args:
        secret: 签名密钥
        entity_id: Telegram 实体 ID（频道/群组）
        message_id: 消息 ID
        file_name: 文件名
        expire_at: 过期时间戳（Unix timestamp）

    Returns:
        签名后的 token 字符串
    """
    # 构造待签名的消息
    message = f"{entity_id}:{message_id}:{file_name}:{expire_at}"

    # 使用 HMAC-SHA256 生成签名
    signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # 返回格式：签名.过期时间
    return f"{signature}.{expire_at}"


def verify_relay_token(
    secret: str,
    token: str,
    entity_id: int,
    message_id: int,
    file_name: str,
    now_ts: int
) -> None:
    """
    验证 relay token 的有效性

    Args:
        secret: 签名密钥
        token: 要验证的 token
        entity_id: Telegram 实体 ID
        message_id: 消息 ID
        file_name: 文件名
        now_ts: 当前时间戳（Unix timestamp）

    Raises:
        ValueError: token 格式错误、签名无效或已过期
    """
    # 解析 token
    parts = token.split('.')
    if len(parts) != 2:
        raise ValueError("Invalid token format")

    signature, expire_at_str = parts

    try:
        expire_at = int(expire_at_str)
    except ValueError:
        raise ValueError("Invalid expiration time in token")

    # 检查是否过期
    if now_ts > expire_at:
        raise ValueError("Token has expired")

    # 重新计算签名
    message = f"{entity_id}:{message_id}:{file_name}:{expire_at}"
    expected_signature = hmac.new(
        secret.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    # 使用常量时间比较防止时序攻击
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Invalid token signature")
