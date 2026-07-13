"""System status and health payload builders."""

import socket
import subprocess
import threading
import time


class SystemStatusService:
    def __init__(
        self,
        *,
        ensure_tg_connection,
        get_tg_connected,
        get_tg_error,
        get_tg_user,
        get_queue_status,
        get_tdl_status,
        proxy_config,
        tdl_binary,
        get_tasks_persisted=None,
        get_resume_count=None,
        get_relay_status=None,
        health_cache_ttl=5,
    ):
        self.ensure_tg_connection = ensure_tg_connection
        self.get_tg_connected = get_tg_connected
        self.get_tg_error = get_tg_error
        self.get_tg_user = get_tg_user
        self.get_queue_status = get_queue_status
        self.get_tdl_status = get_tdl_status
        self.proxy_config = proxy_config
        self.tdl_binary = tdl_binary
        self.get_tasks_persisted = get_tasks_persisted
        self.get_resume_count = get_resume_count
        self.get_relay_status = get_relay_status
        self.health_cache_ttl = health_cache_ttl
        self._health_cache = {}
        self._health_cache_lock = threading.RLock()

    def status_payload(self):
        self.ensure_tg_connection(allow_reconnect=True)
        return {
            "connected": self.get_tg_connected(),
            "error": self.get_tg_error(),
            "user": self.get_tg_user(),
            "queue": self.get_queue_status(),
            "tdl": self.get_tdl_status(),
        }

    def liveness_payload(self):
        """存活探针：进程是否活着（不触碰 Telegram/子进程），始终 200。"""
        return {"status": "alive"}

    def readiness_payload(self):
        """就绪探针：能否对外服务。核心信号是主 Telegram 连接是否就绪。

        返回 (payload, http_status)：就绪 200，未就绪 503。tdl 不可用属可降级
        （telethon 直连仍可下载），不阻断就绪，仅在完整 /api/health 里体现。
        """
        telegram_ok = bool(self.get_tg_connected())
        degraded = [] if telegram_ok else ["telegram"]
        payload = {
            "status": "ready" if telegram_ok else "not_ready",
            "ready": telegram_ok,
            "telegram_connected": telegram_ok,
            "degraded": degraded,
        }
        return payload, (200 if telegram_ok else 503)

    def health_payload(self):
        now = time.time()
        with self._health_cache_lock:
            cached = self._health_cache.get("payload")
            cached_time = float(self._health_cache.get("updated_at") or 0)
            if cached and now - cached_time < self.health_cache_ttl:
                return cached

        telegram_connected = bool(self.get_tg_connected())
        tdl_summary = self.tdl_version_summary()
        degraded = []
        if not telegram_connected:
            degraded.append("telegram")
        if not tdl_summary.get("ok"):
            degraded.append("tdl")

        payload = {
            "ok": telegram_connected,
            "degraded": degraded,
            "telegram": {
                "connected": telegram_connected,
                "user": self.get_tg_user(),
                "error": self.get_tg_error(),
            },
            "proxy": self.proxy_status(),
            "tdl": tdl_summary,
            "queue": self.get_queue_status(),
        }

        relay_status = self.get_relay_status() if self.get_relay_status else None
        if relay_status is not None:
            payload["relay"] = relay_status
        if self.get_tasks_persisted is not None:
            payload["tasks_persisted"] = self.get_tasks_persisted()
        if self.get_resume_count is not None:
            payload["resume_files"] = self.get_resume_count()

        with self._health_cache_lock:
            self._health_cache["payload"] = payload
            self._health_cache["updated_at"] = now
        return payload

    def proxy_status(self):
        if not self.proxy_config:
            return {"enabled": False, "ok": True, "label": "未启用"}

        proxy_type, host, port = self.proxy_config
        started = time.time()
        try:
            with socket.create_connection((host, int(port)), timeout=2):
                pass
            return {
                "enabled": True,
                "ok": True,
                "label": f"{proxy_type}://{host}:{port}",
                "latency_ms": int((time.time() - started) * 1000),
            }
        except Exception as exc:
            return {
                "enabled": True,
                "ok": False,
                "label": f"{proxy_type}://{host}:{port}",
                "error": str(exc),
            }

    def tdl_version_summary(self):
        try:
            output = subprocess.check_output(
                [self.tdl_binary, "version"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=5,
            )
            first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
            return {"ok": True, "version": first_line, "binary": self.tdl_binary}
        except Exception as exc:
            return {"ok": False, "binary": self.tdl_binary, "error": str(exc)}
