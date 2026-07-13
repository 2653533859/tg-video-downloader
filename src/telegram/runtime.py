"""Telegram client runtime helpers and shared caches."""

import asyncio
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError


class TelegramRuntime:
    """Owns Telegram connection execution and in-memory message/dialog caches."""

    def __init__(
        self,
        client,
        loop,
        *,
        format_user_display=None,
        dialog_fetch_max=2000,
        max_dialog_cache_age=300,
        max_message_cache_size=2000,
    ):
        self.client = client
        self.loop = loop
        self.format_user_display = format_user_display or self._default_user_display
        self.dialog_fetch_max = dialog_fetch_max
        self.max_dialog_cache_age = max_dialog_cache_age
        self.max_message_cache_size = max_message_cache_size

        self.connected = False
        self.connect_error = ""
        self.user_info = ""
        self.reconnect_lock = threading.Lock()          # 保护重连状态标志（快，不长持）
        self.client_reconnect_lock = threading.Lock()   # 串行化对 client 的真实 connect/disconnect
        self.last_reconnect_attempt = 0.0
        self.reconnect_in_progress = False
        self.reconnect_failures = 0
        self.reconnect_backoff_base = 8      # 基础冷却秒数
        self.reconnect_backoff_max = 120     # 冷却上限秒数
        self.reconnect_timeout = 45          # 后台重连单次超时
        self.reconnect_grace_seconds = 2.0   # 快速瞬断在本次请求内的宽限（远小于旧 45s 阻塞）
        self._reconnect_thread_factory = threading.Thread

        self.cache_lock = threading.RLock()
        self.dialogs_cache = []
        self.dialogs_serialized_cache = []
        self.dialogs_cache_updated_at = 0.0
        self.dialogs_refresh_in_progress = False
        self.dialogs_refresh_started_at = 0.0
        self.dialogs_refresh_error = ""
        self.messages_cache = {}
        self.current_entity_cache = {}
        self.videos_cache = {}
        self.replies_cache = {}
        self.last_download_dialog = ""

    @staticmethod
    def _default_user_display(user):
        username = getattr(user, "username", None)
        if username:
            return f"{user.first_name} (@{username})"
        return user.first_name

    def mark_connected(self, user_info=""):
        self.connected = True
        self.connect_error = ""
        if user_info:
            self.user_info = user_info

    def mark_error(self, message):
        self.connected = False
        self.connect_error = message

    def ensure_connection(self, allow_reconnect=True):
        if self.client.is_connected():
            self.connected = True
            if self.connect_error.startswith("Telegram 已断开") or self.connect_error.startswith(
                "Telegram 重连"
            ):
                self.connect_error = ""
            self.reconnect_failures = 0
            return True

        self.connected = False

        if not self.loop.is_running():
            self.connect_error = "Telegram 客户端尚未启动，请稍后重试..."
            return False

        if not allow_reconnect:
            if not self.connect_error:
                self.connect_error = "Telegram 未连接，请等待重连..."
            return False

        # 后台重连 + 快速失败：绝不在请求线程内同步等待重连完成（原实现会在
        # reconnect_lock 内 .result(timeout=45) 阻塞 Flask 请求线程最长 45s）。
        started = self._maybe_start_reconnect()

        # 仅当本次刚发起了一次新的重连时，给快速瞬断一个在本请求内恢复的短暂宽限；
        # 上限 reconnect_grace_seconds（默认 2s，远小于旧 45s）。持续断连时后续
        # 请求处于冷却期 started=False，不再等待，快速失败。
        if started and self.reconnect_grace_seconds > 0:
            deadline = time.time() + self.reconnect_grace_seconds
            while time.time() < deadline:
                if self.client.is_connected():
                    self.connected = True
                    self.connect_error = ""
                    self.reconnect_failures = 0
                    return True
                time.sleep(0.1)
        return False

    def _reconnect_cooldown(self):
        """指数退避冷却窗口：base, 2*base, 4*base ... 上限 backoff_max。"""
        window = self.reconnect_backoff_base * (2 ** min(self.reconnect_failures, 5))
        return min(window, self.reconnect_backoff_max)

    def _maybe_start_reconnect(self):
        """尝试发起一次后台重连。返回 True 表示本次确实启动了新的重连线程。"""
        now = time.time()
        with self.reconnect_lock:
            if self.reconnect_in_progress:
                self.connect_error = self.connect_error or "Telegram 重连中，请稍后重试..."
                return False
            if now - self.last_reconnect_attempt < self._reconnect_cooldown():
                self.connect_error = self.connect_error or "Telegram 重连中，请稍后重试..."
                return False
            self.last_reconnect_attempt = now
            self.reconnect_in_progress = True
            self.connect_error = "Telegram 已断开，正在重连..."

        try:
            self._reconnect_thread_factory(target=self._run_reconnect, daemon=True).start()
            return True
        except Exception:
            # 线程启动失败：回滚 in_progress，否则会永久卡在“重连中”而再不发起重连
            with self.reconnect_lock:
                self.reconnect_in_progress = False
            return False

    def _run_reconnect(self):
        try:
            async def _reconnect():
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    raise Exception("Telegram 未登录，请先运行 login.py 登录。")
                user = await self.client.get_me()
                return self.format_user_display(user)

            # 与 health checker 的重连共享同一把 client 锁，避免 connect/disconnect
            # 在同一 client 上并发交错导致连接抖动。
            with self.client_reconnect_lock:
                user_info = asyncio.run_coroutine_threadsafe(
                    _reconnect(),
                    self.loop,
                ).result(timeout=self.reconnect_timeout)
            self.user_info = user_info
            self.connected = True
            self.connect_error = ""
            self.reconnect_failures = 0
        except Exception as exc:
            self.connected = False
            self.connect_error = f"Telegram 重连失败: {exc}"
            self.reconnect_failures += 1
        finally:
            with self.reconnect_lock:
                self.reconnect_in_progress = False

    def run_async(self, coro_factory, timeout=600, allow_reconnect=True, error_label="Telegram"):
        if not callable(coro_factory):
            raise TypeError("run_async expects a callable returning coroutine")

        if not self.ensure_connection(allow_reconnect=allow_reconnect):
            raise Exception(self.connect_error or f"{error_label} 未连接，请等待重连...")

        future = asyncio.run_coroutine_threadsafe(coro_factory(), self.loop)
        try:
            return future.result(timeout=timeout)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(f"{error_label} 操作超时（>{int(timeout)}s）") from exc
        except Exception as exc:
            message = str(exc).lower()
            if "disconnected" in message or "connection reset" in message or "could not connect to proxy" in message:
                self.connected = False
                self.connect_error = f"{error_label} 连接中断: {exc}"
            raise

    def serialize_dialogs(self, dialogs):
        result = []
        for index, dialog in enumerate(dialogs):
            dtype = "频道" if dialog.is_channel else "群组" if dialog.is_group else "私聊"
            name = dialog.name
            is_saved = False
            try:
                if getattr(dialog.entity, "is_self", False):
                    name = "⭐ 个人收藏 (Saved Messages)"
                    is_saved = True
            except Exception:
                pass

            result.append({
                "index": index,
                "name": name,
                "id": dialog.id,
                "type": dtype,
                "is_channel": dialog.is_channel,
                "is_group": dialog.is_group,
                "is_saved": is_saved,
            })

        result.sort(key=lambda item: not item["is_saved"])
        return result

    def dialogs_snapshot(self):
        with self.cache_lock:
            return {
                "dialogs": list(self.dialogs_serialized_cache),
                "updated_at": self.dialogs_cache_updated_at,
                "loading": self.dialogs_refresh_in_progress,
                "error": self.dialogs_refresh_error,
            }

    def set_dialogs_refresh_error(self, message):
        with self.cache_lock:
            self.dialogs_refresh_error = message

    async def collect_dialogs(self):
        dialogs = []
        async for dialog in self.client.iter_dialogs():
            dialogs.append(dialog)
            if len(dialogs) >= self.dialog_fetch_max:
                break
        return dialogs

    def refresh_dialogs_cache(self):
        try:
            dialogs = self.run_async(self.collect_dialogs, timeout=120)
            serialized = self.serialize_dialogs(dialogs)
            with self.cache_lock:
                self.dialogs_cache.clear()
                self.dialogs_cache.extend(dialogs)
                self.dialogs_serialized_cache[:] = serialized
                self.dialogs_cache_updated_at = time.time()
                self.dialogs_refresh_error = ""
        except TimeoutError:
            self.set_dialogs_refresh_error("加载对话列表超时，请稍后重试")
        except Exception as exc:
            self.set_dialogs_refresh_error(str(exc) or "加载对话列表失败")
        finally:
            with self.cache_lock:
                self.dialogs_refresh_in_progress = False
                self.dialogs_refresh_started_at = 0.0

    def kickoff_dialogs_refresh(self, force=False):
        with self.cache_lock:
            cache_exists = bool(self.dialogs_serialized_cache)
            cache_fresh = (
                cache_exists
                and (time.time() - self.dialogs_cache_updated_at) < self.max_dialog_cache_age
            )
            if self.dialogs_refresh_in_progress:
                return False
            if not force and cache_fresh and not self.dialogs_refresh_error:
                return False
            self.dialogs_refresh_in_progress = True
            self.dialogs_refresh_started_at = time.time()

        threading.Thread(target=self.refresh_dialogs_cache, daemon=True).start()
        return True

    @staticmethod
    def entity_id(entity):
        if not entity:
            return None
        return getattr(entity, "id", None)

    @staticmethod
    def message_entity_id(message, fallback_entity_id=None):
        if message is None:
            return fallback_entity_id
        return getattr(message, "chat_id", None) or fallback_entity_id

    @staticmethod
    def make_msg_cache_key(entity_id, msg_id):
        if entity_id is None or msg_id is None:
            return None
        return (int(entity_id), int(msg_id))

    @staticmethod
    def make_task_id(entity_id, msg_id):
        if entity_id is None or msg_id is None:
            return None
        return f"{int(entity_id)}:{int(msg_id)}"

    def cache_message(self, message, entity_id):
        key = self.make_msg_cache_key(entity_id, getattr(message, "id", None))
        if not key:
            return
        with self.cache_lock:
            self.messages_cache[key] = message
            if len(self.messages_cache) > self.max_message_cache_size:
                for _ in range(min(100, len(self.messages_cache) - self.max_message_cache_size + 50)):
                    self.messages_cache.pop(next(iter(self.messages_cache)), None)

    def get_cached_message(self, msg_id, entity_id=None):
        with self.cache_lock:
            key = self.make_msg_cache_key(entity_id, msg_id)
            if key and key in self.messages_cache:
                return self.messages_cache[key]
            if entity_id is None:
                last_eid = self.current_entity_cache.get("entity_id")
                key = self.make_msg_cache_key(last_eid, msg_id)
                if key and key in self.messages_cache:
                    return self.messages_cache[key]
                # 仅在调用方未指定 entity 时才做跨频道兜底；指定了 entity 却未命中
                # 必须返回 None，避免返回其他频道同 msg_id 的消息导致下错文件。
                for (_eid, mid), message in list(self.messages_cache.items()):
                    if mid == msg_id:
                        return message
        return None

    def resolve_requested_entity(self, source="dialog", dialog_index=None, entity_id=None):
        entity = None
        name = "unknown"

        if source == "search":
            with self.cache_lock:
                entity = self.current_entity_cache.get("search_entity")
                name = self.current_entity_cache.get("search_name", "unknown")
            if entity is None and entity_id:
                entity = self.run_async(lambda: self.client.get_entity(entity_id))
                name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity_id)
        elif dialog_index is not None:
            with self.cache_lock:
                if 0 <= dialog_index < len(self.dialogs_cache):
                    entity = self.dialogs_cache[dialog_index].entity
                    name = self.dialogs_cache[dialog_index].name

        if entity is None and entity_id:
            entity = self.run_async(lambda: self.client.get_entity(entity_id))
            name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity_id)

        return entity, name

    def resolve_message(self, entity_id, msg_id, force_refresh=False):
        message = None if force_refresh else self.get_cached_message(msg_id, entity_id)
        if message:
            return message
        message = self.run_async(lambda eid=entity_id, mid=msg_id: self.client.get_messages(eid, ids=mid))
        if not message and entity_id is not None:
            entity = self.run_async(lambda eid=entity_id: self.client.get_entity(eid))
            if entity is not None:
                message = self.run_async(lambda ent=entity, mid=msg_id: self.client.get_messages(ent, ids=mid))
        if message:
            self.cache_message(message, entity_id)
        return message
