"""tdl command, process, and error runtime helpers."""

import os
import threading


class TdlRuntime:
    def __init__(
        self,
        *,
        binary,
        namespace,
        storage_path,
        threads,
        limit,
        proxy_config=None,
        has_fallback_channel=None,
        chat_id_overrides=None,
    ):
        self.binary = binary
        self.namespace = namespace
        self.storage_path = storage_path
        self.threads = threads
        self.limit = limit
        self.proxy_config = proxy_config
        self.has_fallback_channel = has_fallback_channel or (lambda _entity_id: False)
        self.chat_id_overrides = self._parse_chat_id_overrides(chat_id_overrides)
        self.lock = threading.RLock()
        self.processes = {}
        self.last_errors = {}

    def status(self):
        with self.lock:
            active = len([proc for proc in self.processes.values() if proc.poll() is None])
            error = list(self.last_errors.values())[-1] if self.last_errors else ""
        return {
            "binary": self.binary,
            "available": os.path.exists(self.binary),
            "namespace": self.namespace,
            "threads": self.threads,
            "limit": self.limit,
            "active": active,
            "error": error,
        }

    @staticmethod
    def _parse_chat_id_overrides(value):
        if not value:
            return {}
        if isinstance(value, dict):
            return {str(k).strip(): str(v).strip() for k, v in value.items() if str(k).strip() and str(v).strip()}
        overrides = {}
        for item in str(value).split(","):
            if not item.strip() or "=" not in item:
                continue
            key, replacement = item.split("=", 1)
            key = key.strip()
            replacement = replacement.strip()
            if key and replacement:
                overrides[key] = replacement
        return overrides

    def build_message_url(self, entity_id, msg_id):
        if entity_id is None or msg_id is None:
            raise ValueError("缺少消息标识")
        if int(entity_id) >= 0:
            raise ValueError("暂不支持该对话类型的 tdl 直链下载")
        raw = str(int(entity_id))
        if not raw.startswith("-100"):
            raise ValueError("仅支持频道/超级群消息的 tdl 直链下载")
        dialog_id = self.chat_id_overrides.get(raw, raw[4:])
        return f"https://t.me/c/{dialog_id}/{int(msg_id)}"

    def supports_download(self, entity_id):
        if entity_id is None:
            return False
        return str(int(entity_id)).startswith("-100") and not self.has_fallback_channel(entity_id)

    def proxy_url(self):
        if not self.proxy_config:
            return ""
        proxy_type, host, port = self.proxy_config
        return f"{proxy_type}://{host}:{port}"

    def build_download_command(self, message_url, download_dir, output_name):
        command = [
            self.binary,
            "download",
            "--continue",
            "--skip-same",
            "--reconnect-timeout", "0",
            "-u",
            message_url,
            "-d",
            download_dir,
            "--template",
            output_name,
            "-n",
            self.namespace,
            "--storage",
            f"type=bolt,path={self.storage_path}",
            "-t",
            str(self.threads),
            "-l",
            str(self.limit),
        ]
        proxy_url = self.proxy_url()
        if proxy_url:
            command.extend(["--proxy", proxy_url])
        return command

    def register_process(self, task_id, process):
        with self.lock:
            self.processes[task_id] = process

    def drop_process(self, task_id):
        with self.lock:
            return self.processes.pop(task_id, None)

    def get_process(self, task_id):
        with self.lock:
            return self.processes.get(task_id)

    def set_error(self, task_id, message):
        with self.lock:
            self.last_errors[task_id] = message

    def clear_error(self, task_id):
        with self.lock:
            self.last_errors.pop(task_id, None)

    def last_error(self, task_id, default=""):
        with self.lock:
            return self.last_errors.get(task_id, default)

