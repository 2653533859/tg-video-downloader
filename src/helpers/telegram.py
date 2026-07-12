"""
Telegram 相关辅助函数
"""
import asyncio
import threading
from typing import Optional, Any, Dict


class TelegramHelper:
    """Telegram 辅助类"""

    def __init__(self, tg_client, tg_loop):
        """
        初始化

        Args:
            tg_client: Telegram 客户端
            tg_loop: asyncio 事件循环
        """
        self.client = tg_client
        self.loop = tg_loop
        self.cache_lock = threading.RLock()
        self.message_cache = {}

    def run_async(self, coro_or_func, timeout=30, allow_reconnect=True):
        """
        在 Telegram 事件循环中运行异步函数

        Args:
            coro_or_func: 协程或函数
            timeout: 超时时间（秒）
            allow_reconnect: 是否允许重连

        Returns:
            执行结果
        """
        if callable(coro_or_func) and not asyncio.iscoroutine(coro_or_func):
            coro = coro_or_func()
        else:
            coro = coro_or_func

        future = asyncio.run_coroutine_threadsafe(coro, self.loop)

        try:
            return future.result(timeout=timeout)
        except Exception as e:
            if allow_reconnect and "disconnect" in str(e).lower():
                # 尝试重连
                self.ensure_connection(allow_reconnect=True)
                # 重试一次
                future = asyncio.run_coroutine_threadsafe(coro, self.loop)
                return future.result(timeout=timeout)
            raise

    def ensure_connection(self, allow_reconnect=False):
        """
        确保 Telegram 连接

        Args:
            allow_reconnect: 是否允许重连
        """
        # 实现连接检查和重连逻辑
        pass

    def get_cached_message(self, msg_id, entity_id=None):
        """
        获取缓存的消息

        Args:
            msg_id: 消息 ID
            entity_id: 实体 ID

        Returns:
            Message 对象或 None
        """
        with self.cache_lock:
            key = f"{entity_id}_{msg_id}"
            return self.message_cache.get(key)

    def cache_message(self, message, entity_id=None):
        """
        缓存消息

        Args:
            message: Message 对象
            entity_id: 实体 ID
        """
        if not message:
            return

        msg_id = getattr(message, 'id', None)
        if msg_id is None:
            return

        with self.cache_lock:
            key = f"{entity_id}_{msg_id}"
            self.message_cache[key] = message

    def resolve_message(self, entity_id, msg_id, force_refresh=False):
        """
        解析消息

        Args:
            entity_id: 实体 ID
            msg_id: 消息 ID
            force_refresh: 是否强制刷新

        Returns:
            Message 对象
        """
        if not force_refresh:
            cached = self.get_cached_message(msg_id, entity_id)
            if cached:
                return cached

        async def _fetch():
            entity = await self.client.get_entity(entity_id)
            message = await self.client.get_messages(entity, ids=msg_id)
            return message

        message = self.run_async(_fetch)
        self.cache_message(message, entity_id)
        return message


def get_video_info(message):
    """
    从消息中提取视频信息

    Args:
        message: Telegram Message 对象

    Returns:
        视频信息字典或 None
    """
    if not message or not message.media:
        return None

    from telethon.tl.types import MessageMediaDocument, DocumentAttributeVideo, DocumentAttributeFilename

    if not isinstance(message.media, MessageMediaDocument):
        return None

    doc = message.media.document
    if not doc:
        return None

    # 检查是否为视频
    has_video = False
    duration = 0
    width = 0
    height = 0
    filename = None

    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            has_video = True
            duration = getattr(attr, 'duration', 0)
            width = getattr(attr, 'w', 0)
            height = getattr(attr, 'h', 0)
        elif isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name

    if not has_video:
        return None

    # 如果没有文件名，生成一个
    if not filename:
        filename = f"video_{message.id}.mp4"

    return {
        "id": message.id,
        "filename": filename,
        "size": doc.size,
        "duration": duration,
        "width": width,
        "height": height,
        "mime_type": getattr(doc, "mime_type", "video/mp4"),
        "has_thumb": doc.thumbs and len(doc.thumbs) > 0,
    }


def video_info_for_message(message, entity_id, source="", extra=None):
    """
    为消息生成完整的视频信息

    Args:
        message: Message 对象
        entity_id: 实体 ID
        source: 来源标识
        extra: 额外信息字典

    Returns:
        完整的视频信息字典
    """
    info = get_video_info(message)
    if not info:
        return None

    # 添加额外信息
    result = {
        **info,
        "entity_id": entity_id,
        "message_id": message.id,
        "source": source,
        "date": message.date.isoformat() if hasattr(message, 'date') and message.date else None,
    }

    # 添加文本信息
    text = message.text or message.message or ""
    if text:
        result["text"] = text
        result["text_excerpt"] = text[:200] if len(text) > 200 else text

    # 合并额外信息
    if extra:
        result.update(extra)

    return result


def supports_tdl_download(entity_id):
    """
    检查是否支持 TDL 下载

    Args:
        entity_id: 实体 ID

    Returns:
        是否支持
    """
    if entity_id is None:
        return False

    # TDL 仅支持频道/超级群（ID 以 -100 开头）
    return str(int(entity_id)).startswith("-100")


def build_tdl_message_url(entity_id, msg_id):
    """
    构建 TDL 消息 URL

    Args:
        entity_id: 实体 ID
        msg_id: 消息 ID

    Returns:
        消息 URL
    """
    if not supports_tdl_download(entity_id):
        raise ValueError("仅支持频道/超级群消息的 tdl 直链下载")

    raw = str(int(entity_id))
    if not raw.startswith("-100"):
        raise ValueError("仅支持频道/超级群消息的 tdl 直链下载")

    dialog_id = raw[4:]
    return f"https://t.me/c/{dialog_id}/{int(msg_id)}"
