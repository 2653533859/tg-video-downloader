import os


def _strtobool(value, default=False):
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Telegram API 配置
API_ID = int(os.getenv("TG_API_ID", "0") or "0")
API_HASH = os.getenv("TG_API_HASH", "").strip()

# 下载配置
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
SESSION_NAME = os.getenv("SESSION_NAME", "tg_downloader")

# 代理配置，留空表示不使用代理。这个部署通过 mihomo 的 HTTP 入口访问
# Telegram，避免 socks/socks5h 在 Telethon、tdl 和 requests 之间表现不一致。
ALLOWED_PROXY_TYPES = ("http",)
_proxy_host = os.getenv("TG_PROXY_HOST", "127.0.0.1").strip()
_proxy_port = int(os.getenv("TG_PROXY_PORT", "7890") or "7890")
_proxy_type = os.getenv("TG_PROXY_TYPE", "http").strip().lower()


def normalize_proxy_type(value):
    proxy_type = (value or "").strip().lower()
    if proxy_type not in ALLOWED_PROXY_TYPES:
        raise ValueError(
            f"Unsupported proxy type: {value} (allowed: {', '.join(ALLOWED_PROXY_TYPES)})"
        )
    return proxy_type


def build_proxy_config(proxy_type=None):
    if not _strtobool(os.getenv("TG_PROXY_ENABLED"), default=True):
        return None
    normalized_type = normalize_proxy_type(proxy_type or _proxy_type)
    return (normalized_type, _proxy_host, _proxy_port)


def build_telethon_proxy_config(proxy_config=None):
    proxy_config = PROXY_CONFIG if proxy_config is None else proxy_config
    if not proxy_config:
        return None
    return proxy_config


def proxy_config_label(proxy_config):
    if not proxy_config:
        return "未启用"
    proxy_type, host, port = proxy_config
    return f"{proxy_type}://{host}:{port}"


PROXY_CONFIG = build_proxy_config()
TELETHON_PROXY_CONFIG = build_telethon_proxy_config(PROXY_CONFIG)

# Web 安全配置
WEB_BIND_HOST = os.getenv("WEB_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
WEB_BIND_PORT = int(os.getenv("WEB_BIND_PORT", "5003") or "5003")
WEB_AUTH_USERNAME = os.getenv("WEB_AUTH_USERNAME", "").strip()
WEB_AUTH_PASSWORD = os.getenv("WEB_AUTH_PASSWORD", "").strip()
DEBUG_API_ENABLED = _strtobool(os.getenv("DEBUG_API_ENABLED"), default=False)
OPEN_FOLDER_ENABLED = _strtobool(os.getenv("OPEN_FOLDER_ENABLED"), default=False)
# 仅当部署在可信反向代理之后时才开启：开启后 X-Forwarded-For 会参与
# "本地请求"判定；默认关闭，防止伪造该头绕过 Basic Auth。
TRUST_FORWARDED_FOR = _strtobool(os.getenv("TRUST_FORWARDED_FOR"), default=False)

# Relay 配置
RELAY_TOKEN_SECRET = os.getenv("RELAY_TOKEN_SECRET", "").strip()
RELAY_TOKEN_TTL = int(os.getenv("RELAY_TOKEN_TTL", "1800") or "1800")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip()

# 已废弃的 aria2 配置：当前生产入口（app.py）不再支持 aria2 下载通道，
# 仅 app_legacy.py / app_async.py 旧入口仍在 import。待 P1-5 归档旧入口时一并删除。
ARIA2_RPC_URL = os.getenv("ARIA2_RPC_URL", "http://127.0.0.1:6800/jsonrpc").strip()
ARIA2_SECRET = os.getenv("ARIA2_SECRET", "").strip()
ARIA2_DOWNLOAD_DIR = os.getenv("ARIA2_DOWNLOAD_DIR", "/downloads").strip() or "/downloads"

# tdl 直连下载配置
TDL_BINARY = os.getenv("TDL_BINARY", "/usr/local/bin/tdl").strip() or "/usr/local/bin/tdl"
TDL_NAMESPACE = os.getenv("TDL_NAMESPACE", "default").strip() or "default"
TDL_STORAGE_PATH = os.getenv("TDL_STORAGE_PATH", "/root/.tdl/data").strip() or "/root/.tdl/data"
TDL_THREADS = int(os.getenv("TDL_THREADS", "8") or "8")
TDL_LIMIT = int(os.getenv("TDL_LIMIT", "4") or "4")
TDL_CHAT_ID_OVERRIDES = os.getenv("TDL_CHAT_ID_OVERRIDES", "").strip()
