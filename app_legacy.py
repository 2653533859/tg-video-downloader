#!/usr/bin/env python3
"""Telegram 视频下载器 - Web UI"""

import os
import sys
import asyncio
import threading
import time
import json
import re
import subprocess
import hmac
import queue
import shutil
import socket
import sqlite3
from concurrent.futures import TimeoutError as FutureTimeoutError
from ipaddress import ip_address
from datetime import datetime
from urllib.parse import quote
from flask import Flask, render_template, jsonify, request, Response, send_from_directory, send_file
from werkzeug.routing import BaseConverter
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)

# ==================== 日志系统 ====================
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger = logging.getLogger("tg_downloader")
logger.setLevel(logging.INFO)

fh = RotatingFileHandler(os.path.join(LOG_DIR, "app.log"), maxBytes=10*1024*1024, backupCount=30, encoding="utf-8")
fh.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(fh)
logger.addHandler(logging.StreamHandler())

def log_info(msg): logger.info(msg)
def log_error(msg): logger.error(msg)
def log_warning(msg): logger.warning(msg)

from config import (
    API_ID,
    API_HASH,
    ARIA2_DOWNLOAD_DIR,
    ARIA2_RPC_URL,
    ARIA2_SECRET,
    DEBUG_API_ENABLED,
    DOWNLOAD_DIR,
    OPEN_FOLDER_ENABLED,
    PUBLIC_BASE_URL,
    PROXY_CONFIG,
    RELAY_TOKEN_SECRET,
    RELAY_TOKEN_TTL,
    SESSION_NAME,
    TDL_BINARY,
    TDL_LIMIT,
    TDL_NAMESPACE,
    TDL_STORAGE_PATH,
    TDL_THREADS,
    WEB_AUTH_PASSWORD,
    WEB_AUTH_USERNAME,
    WEB_BIND_HOST,
    WEB_BIND_PORT,
)
from aria2_client import Aria2Client
from relay_tokens import build_relay_token, verify_relay_token

app = Flask(__name__)


class SignedIntConverter(BaseConverter):
    regex = r"-?\d+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(int(value))


app.url_map.converters["signed_int"] = SignedIntConverter

from telethon.sessions import StringSession

tg_loop = asyncio.new_event_loop()
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop, proxy=PROXY_CONFIG)
# Relay client will be initialized with a StringSession to avoid database file lock conflicts
relay_loop = asyncio.new_event_loop()
relay_tg_client = TelegramClient(StringSession(), API_ID, API_HASH, loop=relay_loop, proxy=PROXY_CONFIG)
aria2 = Aria2Client(ARIA2_RPC_URL, ARIA2_SECRET)

# 连接状态
tg_connected = False
tg_connect_error = ""
tg_user_info = ""
relay_connected = False
relay_connect_error = ""

# 下载状态: task_id(entity_id:msg_id) -> {filename, progress, status, downloaded, total, error, speed, entity_id, msg_id, dialog_name, downloaded_bytes, total_bytes, speed_bps, queue_position}
download_status = {}
# 下载取消标记: task_id -> True
download_cancel = {}
# 终止态集合，避免回调在任务完成后继续覆盖状态
TERMINAL_STATES = {"done", "skipped", "error", "cancelled"}
status_lock = threading.RLock()
cache_lock = threading.RLock()
tdl_lock = threading.RLock()
tdl_processes = {}
tdl_last_errors = {}
TDL_MAX_EOF_RETRIES = 15
TDL_RESTART_RESET_MIN_BYTES = 64 * 1024 * 1024



# ==================== 下载监控看门狗 ====================
# 此代码需要插入到 app.py 中的全局变量区域之后

class DownloadWatchdog:
    """
    下载监控看门狗
    - 检测下载任务是否停滞（长时间无进度）
    - 自动重启卡死的下载任务
    """
    def __init__(self, check_interval=60, stall_timeout=300):
        self.check_interval = check_interval  # 检查间隔（秒）
        self.stall_timeout = stall_timeout    # 无进度超时（秒）
        self.last_progress = {}               # {task_id: {'bytes': int, 'time': float}}
        self.running = False
        self.thread = None
        
    def start(self):
        """启动监控线程"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        log_info(f"[watchdog] 下载监控已启动 (检查间隔:{self.check_interval}s, 超时阈值:{self.stall_timeout}s)")
    
    def stop(self):
        """停止监控"""
        self.running = False
        
    def _monitor_loop(self):
        """监控主循环"""
        while self.running:
            try:
                time.sleep(self.check_interval)
                self._check_all_tasks()
            except Exception as e:
                log_error(f"[watchdog] 监控异常: {e}")
    
    def _check_all_tasks(self):
        """检查所有下载任务"""
        current_time = time.time()
        
        with status_lock:
            tasks_snapshot = list(download_status.items())
        
        for task_id, task in tasks_snapshot:
            # 只监控正在下载的任务
            if task.get('status') != 'downloading':
                # 清理非下载状态的监控记录
                self.last_progress.pop(task_id, None)
                continue
            
            current_bytes = task.get('downloaded_bytes', 0)
            
            # 初次见到此任务，记录初始状态
            if task_id not in self.last_progress:
                self.last_progress[task_id] = {
                    'bytes': current_bytes,
                    'time': current_time
                }
                continue
            
            last_info = self.last_progress[task_id]
            elapsed = current_time - last_info['time']
            bytes_diff = current_bytes - last_info['bytes']
            
            # 检查是否停滞
            if bytes_diff == 0 and elapsed > self.stall_timeout:
                log_warning(
                    f"[watchdog] 任务 {task_id} 已停滞 {elapsed:.0f}s "
                    f"(进度: {task.get('progress', 0)}%, "
                    f"已下载: {task.get('downloaded', '0B')}), "
                    f"触发自动重启"
                )
                self._restart_stuck_task(task_id, task)
                # 清理监控记录，等待重启后重新监控
                self.last_progress.pop(task_id, None)
                
            elif bytes_diff > 0:
                # 有进度，更新记录
                self.last_progress[task_id] = {
                    'bytes': current_bytes,
                    'time': current_time
                }
    
    def _restart_stuck_task(self, task_id, task):
        """重启卡死的任务"""
        try:
            entity_id = task.get('entity_id')
            msg_id = task.get('msg_id')
            dialog_name = task.get('dialog_name', '')
            
            if not entity_id or not msg_id:
                log_error(f"[watchdog] 任务 {task_id} 缺少必要信息，无法重启")
                return
            
            # 标记任务为错误状态（触发重启前先中断当前下载）
            with status_lock:
                if task_id in download_status:
                    download_status[task_id]['status'] = 'error'
                    download_status[task_id]['error'] = 'watchdog 检测到停滞，自动重启中...'
            
            # 标记取消（中断可能卡住的下载线程）
            _mark_download_cancelled(task_id)
            
            # 等待一小段时间让线程响应取消信号
            time.sleep(2)
            
            # 清除取消标记，准备重启
            _clear_download_cancelled(task_id)
            
            # 调用恢复任务函数（支持断点续传）
            log_info(f"[watchdog] 正在重启任务 {task_id}...")
            result = _resume_task(task_id, dialog_name=dialog_name, auto=True)
            
            if result.get('ok'):
                log_info(f"[watchdog] 任务 {task_id} 重启成功")
            else:
                log_error(f"[watchdog] 任务 {task_id} 重启失败: {result.get('error', 'unknown')}")
                
        except Exception as e:
            log_error(f"[watchdog] 重启任务 {task_id} 时异常: {e}")


# 全局看门狗实例
download_watchdog = DownloadWatchdog(
    check_interval=60,      # 每 60 秒检查一次
    stall_timeout=300       # 5 分钟无进度视为停滞
)



# ==================== Telegram 连接健康检查 ====================
# 此代码需要添加到 app.py 中

import asyncio
import threading
import time

class TelegramHealthChecker:
    """
    Telegram 连接健康检查
    - 定期检查 Telegram 客户端连接状态
    - 发现异常时主动重连
    - 减少下载卡死的概率
    """
    def __init__(self, client, loop, check_interval=120, max_retry=3):
        self.client = client                # TelegramClient 实例
        self.loop = loop                    # asyncio event loop
        self.check_interval = check_interval  # 检查间隔（秒）
        self.max_retry = max_retry          # 最大重试次数
        self.running = False
        self.thread = None
        self.last_check_ok = True
        self.consecutive_failures = 0
        
    def start(self):
        """启动健康检查线程"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._check_loop, daemon=True)
        self.thread.start()
        log_info(f"[tg-health] Telegram 连接健康检查已启动 (间隔:{self.check_interval}s)")
    
    def stop(self):
        """停止健康检查"""
        self.running = False
        
    def _check_loop(self):
        """健康检查主循环"""
        while self.running:
            try:
                time.sleep(self.check_interval)
                self._perform_health_check()
            except Exception as e:
                log_error(f"[tg-health] 健康检查异常: {e}")
    
    def _perform_health_check(self):
        """执行健康检查"""
        try:
            # 使用 asyncio.run_coroutine_threadsafe 在事件循环中执行异步检查
            future = asyncio.run_coroutine_threadsafe(
                self._async_health_check(),
                self.loop
            )
            # 等待结果，设置超时
            result = future.result(timeout=30)
            
            if result:
                if not self.last_check_ok:
                    log_info("[tg-health] Telegram 连接已恢复正常")
                self.last_check_ok = True
                self.consecutive_failures = 0
            else:
                self._handle_check_failure()
                
        except Exception as e:
            log_error(f"[tg-health] 健康检查执行失败: {e}")
            self._handle_check_failure()
    
    async def _async_health_check(self):
        """异步健康检查（轻量级操作）"""
        try:
            # 检查客户端是否已连接
            if not self.client.is_connected():
                log_warning("[tg-health] 客户端未连接")
                return False
            
            # 尝试获取一个对话（轻量级操作）
            await asyncio.wait_for(
                self.client.get_dialogs(limit=1),
                timeout=10.0
            )
            return True
            
        except asyncio.TimeoutError:
            log_warning("[tg-health] 健康检查超时")
            return False
        except Exception as e:
            log_warning(f"[tg-health] 健康检查失败: {e}")
            return False
    
    def _handle_check_failure(self):
        """处理检查失败"""
        self.consecutive_failures += 1
        self.last_check_ok = False
        
        log_warning(
            f"[tg-health] Telegram 连接异常 "
            f"(连续失败: {self.consecutive_failures}/{self.max_retry})"
        )
        
        # 连续失败达到阈值，触发重连
        if self.consecutive_failures >= self.max_retry:
            log_warning("[tg-health] 触发 Telegram 重连")
            self._trigger_reconnect()
            self.consecutive_failures = 0
    
    def _trigger_reconnect(self):
        """触发 Telegram 重连"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_reconnect(),
                self.loop
            )
            future.result(timeout=30)
        except Exception as e:
            log_error(f"[tg-health] 重连失败: {e}")
    
    async def _async_reconnect(self):
        """异步重连"""
        try:
            log_info("[tg-health] 正在断开连接...")
            await self.client.disconnect()
            await asyncio.sleep(5)
            
            log_info("[tg-health] 正在重新连接...")
            await self.client.connect()
            
            if self.client.is_connected():
                log_info("[tg-health] Telegram 重连成功")
                global tg_connected
                tg_connected = True
            else:
                log_error("[tg-health] Telegram 重连失败")
                
        except Exception as e:
            log_error(f"[tg-health] 重连过程异常: {e}")


# 全局健康检查实例（需要在 tg_client 初始化后创建）
tg_health_checker = None

def init_tg_health_checker():
    """初始化 Telegram 健康检查器"""
    global tg_health_checker
    if tg_health_checker is None:
        tg_health_checker = TelegramHealthChecker(
            client=tg_client,
            loop=tg_loop,
            check_interval=120,    # 每 2 分钟检查一次
            max_retry=3            # 连续 3 次失败触发重连
        )
        tg_health_checker.start()


TDL_MAX_RETRY_ATTEMPTS = 5
TDL_MAX_STALLED_EOF_RETRIES = 2



LOCAL_ONLY_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_local_bind_only():
    return WEB_BIND_HOST in LOCAL_ONLY_HOSTS


def _request_ip_is_local():
    remote = request.headers.get("X-Forwarded-For", request.remote_addr or "")
    remote = remote.split(",", 1)[0].strip()
    if not remote:
        return False
    try:
        return ip_address(remote).is_loopback
    except ValueError:
        return remote in LOCAL_ONLY_HOSTS


def _require_web_auth():
    if _request_ip_is_local() and _is_local_bind_only():
        return None
    if not WEB_AUTH_USERNAME or not WEB_AUTH_PASSWORD:
        return jsonify({"error": "Web auth is required for non-local access"}), 403
    auth = request.authorization
    if not auth:
        return Response(
            "Authentication required",
            401,
            {"WWW-Authenticate": 'Basic realm="tg-video-downloader"'},
        )
    user_ok = hmac.compare_digest(auth.username or "", WEB_AUTH_USERNAME)
    pass_ok = hmac.compare_digest(auth.password or "", WEB_AUTH_PASSWORD)
    if user_ok and pass_ok:
        return None
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": 'Basic realm="tg-video-downloader"'},
    )


def _abort_if_debug_disabled():
    if DEBUG_API_ENABLED:
        return None
    return jsonify({"error": "Debug API disabled"}), 404


def _resolve_download_path(*parts, must_exist=False):
    base_dir = os.path.realpath(DOWNLOAD_DIR)
    candidate = os.path.realpath(os.path.join(base_dir, *parts))
    if os.path.commonpath([base_dir, candidate]) != base_dir:
        raise ValueError("非法路径")
    if must_exist and not os.path.exists(candidate):
        raise FileNotFoundError("文件不存在")
    return candidate


def _copy_task_state(task_id):
    with status_lock:
        state = download_status.get(task_id)
        return dict(state) if state else None


TASK_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".task_state")
os.makedirs(TASK_STATE_DIR, exist_ok=True)
TASK_DB_PATH = os.path.join(TASK_STATE_DIR, "tasks.sqlite3")
TASK_DB_BACKUP_DIR = os.path.join(TASK_STATE_DIR, "backups")
TASK_DB_BACKUP_RETENTION_DAYS = 7
_task_db_lock = threading.RLock()


def _get_task_state_file(task_id):
    safe_name = re.sub(r"[^0-9A-Za-z_.:-]", "_", str(task_id))
    return os.path.join(TASK_STATE_DIR, f"{safe_name}.json")


def _task_persistence_enabled():
    return "unittest" not in sys.modules


def _task_db_connect():
    conn = sqlite3.connect(TASK_DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_states (
            task_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tdl_fallback_channels (
            entity_id TEXT PRIMARY KEY,
            reason TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_history (
            task_id TEXT PRIMARY KEY,
            state_json TEXT NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        )
    """)
    return conn


def _migrate_legacy_task_state_files():
    if not _task_persistence_enabled():
        return
    for name in os.listdir(TASK_STATE_DIR):
        if not name.endswith(".json"):
            continue
        task_id = name[:-5]
        path = os.path.join(TASK_STATE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            _persist_task_state(task_id, state)
            os.remove(path)
        except Exception as exc:
            log_warning(f"[{task_id}] 迁移旧任务状态失败: {exc}")


def _persist_task_state(task_id, state):
    if not task_id or not _task_persistence_enabled():
        return
    try:
        payload = dict(state or {})
        with _task_db_lock, _task_db_connect() as conn:
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO task_states(task_id, state_json, updated_at) VALUES (?, ?, ?)",
                (str(task_id), json.dumps(payload, ensure_ascii=False), now),
            )
            if payload.get("status") in TERMINAL_STATES:
                conn.execute(
                    "INSERT OR REPLACE INTO task_history(task_id, state_json, updated_at, completed_at) VALUES (?, ?, ?, ?)",
                    (str(task_id), json.dumps(payload, ensure_ascii=False), now, payload.get("finish_time") or now),
                )
    except Exception as exc:
        log_warning(f"[{task_id}] 持久化任务状态失败: {exc}")


def _delete_persisted_task_state(task_id):
    if not _task_persistence_enabled():
        return
    try:
        with _task_db_lock, _task_db_connect() as conn:
            conn.execute("DELETE FROM task_states WHERE task_id = ?", (str(task_id),))
    except Exception as exc:
        log_warning(f"[{task_id}] 删除持久化任务状态失败: {exc}")


def _load_persisted_task_states():
    if not _task_persistence_enabled():
        return 0
    _migrate_legacy_task_state_files()
    try:
        with _task_db_lock, _task_db_connect() as conn:
            rows = list(conn.execute("SELECT task_id, state_json FROM task_states ORDER BY updated_at"))
    except Exception as exc:
        log_warning(f"读取 SQLite 任务状态失败: {exc}")
        return 0
    loaded = 0
    for task_id, state_json in rows:
        try:
            state = json.loads(state_json)
            if not isinstance(state, dict):
                continue
            if state.get("status") in {"submitting", "queued", "downloading"}:
                state["status"] = "error"
                state["error"] = "服务重启后任务已停止，等待自动恢复"
                state["speed"] = ""
                state["speed_bps"] = 0.0
                state["queue_position"] = None
                state["queue_size"] = 0
                state["finish_time"] = time.time()
            download_status[task_id] = state
            loaded += 1
        except Exception as exc:
            log_warning(f"[{task_id}] 读取持久化任务状态失败: {exc}")
    return loaded


def _count_persisted_task_states():
    try:
        with _task_db_lock, _task_db_connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0])
    except Exception:
        return 0


def backup_task_database():
    if not _task_persistence_enabled():
        return None
    os.makedirs(TASK_DB_BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    backup_path = os.path.join(TASK_DB_BACKUP_DIR, f"tasks-{stamp}.sqlite3")
    try:
        with _task_db_lock, _task_db_connect() as source:
            with sqlite3.connect(backup_path) as target:
                source.backup(target)
        cutoff = time.time() - TASK_DB_BACKUP_RETENTION_DAYS * 24 * 3600
        for name in os.listdir(TASK_DB_BACKUP_DIR):
            path = os.path.join(TASK_DB_BACKUP_DIR, name)
            if name.startswith("tasks-") and name.endswith(".sqlite3") and os.path.getmtime(path) < cutoff:
                os.remove(path)
        return backup_path
    except Exception as exc:
        log_warning(f"SQLite 自动备份失败: {exc}")
        return None


def run_task_database_backup_loop():
    while True:
        backup_task_database()
        time.sleep(24 * 3600)


def _query_task_history(status="", query="", page=1, per_page=30):
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 30)))
    query = str(query or "").strip().lower()
    try:
        with _task_db_lock, _task_db_connect() as conn:
            rows = list(conn.execute(
                "SELECT task_id, state_json FROM task_history ORDER BY completed_at DESC, updated_at DESC"
            ))
    except Exception as exc:
        log_warning(f"读取下载历史失败: {exc}")
        return [], 0
    with status_lock:
        live_items = [(task_id, dict(state)) for task_id, state in download_status.items()]
    items = []
    seen = set()
    for task_id, state in live_items:
        state["task_id"] = task_id
        if status and state.get("status") != status:
            continue
        haystack = " ".join(str(state.get(key, "")) for key in ("filename", "dialog_name", "downloader", "error")).lower()
        if query and query not in haystack and query not in task_id.lower():
            continue
        items.append(state)
        seen.add(task_id)
    for task_id, state_json in rows:
        if task_id in seen:
            continue
        try:
            state = json.loads(state_json)
        except Exception:
            continue
        state["task_id"] = task_id
        if status and state.get("status") != status:
            continue
        haystack = " ".join(str(state.get(key, "")) for key in ("filename", "dialog_name", "downloader", "error")).lower()
        if query and query not in haystack and query not in task_id.lower():
            continue
        items.append(state)
    start = (page - 1) * per_page
    return items[start:start + per_page], len(items)


def _remember_tdl_fallback_channel(entity_id, reason):
    if entity_id is None or not _task_persistence_enabled():
        return
    try:
        with _task_db_lock, _task_db_connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tdl_fallback_channels(entity_id, reason, updated_at) VALUES (?, ?, ?)",
                (str(int(entity_id)), str(reason or ""), time.time()),
            )
    except Exception as exc:
        log_warning(f"[{entity_id}] 保存 tdl 回退缓存失败: {exc}")


def _has_tdl_fallback_channel(entity_id):
    if entity_id is None or not _task_persistence_enabled():
        return False
    try:
        with _task_db_lock, _task_db_connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM tdl_fallback_channels WHERE entity_id = ?",
                (str(int(entity_id)),),
            ).fetchone()
        return bool(row)
    except Exception:
        return False


def _set_task_state(task_id, state):
    with status_lock:
        state = dict(state)
        state["updated_at"] = time.time()
        download_status[task_id] = state
        _persist_task_state(task_id, state)
        return dict(state)


def _update_task_state(task_id, **updates):
    with status_lock:
        state = download_status.get(task_id)
        if state is None:
            return None
        state.update(updates)
        state["updated_at"] = time.time()
        _persist_task_state(task_id, state)
        return dict(state)


def _drop_task_state(task_id):
    with status_lock:
        state = download_status.pop(task_id, None)
        _delete_persisted_task_state(task_id)
        return state


def _get_download_cancelled(task_id):
    with status_lock:
        return bool(download_cancel.get(task_id))


def _mark_download_cancelled(task_id):
    with status_lock:
        download_cancel[task_id] = True


def _clear_download_cancelled(task_id):
    with status_lock:
        download_cancel.pop(task_id, None)


def _calc_download_timeout(file_size_bytes):
    """根据文件大小动态计算超时时间，最低速率按 100KB/s 估算"""
    if not file_size_bytes or file_size_bytes <= 0:
        return 1800  # 默认30分钟
    seconds = max(600, int(file_size_bytes / (100 * 1024)) + 300)  # +5分钟余量
    return min(seconds, 43200)  # 上限12小时

# ==================== 下载队列系统 ====================
# `tdl` shares a single Bolt DB under `TDL_STORAGE_PATH`; concurrent runs fail with
# "Current database is used by another process".
MAX_CONCURRENT_DOWNLOADS = 1
download_queue = []
active_downloads = 0
scheduled_task_ids = set()
queue_lock = threading.RLock()
TASK_STALL_TIMEOUT = 600
TELEGRAM_CHUNK_TIMEOUT = 60
TELEGRAM_MAX_RETRY_ATTEMPTS = 12


def _is_task_queued_locked(task_id):
    return any(task.get("task_id") == task_id for task in download_queue)


def _recover_stalled_tasks(force=False, timeout=TASK_STALL_TIMEOUT):
    global active_downloads

    now = time.time()
    repaired = []
    release_count = 0

    with queue_lock:
        with status_lock:
            for task_id, state in list(download_status.items()):
                if state.get("status") in TERMINAL_STATES:
                    continue
                if _is_task_queued_locked(task_id):
                    continue
                if state.get("queue_position") not in (None, 0):
                    continue
                if state.get("speed_bps") not in (None, 0, 0.0):
                    continue

                updated_at = float(state.get("updated_at") or 0)
                if not force and (not updated_at or now - updated_at < timeout):
                    continue

                log_warning(f"检测到任务疑似卡住 [{task_id}]，正在释放槽位并停止任务")
                _mark_download_cancelled(task_id)
                state.update({
                    "status": "error",
                    "error": "任务疑似卡住，已释放下载槽位，请重试",
                    "speed": "",
                    "speed_bps": 0.0,
                    "queue_position": None,
                    "queue_size": 0,
                    "finish_time": now,
                    "updated_at": now,
                })
                was_scheduled = task_id in scheduled_task_ids
                scheduled_task_ids.discard(task_id)
                if was_scheduled:
                    active_downloads = max(0, active_downloads - 1)
                repaired.append(task_id)

    return repaired


async def _next_telegram_chunk(iterator, timeout=TELEGRAM_CHUNK_TIMEOUT):
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(f"Telegram 下载停滞超过 {timeout}s") from exc


def _should_retry_telegram_download_error(error_message):
    text = str(error_message or "").lower()
    transient_markers = (
        "expired",
        "file reference",
        "getfilerequest",
        "disconnected",
        "connection",
        "reset",
        "timed out",
        "timeout",
        "proxy",
        "eof",
        "temporarily",
        "server closed",
        "停滞",
        "超时",
        "连接",
    )
    return any(marker in text for marker in transient_markers)


def _update_queue_positions_locked():
    """刷新队列中任务的排位信息，需在持有 queue_lock 时调用"""
    queue_length = len(download_queue)
    with status_lock:
        for idx, task in enumerate(download_queue, start=1):
            tid = task.get("task_id")
            if not tid or tid not in download_status:
                continue
            state = download_status[tid]
            state["queue_position"] = idx
            state["queue_size"] = queue_length
            if state.get("status") not in TERMINAL_STATES and state.get("status") != "downloading":
                state["status"] = "queued"
            state["updated_at"] = time.time()
            _persist_task_state(tid, state)

RESUME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resume")
os.makedirs(RESUME_DIR, exist_ok=True)

def _get_resume_file(task_id):
    return os.path.join(RESUME_DIR, f"{task_id}.json")

def save_resume_info(task_id, info):
    try:
        with open(_get_resume_file(task_id), "w", encoding="utf-8") as f:
            json.dump(info, f)
    except: pass

def load_resume_info(task_id):
    try:
        path = _get_resume_file(task_id)
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except: pass
    return None

def clear_resume_info(task_id):
    try:
        path = _get_resume_file(task_id)
        if os.path.exists(path): os.remove(path)
    except: pass


def _restore_resume_tasks_into_memory():
    try:
        filenames = [name for name in os.listdir(RESUME_DIR) if name.endswith(".json")]
    except FileNotFoundError:
        return

    for name in filenames:
        task_id = name[:-5]
        if _copy_task_state(task_id):
            continue

        info = load_resume_info(task_id)
        if not info:
            continue

        downloaded_bytes = int(info.get("offset") or 0)
        total_bytes = int(info.get("total") or 0)
        progress = int(downloaded_bytes / total_bytes * 100) if total_bytes > 0 else 0
        _set_task_state(task_id, {
            "filename": info.get("filename") or os.path.basename(info.get("filepath") or task_id),
            "progress": min(progress, 99) if total_bytes and downloaded_bytes < total_bytes else progress,
            "status": "error",
            "downloaded": format_size(downloaded_bytes),
            "total": format_size(total_bytes) if total_bytes else "",
            "error": "服务重启后任务已停止，请重试继续下载",
            "speed": "",
            "msg_id": info.get("msg_id"),
            "entity_id": info.get("entity_id"),
            "dialog_name": info.get("dialog_name") or "",
            "downloaded_bytes": downloaded_bytes,
            "total_bytes": total_bytes,
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
            "downloader": info.get("downloader") or "",
        })

def add_to_queue(task):
    with queue_lock:
        task_id = task.get("task_id")
        if task_id:
            if task_id in scheduled_task_ids or any(item.get("task_id") == task_id for item in download_queue):
                _update_queue_positions_locked()
                return
            scheduled_task_ids.add(task_id)
        download_queue.append(task)
        _update_queue_positions_locked()

def get_next_from_queue():
    global active_downloads
    with queue_lock:
        if download_queue and active_downloads < MAX_CONCURRENT_DOWNLOADS:
            active_downloads += 1
            task = download_queue.pop(0)
            _update_queue_positions_locked()
            return task
    return None

def get_queue_status():
    with queue_lock:
        return {
            "active": active_downloads,
            "queued": len(download_queue),
            "max": MAX_CONCURRENT_DOWNLOADS,
        }

def process_queue():
    while True:
        task = get_next_from_queue()
        if not task: break
        threading.Thread(target=_do_download, args=([task], task.get("dialog_name", "unknown")), daemon=True).start()
        time.sleep(1)


def _resume_task(task_id, dialog_name=None, auto=False):
    eid, mid = _parse_task_id(task_id)
    stored = _copy_task_state(task_id) or {}
    resume_info = load_resume_info(task_id) or {}
    entity_id = stored.get("entity_id") if stored.get("entity_id") is not None else resume_info.get("entity_id", eid)
    msg_id = stored.get("msg_id") if stored.get("msg_id") is not None else resume_info.get("msg_id", mid)
    dialog_name = dialog_name or stored.get("dialog_name") or resume_info.get("dialog_name") or _last_download_dialog
    if task_id and stored.get("status") in {"submitting", "queued", "downloading"}:
        return {"ok": True, "task_id": task_id, "skipped": "already_active"}
    if not task_id or entity_id is None or msg_id is None:
        return {"ok": False, "error": "消息未找到"}

    info = None
    msg = None
    try:
        if auto:
            msg = _resolve_message(entity_id, msg_id, force_refresh=True)
        else:
            msg = _resolve_message(entity_id, msg_id)
        info = get_video_info(msg) if msg else None
    except Exception as exc:
        if not auto:
            raise
        log_warning(f"[{task_id}] 自动恢复时刷新消息失败，将入队后重试: {exc}")

    fname = (info or {}).get("filename") or resume_info.get("filename") or stored.get("filename") or "unknown"
    total_bytes = (info or {}).get("size") or int(resume_info.get("total") or stored.get("total_bytes") or 0)
    resume_path = _resolve_download_path(dialog_name or "", fname)
    resume_offset = _detect_resume_offset(task_id, resume_path, total_bytes)
    resume_progress = int(resume_offset / total_bytes * 100) if total_bytes and resume_offset else 0
    downloader = "tdl" if _supports_tdl_download(entity_id) else "telegram"

    _clear_download_cancelled(task_id)
    _set_task_state(task_id, {
        "filename": fname,
        "progress": min(resume_progress, 99) if total_bytes and resume_offset < total_bytes else resume_progress,
        "status": "submitting",
        "downloaded": format_size(resume_offset) if resume_offset else ("0B" if total_bytes else ""),
        "total": format_size(total_bytes) if total_bytes else stored.get("total", ""),
        "error": ("自动恢复中" if auto else "准备续传") + (f" {format_size(resume_offset)}" if resume_offset else ""),
        "speed": "",
        "msg_id": int(msg_id),
        "entity_id": int(entity_id),
        "dialog_name": dialog_name or "",
        "downloaded_bytes": resume_offset,
        "total_bytes": total_bytes,
        "speed_bps": 0.0,
        "queue_position": None,
        "queue_size": 0,
        "downloader": downloader,
    })
    queued_task_id = enqueue_download(task_id, entity_id, msg_id, dialog_name or "", info)
    return {"ok": True, "task_id": queued_task_id}


def _resume_all_incomplete_tasks(auto=False):
    _restore_resume_tasks_into_memory()
    with status_lock:
        candidates = [
            task_id for task_id, state in list(download_status.items())
            if state.get("status") in {"error", "cancelled", "paused"}
            and state.get("entity_id") is not None
            and state.get("msg_id") is not None
        ]
    submitted = []
    errors = {}
    for task_id in candidates:
        try:
            result = _resume_task(task_id, auto=auto)
            if result.get("ok"):
                submitted.append(task_id)
            else:
                errors[task_id] = result.get("error", "恢复失败")
        except Exception as exc:
            errors[task_id] = str(exc)
            _update_task_state(task_id, status="error", error=str(exc), finish_time=time.time())
    return {"submitted": submitted, "errors": errors}


def remove_from_queue(task_id):
    with queue_lock:
        for i, task in enumerate(download_queue):
            if task.get("task_id") == task_id:
                download_queue.pop(i)
                scheduled_task_ids.discard(task_id)
                _update_queue_positions_locked()
                return True
    return False


def move_queued_task(task_id, action):
    with queue_lock:
        index = next((i for i, item in enumerate(download_queue) if item.get("task_id") == task_id), None)
        if index is None:
            return False
        if action == "top":
            target = 0
        elif action == "up":
            target = max(0, index - 1)
        elif action == "down":
            target = min(len(download_queue) - 1, index + 1)
        else:
            return False
        item = download_queue.pop(index)
        download_queue.insert(target, item)
        _update_queue_positions_locked()
        return True


def _log_recovery_candidates(limit=200):
    log_path = os.path.join(LOG_DIR, "app.log")
    if not os.path.exists(log_path):
        return []
    failed = {}
    completed = set()
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-50000:]
    except Exception as exc:
        log_warning(f"读取历史日志失败: {exc}")
        return []
    for line in lines:
        completed_match = re.search(r"下载完成 \[(-?\d+:\d+)\]", line)
        if completed_match:
            completed.add(completed_match.group(1))
        failed_match = re.search(r"下载失败 \[(-?\d+:\d+)\] (.*?): (.*)$", line.strip())
        if not failed_match:
            continue
        task_id, filename, error = failed_match.groups()
        entity_id, msg_id = _parse_task_id(task_id)
        failed[task_id] = {
            "task_id": task_id,
            "entity_id": entity_id,
            "msg_id": msg_id,
            "filename": filename,
            "error": error,
        }
    with status_lock:
        existing = set(download_status)
    candidates = [item for task_id, item in reversed(list(failed.items())) if task_id not in completed and task_id not in existing]
    return candidates[:max(1, min(int(limit or 200), 500))]


THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
THUMB_MAX_AGE_SECONDS = 30 * 24 * 3600
THUMB_MAX_BYTES = 128 * 1024 * 1024

_reconnect_lock = threading.Lock()
_last_reconnect_attempt = 0.0
_relay_reconnect_lock = threading.Lock()
_last_relay_reconnect_attempt = 0.0
_health_cache = {"updated_at": 0.0, "payload": None}
_health_cache_lock = threading.Lock()


def cleanup_thumbnail_cache():
    try:
        now = time.time()
        entries = []
        total_bytes = 0
        for name in os.listdir(THUMB_DIR):
            path = os.path.join(THUMB_DIR, name)
            if not os.path.isfile(path):
                continue
            stat = os.stat(path)
            if now - stat.st_mtime > THUMB_MAX_AGE_SECONDS:
                os.remove(path)
                continue
            entries.append((stat.st_mtime, stat.st_size, path))
            total_bytes += stat.st_size
        for _, size, path in sorted(entries):
            if total_bytes <= THUMB_MAX_BYTES:
                break
            os.remove(path)
            total_bytes -= size
        log_info(f"缩略图缓存清理完成: {format_size(total_bytes)}")
    except Exception as exc:
        log_warning(f"缩略图缓存清理失败: {exc}")


def run_thumbnail_cleanup_loop():
    cleanup_thumbnail_cache()
    while True:
        time.sleep(6 * 3600)
        cleanup_thumbnail_cache()


def _format_user_display(me):
    username = getattr(me, "username", None)
    if username:
        return f"{me.first_name} (@{username})"
    return me.first_name


def _ensure_client_connection(
    *,
    client,
    loop,
    allow_reconnect,
    reconnect_lock,
    last_attempt_attr,
    connected_attr,
    error_attr,
    user_info_attr=None,
):
    if client.is_connected():
        globals()[connected_attr] = True
        if globals().get(error_attr, "").startswith("Telegram 已断开"):
            globals()[error_attr] = ""
        return True

    globals()[connected_attr] = False

    if not loop.is_running():
        globals()[error_attr] = "Telegram 客户端尚未启动，请稍后重试..."
        return False

    if not allow_reconnect:
        if not globals().get(error_attr):
            globals()[error_attr] = "Telegram 未连接，请等待重连..."
        return False

    now = time.time()
    if now - globals()[last_attempt_attr] < 8:
        if not globals().get(error_attr):
            globals()[error_attr] = "Telegram 重连中，请稍后重试..."
        return False

    with reconnect_lock:
        now = time.time()
        if client.is_connected():
            globals()[connected_attr] = True
            globals()[error_attr] = ""
            return True

        if now - globals()[last_attempt_attr] < 8:
            globals()[error_attr] = globals().get(error_attr) or "Telegram 重连中，请稍后重试..."
            return False

        globals()[last_attempt_attr] = now
        globals()[error_attr] = "Telegram 已断开，正在重连..."

        try:
            async def _reconnect():
                await client.connect()
                if not await client.is_user_authorized():
                    raise Exception("Telegram 未登录，请先运行 downloader.py 登录。")
                me = await client.get_me()
                return _format_user_display(me)

            user_display = asyncio.run_coroutine_threadsafe(_reconnect(), loop).result(timeout=45)
            globals()[connected_attr] = True
            globals()[error_attr] = ""
            if user_info_attr:
                globals()[user_info_attr] = user_display
            return True
        except Exception as e:
            globals()[connected_attr] = False
            globals()[error_attr] = f"Telegram 重连失败: {e}"
            return False


def ensure_tg_connection(allow_reconnect=True):
    return _ensure_client_connection(
        client=tg_client,
        loop=tg_loop,
        allow_reconnect=allow_reconnect,
        reconnect_lock=_reconnect_lock,
        last_attempt_attr="_last_reconnect_attempt",
        connected_attr="tg_connected",
        error_attr="tg_connect_error",
        user_info_attr="tg_user_info",
    )


def ensure_relay_connection(allow_reconnect=True):
    return _ensure_client_connection(
        client=relay_tg_client,
        loop=relay_loop,
        allow_reconnect=allow_reconnect,
        reconnect_lock=_relay_reconnect_lock,
        last_attempt_attr="_last_relay_reconnect_attempt",
        connected_attr="relay_connected",
        error_attr="relay_connect_error",
    )


def run_async(coro_factory, timeout=600, allow_reconnect=True):
    if not callable(coro_factory):
        raise TypeError("run_async expects a callable returning coroutine")

    if not ensure_tg_connection(allow_reconnect=allow_reconnect):
        raise Exception(tg_connect_error or "Telegram 未连接，请等待重连...")

    future = asyncio.run_coroutine_threadsafe(coro_factory(), tg_loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as e:
        future.cancel()
        raise RuntimeError(f"Telegram 操作超时（>{int(timeout)}s）") from e
    except Exception as e:
        msg = str(e).lower()
        if "disconnected" in msg or "connection reset" in msg or "could not connect to proxy" in msg:
            tg_connected = False
            tg_connect_error = f"Telegram 连接中断: {e}"
        raise


def relay_run_async(coro_factory, timeout=600, allow_reconnect=True):
    global relay_connected, relay_connect_error

    if not callable(coro_factory):
        raise TypeError("relay_run_async expects a callable returning coroutine")

    if not ensure_relay_connection(allow_reconnect=allow_reconnect):
        raise Exception(relay_connect_error or "Relay Telegram 未连接，请等待重连...")

    future = asyncio.run_coroutine_threadsafe(coro_factory(), relay_loop)
    try:
        return future.result(timeout=timeout)
    except FutureTimeoutError as e:
        future.cancel()
        raise RuntimeError(f"Relay Telegram 操作超时（>{int(timeout)}s）") from e
    except Exception as e:
        msg = str(e).lower()
        if "disconnected" in msg or "connection reset" in msg or "could not connect to proxy" in msg:
            relay_connected = False
            relay_connect_error = f"Relay Telegram 连接中断: {e}"
        raise


def _message_text(message):
    raw = getattr(message, "message", None) or getattr(message, "text", None) or ""
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _make_excerpt(text, limit=180):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def get_video_info(message):
    if not message.media or not isinstance(message.media, MessageMediaDocument):
        return None
    doc = message.media.document
    is_video = False
    filename = None
    duration = 0
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            is_video = True
            duration = attr.duration
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
    if not is_video:
        return None
    if not filename:
        filename = f"video_{message.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    message_text = _message_text(message)
    return {
        "id": message.id,
        "document_id": str(getattr(doc, "id", "")),
        "filename": filename,
        "size": doc.size,
        "duration": duration,
        "date": message.date.strftime("%Y-%m-%d %H:%M"),
        "has_thumb": bool(doc.thumbs),
        "text": message_text,
        "text_excerpt": _make_excerpt(message_text, 220),
        "reply_to_msg_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
    }


def format_size(size_bytes):
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def format_duration(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h > 0 else f"{m}:{s:02d}"


_dialogs_cache = []
_dialogs_serialized_cache = []
_dialogs_cache_updated_at = 0.0
_dialogs_refresh_in_progress = False
_dialogs_refresh_started_at = 0.0
_dialogs_refresh_error = ""
_messages_cache = {}
_current_entity_cache = {}
_videos_cache = {}
_replies_cache = {}
_last_download_dialog = ""

# 缓存大小上限，防止内存无限增长
MAX_DIALOG_CACHE_AGE = 300
DIALOGS_FETCH_MAX = 2000
MAX_MSG_CACHE_SIZE = 2000
MAX_VIDEO_CACHE_SIZE = 30
MAX_REPLY_CACHE_SIZE = 500


def _serialize_dialogs(dialogs):
    result = []
    for i, d in enumerate(dialogs):
        dtype = "频道" if d.is_channel else "群组" if d.is_group else "私聊"
        name = d.name
        is_saved = False
        try:
            if getattr(d.entity, "is_self", False):
                name = "⭐ 个人收藏 (Saved Messages)"
                is_saved = True
        except Exception:
            pass

        result.append({
            "index": i,
            "name": name,
            "id": d.id,
            "type": dtype,
            "is_channel": d.is_channel,
            "is_group": d.is_group,
            "is_saved": is_saved,
        })

    result.sort(key=lambda x: not x["is_saved"])
    return result


def _dialogs_cache_snapshot():
    with cache_lock:
        return {
            "dialogs": list(_dialogs_serialized_cache),
            "updated_at": _dialogs_cache_updated_at,
            "loading": _dialogs_refresh_in_progress,
            "error": _dialogs_refresh_error,
        }


def _set_dialogs_refresh_error(message):
    global _dialogs_refresh_error
    with cache_lock:
        _dialogs_refresh_error = message


async def _collect_dialogs():
    dialogs = []
    async for dialog in tg_client.iter_dialogs():
        dialogs.append(dialog)
        if len(dialogs) >= DIALOGS_FETCH_MAX:
            break
    return dialogs


def _refresh_dialogs_cache():
    global _dialogs_cache_updated_at, _dialogs_refresh_error, _dialogs_refresh_in_progress
    global _dialogs_refresh_started_at

    try:
        dialogs = run_async(_collect_dialogs, timeout=120)
        serialized = _serialize_dialogs(dialogs)
        with cache_lock:
            _dialogs_cache.clear()
            _dialogs_cache.extend(dialogs)
            _dialogs_serialized_cache[:] = serialized
            _dialogs_cache_updated_at = time.time()
            _dialogs_refresh_error = ""
    except TimeoutError:
        _set_dialogs_refresh_error("加载对话列表超时，请稍后重试")
    except Exception as e:
        _set_dialogs_refresh_error(str(e) or "加载对话列表失败")
    finally:
        with cache_lock:
            _dialogs_refresh_in_progress = False
            _dialogs_refresh_started_at = 0.0


def _kickoff_dialogs_refresh(force=False):
    global _dialogs_refresh_in_progress, _dialogs_refresh_started_at

    with cache_lock:
        cache_exists = bool(_dialogs_serialized_cache)
        cache_fresh = cache_exists and (time.time() - _dialogs_cache_updated_at) < MAX_DIALOG_CACHE_AGE
        if _dialogs_refresh_in_progress:
            return False
        if not force and cache_fresh and not _dialogs_refresh_error:
            return False
        _dialogs_refresh_in_progress = True
        _dialogs_refresh_started_at = time.time()

    threading.Thread(target=_refresh_dialogs_cache, daemon=True).start()
    return True


def _get_entity_id(entity):
    if not entity:
        return None
    return getattr(entity, "id", None)


def _message_entity_id(message, fallback_entity_id=None):
    if message is None:
        return fallback_entity_id
    return getattr(message, "chat_id", None) or fallback_entity_id


def _make_msg_cache_key(entity_id, msg_id):
    if entity_id is None or msg_id is None:
        return None
    return (int(entity_id), int(msg_id))


def _make_task_id(entity_id, msg_id):
    if entity_id is None or msg_id is None:
        return None
    return f"{int(entity_id)}:{int(msg_id)}"


def _cache_message(message, entity_id):
    key = _make_msg_cache_key(entity_id, getattr(message, "id", None))
    if not key:
        return
    with cache_lock:
        _messages_cache[key] = message
        if len(_messages_cache) > MAX_MSG_CACHE_SIZE:
            for _ in range(min(100, len(_messages_cache) - MAX_MSG_CACHE_SIZE + 50)):
                _messages_cache.pop(next(iter(_messages_cache)), None)


def _video_info_for_message(message, current_entity_id, source="主消息", extra=None):
    info = get_video_info(message)
    if not info:
        return None
    message_entity_id = _message_entity_id(message, current_entity_id)
    _cache_message(message, message_entity_id)
    info["entity_id"] = message_entity_id
    info["size_fmt"] = format_size(info["size"])
    info["duration_fmt"] = format_duration(info["duration"])
    info["source"] = source
    if extra:
        info.update(extra)
    return info


def _resolve_requested_entity(source="dialog", dialog_index=None, entity_id=None):
    entity = None
    name = "unknown"

    if source == "search":
        with cache_lock:
            entity = _current_entity_cache.get("search_entity")
            name = _current_entity_cache.get("search_name", "unknown")
        if entity is None and entity_id:
            entity = run_async(lambda: tg_client.get_entity(entity_id))
            name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity_id)
    elif dialog_index is not None:
        with cache_lock:
            if 0 <= dialog_index < len(_dialogs_cache):
                entity = _dialogs_cache[dialog_index].entity
                name = _dialogs_cache[dialog_index].name

    if entity is None and entity_id:
        entity = run_async(lambda: tg_client.get_entity(entity_id))
        name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or str(entity_id)

    return entity, name


def _parse_task_id(task_id):
    if not task_id or ":" not in task_id:
        return (None, None)
    left, right = task_id.split(":", 1)
    try:
        return int(left), int(right)
    except ValueError:
        return (None, None)


def _sanitize_dialog_name(dialog_name):
    return "".join(c if c.isalnum() or c in " _-" else "_" for c in (dialog_name or "unknown")).strip() or "unknown"


def _public_base_url():
    base_url = (PUBLIC_BASE_URL or "").strip()
    if base_url:
        return base_url.rstrip("/")
    return request.host_url.rstrip("/")


def _aria2_download_dir(dialog_name):
    return os.path.join(ARIA2_DOWNLOAD_DIR, _sanitize_dialog_name(dialog_name))


def _download_dir_for_dialog(dialog_name):
    return os.path.join(DOWNLOAD_DIR, _sanitize_dialog_name(dialog_name))


def _tdl_proxy_url():
    if not PROXY_CONFIG:
        return ""
    proxy_type, host, port = PROXY_CONFIG
    return f"{proxy_type}://{host}:{port}"


def _resolve_tdl_progress_path(filepath):
    tmp_path = filepath + ".tmp"
    tmp_exists = os.path.exists(tmp_path)
    final_exists = os.path.exists(filepath)
    if tmp_exists and final_exists:
        # Both exist: prefer the larger file (final file likely completed)
        if os.path.getsize(filepath) >= os.path.getsize(tmp_path):
            return filepath
        return tmp_path
    if tmp_exists:
        return tmp_path
    return filepath


def _prepare_telegram_fallback_target(filepath):
    tmp_path = filepath + ".tmp"
    if not os.path.exists(tmp_path):
        return filepath
    if os.path.exists(filepath):
        if os.path.getsize(filepath) >= os.path.getsize(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return filepath
        try:
            os.remove(filepath)
        except OSError:
            pass
    os.replace(tmp_path, filepath)
    return filepath


def _detect_resume_offset(task_id, filepath, total_bytes=0):
    progress_path = _resolve_tdl_progress_path(filepath)
    if os.path.exists(progress_path):
        size = os.path.getsize(progress_path)
        if size > 0 and (not total_bytes or size < total_bytes):
            return size

    resume_info = load_resume_info(task_id) or {}
    resume_offset = int(resume_info.get("offset") or 0)
    if resume_offset > 0 and (not total_bytes or resume_offset < total_bytes):
        return resume_offset

    if os.path.exists(filepath):
        size = os.path.getsize(filepath)
        if size > 0 and (not total_bytes or size < total_bytes):
            return size

    return 0


def _should_retry_tdl_error(error_message, retry_count, current_size=0, last_retry_size=0):
    category = _classify_tdl_error(error_message)
    if category not in {"eof", "timeout", "network"}:
        return False
    if retry_count >= min(TDL_MAX_EOF_RETRIES, TDL_MAX_RETRY_ATTEMPTS):
        return False
    if category == "eof" and last_retry_size > 0 and current_size <= last_retry_size:
        return retry_count < TDL_MAX_STALLED_EOF_RETRIES
    return True


def _should_fallback_from_tdl(error_message):
    text = (error_message or "").lower()
    return any(
        token in text
        for token in (
            "chat_id_invalid",
            "channel_invalid",
            "peer_id_invalid",
        )
    )


def _classify_tdl_error(error_message):
    text = (error_message or "").lower()
    if _should_fallback_from_tdl(text):
        return "fatal"
    if "未从断点续传" in text or "断点失效" in text:
        return "network"
    if "eof" not in text:
        if "timeout" in text or "i/o timeout" in text:
            return "timeout"
        if any(
            token in text
            for token in (
                "proxy",
                "connection reset",
                "connection refused",
                "connection aborted",
                "broken pipe",
                "context canceled",
                "context deadline exceeded",
                "transport is closing",
                "rpc error",
                "stream error",
                "read tcp",
                "dial tcp",
                "tls handshake timeout",
            )
        ):
            return "network"
        return "fatal"
    return "eof"


def _should_capture_tdl_error_line(line):
    text = (line or "").strip()
    if not text:
        return False
    if "\x1b[" in text:
        return False
    lowered = text.lower()
    if lowered.startswith("cpu: "):
        return False
    if "%]" in lowered or "; ~eta:" in lowered:
        return False
    return True


def _reconcile_tdl_progress_size(current_size, written, allow_offset_correction):
    if current_size < written and allow_offset_correction:
        return current_size, False
    if current_size < written:
        return written, allow_offset_correction
    return current_size, allow_offset_correction


def _did_tdl_restart_from_scratch(retry_count, previous_size, current_size, start_offset=0):
    effective_previous = previous_size if retry_count > 0 else start_offset
    if effective_previous <= 0 or current_size <= 0:
        return False
    if effective_previous < TDL_RESTART_RESET_MIN_BYTES:
        return False
    if current_size >= effective_previous:
        return False
    if effective_previous - current_size < TDL_RESTART_RESET_MIN_BYTES:
        return False
    return current_size < int(effective_previous * 0.9)


def _validate_tdl_completion(total_bytes, final_size):
    if total_bytes and final_size != total_bytes:
        return f"下载不完整：期望 {format_size(total_bytes)}，实际 {format_size(final_size)}"
    return None


def _stop_tdl_process(process, wait_timeout=3):
    if not process:
        return
    try:
        if process.poll() is not None:
            return
    except Exception:
        return

    try:
        process.terminate()
    except Exception:
        return

    try:
        process.wait(timeout=wait_timeout)
        return
    except Exception:
        pass

    try:
        if process.poll() is None:
            process.kill()
    except Exception:
        pass


def _clear_tdl_error(task_id):
    with tdl_lock:
        tdl_last_errors.pop(task_id, None)


def _error_priority(message):
    category = _classify_tdl_error(message)
    return {
        "eof": 3,
        "timeout": 3,
        "network": 2,
        "fatal": 1,
    }.get(category, 0)


def _choose_more_specific_tdl_error(current_message, candidate_message):
    candidate = (candidate_message or "").strip()
    if not candidate:
        return current_message
    current = (current_message or "").strip()
    if not current:
        return candidate
    if _error_priority(candidate) >= _error_priority(current):
        return candidate
    return current


def get_tdl_status():
    with tdl_lock:
        active = len([proc for proc in tdl_processes.values() if proc.poll() is None])
        error = list(tdl_last_errors.values())[-1] if tdl_last_errors else ""
    return {
        "binary": TDL_BINARY,
        "available": os.path.exists(TDL_BINARY),
        "namespace": TDL_NAMESPACE,
        "threads": TDL_THREADS,
        "limit": TDL_LIMIT,
        "active": active,
        "error": error,
    }


def build_tdl_message_url(entity_id, msg_id):
    if entity_id is None or msg_id is None:
        raise ValueError("缺少消息标识")
    if int(entity_id) >= 0:
        raise ValueError("暂不支持该对话类型的 tdl 直链下载")
    raw = str(int(entity_id))
    if not raw.startswith("-100"):
        raise ValueError("仅支持频道/超级群消息的 tdl 直链下载")
    dialog_id = raw[4:]
    return f"https://t.me/c/{dialog_id}/{int(msg_id)}"


def _supports_tdl_download(entity_id):
    if entity_id is None:
        return False
    return str(int(entity_id)).startswith("-100") and not _has_tdl_fallback_channel(entity_id)


def build_tdl_download_command(message_url, download_dir, output_name):
    command = [
        TDL_BINARY,
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
        TDL_NAMESPACE,
        "--storage",
        f"type=bolt,path={TDL_STORAGE_PATH}",
        "-t",
        str(TDL_THREADS),
        "-l",
        str(TDL_LIMIT),
    ]
    proxy_url = _tdl_proxy_url()
    if proxy_url:
        command.extend(["--proxy", proxy_url])
    return command


def _register_tdl_process(task_id, process):
    with tdl_lock:
        tdl_processes[task_id] = process


def _drop_tdl_process(task_id):
    with tdl_lock:
        return tdl_processes.pop(task_id, None)


def _get_tdl_process(task_id):
    with tdl_lock:
        return tdl_processes.get(task_id)


def _set_tdl_error(task_id, message):
    with tdl_lock:
        tdl_last_errors[task_id] = message


def enqueue_tdl_download(task_id, entity_id, msg_id, dialog_name, info):
    if not os.path.exists(TDL_BINARY):
        raise RuntimeError(f"tdl 不存在: {TDL_BINARY}")
    task = {
        "task_id": task_id,
        "entity_id": entity_id,
        "msg_id": msg_id,
        "dialog_name": dialog_name,
        "info": info,
    }
    add_to_queue(task)
    process_queue()
    return task_id


def enqueue_telegram_download(task_id, entity_id, msg_id, dialog_name, info):
    task = {
        "task_id": task_id,
        "entity_id": entity_id,
        "msg_id": msg_id,
        "dialog_name": dialog_name,
        "info": info,
        "downloader": "telegram",
    }
    add_to_queue(task)
    process_queue()
    return task_id


def enqueue_download(task_id, entity_id, msg_id, dialog_name, info):
    if _supports_tdl_download(entity_id):
        return enqueue_tdl_download(task_id, entity_id, msg_id, dialog_name, info)
    return enqueue_telegram_download(task_id, entity_id, msg_id, dialog_name, info)


def build_relay_url(entity_id, msg_id, file_name, base_url=None):
    expire_at = int(time.time()) + max(RELAY_TOKEN_TTL, 60)
    token = build_relay_token(
        secret=RELAY_TOKEN_SECRET,
        entity_id=entity_id,
        message_id=msg_id,
        file_name=file_name,
        expire_at=expire_at,
    )
    root = (base_url or _public_base_url()).rstrip("/")
    quoted_name = quote(file_name)
    return f"{root}/relay/{int(entity_id)}/{int(msg_id)}?file_name={quoted_name}&token={token}"


def _resolve_message(entity_id, msg_id, force_refresh=False):
    message = None if force_refresh else _get_cached_message(msg_id, entity_id)
    if message:
        return message
    message = run_async(lambda eid=entity_id, mid=msg_id: tg_client.get_messages(eid, ids=mid))
    if not message and entity_id is not None:
        entity = run_async(lambda eid=entity_id: tg_client.get_entity(eid))
        if entity is not None:
            message = run_async(lambda ent=entity, mid=msg_id: tg_client.get_messages(ent, ids=mid))
    key = _make_msg_cache_key(entity_id, msg_id)
    if key and message:
        with cache_lock:
            _messages_cache[key] = message
    return message


def _relay_session_file(session_name):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{session_name}.session")


def _recreate_relay_client_from_main_session():
    global relay_tg_client
    # Export current main session as a string
    try:
        session_string = StringSession.save(tg_client.session)
        # Directly update the session of the existing relay_tg_client
        # This prevents opening a new .session file and causing "Current database is used by another process"
        relay_tg_client = TelegramClient(
            StringSession(session_string),
            API_ID,
            API_HASH,
            loop=relay_loop,
            proxy=PROXY_CONFIG,
        )
        return relay_tg_client
    except Exception as e:
        log_error(f"同步 Relay Session 失败: {e}")
        raise


def _wait_for_main_tg_ready(timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tg_connected and tg_client.is_connected():
            return True
        time.sleep(0.5)
    return False


def _prepare_relay_session():
    src = _relay_session_file(SESSION_NAME)
    dst = _relay_session_file(RELAY_SESSION_NAME)
    if os.path.exists(dst) or not os.path.exists(src):
        return
    try:
        shutil.copy2(src, dst)
    except Exception as exc:
        log_warning(f"复制 relay session 失败: {exc}")


def _relay_resolve_message(entity_id, msg_id):
    # Use the main Telegram client for relay reads. Running a second client with
    # a cloned auth key causes Telethon "wrong session ID" security warnings.
    return _resolve_message(entity_id, msg_id, force_refresh=True)


def submit_download_to_aria2(task_id, entity_id, msg_id, dialog_name, info):
    if not RELAY_TOKEN_SECRET:
        raise RuntimeError("RELAY_TOKEN_SECRET 未配置")
    relay_url = build_relay_url(entity_id, msg_id, info["filename"])
    gid = aria2.add_uri(
        relay_url=relay_url,
        out=info["filename"],
        download_dir=_aria2_download_dir(dialog_name),
    )
    _update_task_state(
        task_id,
        status="submitted",
        gid=gid,
        speed="",
        speed_bps=0.0,
        queue_position=None,
        queue_size=0,
    )
    return gid


def fetch_aria2_status(gid):
    return aria2.tell_status(gid)


def _map_aria2_status(task_id, aria2_status):
    total_bytes = int(aria2_status.get("totalLength", "0") or 0)
    downloaded_bytes = int(aria2_status.get("completedLength", "0") or 0)
    speed_bps = float(aria2_status.get("downloadSpeed", "0") or 0)
    status = aria2_status.get("status", "")
    if total_bytes > 0:
        progress = min(int(downloaded_bytes * 100 / total_bytes), 100)
    else:
        progress = 0

    mapped = {
        "active": "downloading",
        "waiting": "queued",
        "paused": "queued",
        "complete": "done",
        "error": "error",
        "removed": "cancelled",
    }.get(status, "submitted")

    updates = {
        "status": mapped,
        "progress": progress,
        "downloaded_bytes": downloaded_bytes,
        "total_bytes": total_bytes or (_copy_task_state(task_id) or {}).get("total_bytes", 0),
        "downloaded": format_size(downloaded_bytes) if downloaded_bytes else "0B",
        "total": format_size(total_bytes) if total_bytes else (_copy_task_state(task_id) or {}).get("total", ""),
        "speed_bps": speed_bps,
        "speed": f"{format_size(speed_bps)}/s" if speed_bps else "",
        "error": aria2_status.get("errorMessage", "") or "",
        "queue_position": None,
        "queue_size": 0,
    }
    if mapped in TERMINAL_STATES:
        updates["finish_time"] = time.time()
    _update_task_state(task_id, **updates)


def get_relay_media(entity_id, msg_id):
    message = _relay_resolve_message(entity_id, msg_id)
    info = get_video_info(message) if message else None
    if not message or not info:
        raise FileNotFoundError("消息不包含可下载视频")
    mime_type = getattr(message.media.document, "mime_type", None) or "application/octet-stream"
    return {
        "message": message,
        "size": info.get("size") or 0,
        "file_name": info["filename"],
        "mime_type": mime_type,
        "chunk_size": 512 * 1024,
    }


def _parse_range(range_header, total_size):
    relay_chunk_size = 4 * 1024 * 1024
    if not range_header:
        end = min(relay_chunk_size - 1, max(total_size - 1, 0))
        return 0, end, 206
    if not range_header.startswith("bytes=") or "," in range_header:
        raise ValueError("invalid range")
    raw = range_header[6:]
    start_str, end_str = raw.split("-", 1)
    if start_str == "":
        length = int(end_str)
        if length <= 0:
            raise ValueError("invalid range")
        start = max(total_size - length, 0)
        end = total_size - 1
    else:
        start = int(start_str)
        end = min(start + relay_chunk_size - 1, total_size - 1) if end_str == "" else int(end_str)
    if start < 0 or end < start or start >= total_size:
        raise ValueError("invalid range")
    return start, min(end, total_size - 1), 206


def iter_relay_bytes(media, start_offset, end_offset):
    data_queue = queue.Queue(maxsize=8)
    sentinel = object()
    relay_label = f"relay:{media.get('file_name', 'unknown')}:{start_offset}-{end_offset}"

    async def _producer():
        remaining = end_offset - start_offset + 1
        emitted = 0
        request_size = int(media.get("chunk_size") or 512 * 1024)
        aligned_offset = (start_offset // request_size) * request_size
        skip_bytes = start_offset - aligned_offset
        async for chunk in tg_client.iter_download(
            media["message"].media.document,
            offset=aligned_offset,
            file_size=media["size"],
            request_size=request_size,
        ):
            if remaining <= 0:
                break
            if skip_bytes:
                if skip_bytes >= len(chunk):
                    skip_bytes -= len(chunk)
                    continue
                chunk = chunk[skip_bytes:]
                skip_bytes = 0
            piece = bytes(chunk[:remaining])
            if not piece:
                continue
            data_queue.put(piece)
            remaining -= len(piece)
            emitted += len(piece)
            if emitted == len(piece):
                log_info(f"[{relay_label}] first chunk={len(piece)} bytes")
            if remaining <= 0:
                break
        log_info(f"[{relay_label}] producer completed emitted={emitted}")

    def _runner():
        try:
            log_info(f"[{relay_label}] stream start size={media.get('size', 0)}")
            run_async(lambda: _producer(), timeout=_calc_download_timeout(end_offset - start_offset + 1), allow_reconnect=True)
        except Exception as exc:
            log_error(f"[{relay_label}] stream failed: {exc}")
            data_queue.put(exc)
        finally:
            data_queue.put(sentinel)

    threading.Thread(target=_runner, daemon=True).start()
    while True:
        try:
            item = data_queue.get(timeout=75)
        except queue.Empty:
            raise RuntimeError("Relay stream stalled waiting for Telegram data")
        if item is sentinel:
            break
        if isinstance(item, Exception):
            raise item
        yield item


def _get_cached_message(msg_id, entity_id=None):
    """根据 msg_id/对话 ID 从缓存里取消息，避免 ID 冲突"""
    with cache_lock:
        key = _make_msg_cache_key(entity_id, msg_id)
        if key and key in _messages_cache:
            return _messages_cache[key]
        if entity_id is None:
            last_eid = _current_entity_cache.get("entity_id")
            key = _make_msg_cache_key(last_eid, msg_id)
            if key and key in _messages_cache:
                return _messages_cache[key]
        for (eid, mid), message in list(_messages_cache.items()):
            if mid == msg_id:
                return message
    return None


def _cache_key(entity_id, limit, include_replies, reply_post_limit=0):
    return f"{entity_id}:{limit}:{include_replies}:{reply_post_limit}"


def _reply_cache_key(entity_id, post_id, limit):
    return f"{entity_id}:{post_id}:{limit}"



@app.before_request
def enforce_access_control():
    if request.path.startswith("/relay/"):
        return None
    auth_error = _require_web_auth()
    if auth_error is not None:
        return auth_error


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    folder = (request.json or {}).get("folder", "")
    if not folder:
        return jsonify({"error": "Missing folder"}), 400
    try:
        path = _resolve_download_path(folder, must_exist=True)
        if not os.path.isdir(path):
            return jsonify({"error": "Folder not found"}), 404
        if not OPEN_FOLDER_ENABLED:
            return jsonify({
                "ok": False,
                "path": path,
                "error": "服务器目录打开功能已禁用，下面是服务器目录路径",
            }), 409
        if not _request_ip_is_local():
            return jsonify({
                "ok": False,
                "path": path,
                "error": "浏览器不在服务器本机，无法直接打开服务器目录",
            }), 409
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return jsonify({"ok": True, "path": path})
    except FileNotFoundError:
        return jsonify({"error": "Folder not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/rename-file", methods=["POST"])
def api_rename_file():
    data = request.json or {}
    folder, old_name, new_name = data.get("folder"), data.get("old_name"), data.get("new_name")
    if not all([folder, old_name, new_name]):
        return jsonify({"error": "Missing parameters"}), 400
    if os.path.basename(new_name) != new_name:
        return jsonify({"error": "非法文件名"}), 400
    try:
        old_path = _resolve_download_path(folder, old_name, must_exist=True)
        new_path = _resolve_download_path(folder, new_name)
        if os.path.exists(new_path):
            return jsonify({"error": "Target file name already exists"}), 400
        os.rename(old_path, new_path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/delete-file", methods=["POST"])
def api_delete_file():
    data = request.json or {}
    folder, filename = data.get("folder"), data.get("filename")
    if not all([folder, filename]):
        return jsonify({"error": "Missing parameters"}), 400
    try:
        path = _resolve_download_path(folder, filename, must_exist=True)
        if not os.path.isfile(path):
            return jsonify({"error": "File not found"}), 404
        os.remove(path)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/status")
def api_status():
    ensure_tg_connection(allow_reconnect=True)
    return jsonify({
        "connected": tg_connected,
        "error": tg_connect_error,
        "user": tg_user_info,
        "queue": get_queue_status(),
        "tdl": get_tdl_status(),
    })


def _proxy_status():
    if not PROXY_CONFIG:
        return {"enabled": False, "ok": True, "label": "未启用"}
    proxy_type, host, port = PROXY_CONFIG
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


def _tdl_version_summary():
    try:
        output = subprocess.check_output([TDL_BINARY, "version"], text=True, stderr=subprocess.STDOUT, timeout=5)
        first = next((line.strip() for line in output.splitlines() if line.strip()), "")
        return {"ok": True, "version": first, "binary": TDL_BINARY}
    except Exception as exc:
        return {"ok": False, "binary": TDL_BINARY, "error": str(exc)}


@app.route("/api/health")
def api_health():
    now = time.time()
    with _health_cache_lock:
        cached = _health_cache.get("payload")
        if cached and now - float(_health_cache.get("updated_at") or 0) < 5:
            return jsonify(cached)
    payload = {
        "ok": bool(tg_connected),
        "telegram": {
            "connected": bool(tg_connected),
            "user": tg_user_info,
            "error": tg_connect_error,
        },
        "relay": {
            "connected": bool(tg_connected),
            "error": tg_connect_error,
            "mode": "main-client",
            "active": active_relays if "active_relays" in globals() else 0,
            "max": MAX_CONCURRENT_RELAYS if "MAX_CONCURRENT_RELAYS" in globals() else 0,
        },
        "proxy": _proxy_status(),
        "tdl": _tdl_version_summary(),
        "queue": get_queue_status(),
        "tasks_persisted": _count_persisted_task_states(),
        "resume_files": len([name for name in os.listdir(RESUME_DIR) if name.endswith(".json")]) if os.path.isdir(RESUME_DIR) else 0,
    }
    with _health_cache_lock:
        _health_cache["updated_at"] = now
        _health_cache["payload"] = payload
    return jsonify(payload)


@app.route("/api/dialogs")
def api_dialogs():
    force_refresh = request.args.get("refresh", "false") == "true"
    started_refresh = _kickoff_dialogs_refresh(force=force_refresh)
    snapshot = _dialogs_cache_snapshot()

    if snapshot["dialogs"]:
        return jsonify({
            "dialogs": snapshot["dialogs"],
            "cached": True,
            "loading": snapshot["loading"],
            "error": "",
            "updated_at": snapshot["updated_at"],
        })

    if snapshot["loading"] or started_refresh:
        return jsonify({
            "dialogs": [],
            "cached": False,
            "loading": True,
            "error": "",
            "updated_at": snapshot["updated_at"],
        }), 202

    if snapshot["error"]:
        return jsonify({
            "dialogs": [],
            "cached": False,
            "loading": False,
            "error": snapshot["error"],
            "updated_at": snapshot["updated_at"],
        }), 503

    return jsonify({
        "dialogs": [],
        "cached": False,
        "loading": False,
        "error": "对话列表暂不可用，请稍后重试",
        "updated_at": snapshot["updated_at"],
    }), 503


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "请输入搜索内容"}), 400
        
    # 智能链接嗅探解析
    if "t.me/" in query:
        m = re.search(r't\.me/(?:c/)?([^/\s\?]+)', query)
        if m:
            val = m.group(1)
            # 如果是纯数字（私密频道的 ID）
            if val.isdigit():
                query = int("-100" + val)
            elif val.startswith("+") or val == "joinchat":
                return jsonify({"error": "暂不支持直接嗅探私密邀请链接，请先加入群组"}), 400
            else:
                query = val

    try:
        entity = run_async(lambda: tg_client.get_entity(query))
        name = getattr(entity, "title", None) or getattr(entity, "first_name", str(query))
        with cache_lock:
            _current_entity_cache["search_entity"] = entity
            _current_entity_cache["search_name"] = name
        return jsonify({"name": name, "id": getattr(entity, "id", 0), "source": "search"})
    except Exception as e:
        return jsonify({"error": f"解析失败: {str(e)}"}), 500




@app.route("/api/debug")
def api_debug():
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error
    dialog_index = request.args.get("dialog_index", type=int)
    if dialog_index is not None:
        with cache_lock:
            entity = _dialogs_cache[dialog_index].entity if dialog_index < len(_dialogs_cache) else None
    else:
        entity = None
    if entity is None:
        return jsonify({"error": "invalid dialog_index"})
        
    async def scan():
        results = []
        async for msg in tg_client.iter_messages(entity, limit=20):
            item = {"id": msg.id, "text": msg.text[:50] if msg.text else None, "media_type": None, "doc_mime": None, "doc_size": None, "attrs": []}
            if msg.media:
                item["media_type"] = type(msg.media).__name__
                if hasattr(msg.media, "document"):
                    doc = msg.media.document
                    item["doc_mime"] = doc.mime_type
                    item["doc_size"] = doc.size
                    item["attrs"] = [type(a).__name__ for a in doc.attributes]
                    from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo
                    item["attr_details"] = []
                    for a in doc.attributes:
                        if isinstance(a, DocumentAttributeFilename): item["attr_details"].append({"file_name": a.file_name})
                        elif isinstance(a, DocumentAttributeVideo): item["attr_details"].append({"video_duration": a.duration})
            results.append(item)
        return results

    try:
        res = run_async(scan)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/debug_replies")
def api_debug_replies():
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error
    dialog_index = request.args.get("dialog_index", type=int)
    post_id = request.args.get("post_id", type=int)
    if dialog_index is not None:
        with cache_lock:
            entity = _dialogs_cache[dialog_index].entity if dialog_index < len(_dialogs_cache) else None
    else:
        entity = None
    if entity is None:
        return jsonify({"error": "invalid dialog_index"})
        
    async def scan():
        results = []
        async for msg in tg_client.iter_messages(entity, reply_to=post_id, limit=20):
            item = {"id": msg.id, "text": msg.text[:50] if msg.text else None, "media_type": None, "doc_mime": None, "doc_size": None, "attrs": []}
            if msg.media:
                item["media_type"] = type(msg.media).__name__
                if hasattr(msg.media, "document"):
                    doc = msg.media.document
                    item["doc_mime"] = doc.mime_type
                    item["doc_size"] = doc.size
                    item["attrs"] = [type(a).__name__ for a in doc.attributes]
            results.append(item)
        return results

    try:
        res = run_async(scan)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)})



@app.route("/api/debug_full")
def api_debug_full():
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error
    dialog_index = request.args.get("dialog_index", type=int)
    if dialog_index is not None:
        with cache_lock:
            entity = _dialogs_cache[dialog_index].entity if dialog_index < len(_dialogs_cache) else None
    else:
        entity = None
    if entity is None:
        return jsonify({"error": "invalid dialog_index"})
        
    async def scan():
        results = []
        async for msg in tg_client.iter_messages(entity, limit=5):
            reply_markup = None
            if hasattr(msg, "reply_markup") and msg.reply_markup:
                reply_markup = str(msg.reply_markup)
            results.append({
                "id": msg.id,
                "text": msg.text,
                "reply_markup": reply_markup,
                "entities": [str(e) for e in (msg.entities or [])]
            })
        return results

    try:
        res = run_async(scan)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)})

# === 插入位置 ===

@app.route("/api/videos")
def api_videos():
    dialog_index = request.args.get("dialog_index", type=int)
    entity_id = request.args.get("entity_id", type=int)
    source = request.args.get("source", "dialog")
    limit = request.args.get("limit", 100, type=int)
    include_replies = request.args.get("include_replies", "false") == "true"
    reply_post_limit = min(max(request.args.get("reply_post_limit", 50, type=int), 0), 500)
    refresh = request.args.get("refresh", "false") == "true"

    try:
        entity, name = _resolve_requested_entity(source, dialog_index, entity_id)
        if entity is None:
            return jsonify({"error": "无效的对话"}), 400

        with cache_lock:
            _current_entity_cache["entity"] = entity
            _current_entity_cache["name"] = name

        eid = getattr(entity, "id", entity_id)
        if eid is None:
            return jsonify({"error": "无法确定对话 ID"}), 400
        current_entity_id = entity_id if entity_id is not None else eid

        with cache_lock:
            _current_entity_cache["entity_id"] = current_entity_id
            cached_videos = _videos_cache.get(_cache_key(current_entity_id, limit, include_replies, reply_post_limit))

        if not refresh and cached_videos:
            return jsonify({
                "videos": cached_videos.get("videos", []),
                "posts_with_replies": cached_videos.get("posts_with_replies", []),
                "cached": True,
            })

        async def scan():
            videos = []
            posts_with_replies = []
            async for message in tg_client.iter_messages(entity, limit=limit):
                info = _video_info_for_message(message, current_entity_id)
                if info:
                    videos.append(info)
                if (
                    include_replies
                    and len(posts_with_replies) < reply_post_limit
                    and message.replies
                    and message.replies.replies > 0
                ):
                    posts_with_replies.append({
                        "id": message.id,
                        "count": message.replies.replies,
                        "text_excerpt": _make_excerpt(_message_text(message), 220),
                    })
            return videos, posts_with_replies

        videos, posts_with_replies = run_async(scan)
        with cache_lock:
            _videos_cache[_cache_key(current_entity_id, limit, include_replies, reply_post_limit)] = {
                "videos": videos,
                "posts_with_replies": posts_with_replies,
                "time": time.time(),
            }
            while len(_videos_cache) > MAX_VIDEO_CACHE_SIZE:
                _videos_cache.pop(next(iter(_videos_cache)))
        return jsonify({
            "videos": videos,
            "posts_with_replies": posts_with_replies if include_replies else [],
            "entity_id": current_entity_id,
            "reply_post_limit": reply_post_limit,
            "cached": False,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/video_search")
def api_video_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "请输入要搜索的文件名或关键词"}), 400

    dialog_index = request.args.get("dialog_index", type=int)
    entity_id = request.args.get("entity_id", type=int)
    source = request.args.get("source", "dialog")
    limit = min(max(request.args.get("limit", 200, type=int), 10), 1000)
    scan_limit = min(max(request.args.get("scan_limit", 1000, type=int), limit), 5000)
    include_comments = request.args.get("include_comments", "true") == "true"
    comment_post_limit = min(max(request.args.get("comment_post_limit", 80, type=int), 0), 300)
    comment_limit = min(max(request.args.get("comment_limit", 100, type=int), 10), 300)

    try:
        entity, name = _resolve_requested_entity(source, dialog_index, entity_id)
        if entity is None:
            return jsonify({"error": "无效的对话"}), 400

        with cache_lock:
            _current_entity_cache["entity"] = entity
            _current_entity_cache["name"] = name

        eid = getattr(entity, "id", entity_id)
        if eid is None:
            return jsonify({"error": "无法确定对话 ID"}), 400
        current_entity_id = entity_id if entity_id is not None else eid

        with cache_lock:
            _current_entity_cache["entity_id"] = current_entity_id

        keyword = query.lower()

        def matches_video(item):
            haystack = " ".join([
                item.get("filename") or "",
                item.get("text") or "",
                item.get("text_excerpt") or "",
                item.get("parent_text_excerpt") or "",
            ]).lower()
            return keyword in haystack

        async def search_channel():
            found = {}
            telegram_hits = 0
            scanned = 0
            comment_posts = []
            comments_scanned = 0
            comment_hits = 0

            async for message in tg_client.iter_messages(entity, search=query, limit=limit):
                info = _video_info_for_message(message, current_entity_id, "频道搜索")
                if info:
                    key = (int(info["entity_id"]), int(info["id"]))
                    found[key] = info
                    telegram_hits += 1

            async for message in tg_client.iter_messages(entity, limit=scan_limit):
                scanned += 1
                parent_text = _message_text(message)
                parent_matched = keyword in parent_text.lower()
                info = _video_info_for_message(message, current_entity_id, "文件名匹配")
                if info and matches_video(info):
                    key = (int(info["entity_id"]), int(info["id"]))
                    found[key] = info
                if (
                    include_comments
                    and len(comment_posts) < comment_post_limit
                    and getattr(message, "replies", None)
                    and message.replies.replies > 0
                ):
                    comment_posts.append({
                        "id": message.id,
                        "text": parent_text,
                        "excerpt": _make_excerpt(parent_text, 260),
                        "matched": parent_matched,
                    })

            if include_comments:
                for post in comment_posts:
                    try:
                        async for reply in tg_client.iter_messages(entity, reply_to=post["id"], limit=comment_limit):
                            comments_scanned += 1
                            reply_text = _message_text(reply)
                            reply_matched = keyword in reply_text.lower()
                            info = _video_info_for_message(
                                reply,
                                current_entity_id,
                                "评论搜索",
                                {
                                    "parent_post_id": post["id"],
                                    "parent_text": post["text"],
                                    "parent_text_excerpt": post["excerpt"],
                                },
                            )
                            if not info:
                                continue
                            if post["matched"] or reply_matched or matches_video(info):
                                if reply_matched or matches_video(info):
                                    comment_hits += 1
                                if post["matched"] and not reply_matched and not matches_video(info):
                                    info["source"] = f"主帖标签匹配@帖子{post['id']}"
                                else:
                                    info["source"] = f"评论匹配@帖子{post['id']}"
                                key = (int(info["entity_id"]), int(info["id"]))
                                found[key] = info
                    except Exception as e:
                        print(f"搜索帖子 {post['id']} 评论失败: {e}")

            return list(found.values()), telegram_hits, scanned, comments_scanned, comment_hits

        videos, telegram_hits, scanned, comments_scanned, comment_hits = run_async(search_channel)
        videos.sort(key=lambda item: item.get("date", ""), reverse=True)
        return jsonify({
            "videos": videos,
            "entity_id": current_entity_id,
            "query": query,
            "telegram_hits": telegram_hits,
            "scanned": scanned,
            "comments_scanned": comments_scanned,
            "comment_hits": comment_hits,
            "limit": limit,
            "scan_limit": scan_limit,
            "include_comments": include_comments,
            "comment_post_limit": comment_post_limit,
            "comment_limit": comment_limit,
            "cached": False,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies")
def api_replies():
    entity_id = request.args.get("entity_id", type=int)
    post_id = request.args.get("post_id", type=int)
    limit = min(max(request.args.get("limit", 100, type=int), 10), 300)
    refresh = request.args.get("refresh", "false") == "true"
    if not entity_id or not post_id:
        return jsonify({"error": "缺少参数"}), 400
    
    try:
        cache_key = _reply_cache_key(entity_id, post_id, limit)
        with cache_lock:
            cached = _replies_cache.get(cache_key)
        if cached and not refresh:
            return jsonify({"videos": cached.get("videos", []), "cached": True})

        async def scan_one_post_replies():
            with cache_lock:
                entity = _current_entity_cache.get("entity")
            if not entity or getattr(entity, "id", 0) != entity_id:
                entity = await tg_client.get_entity(entity_id)

            parent_message = await tg_client.get_messages(entity, ids=post_id)
            parent_text = _message_text(parent_message) if parent_message else ""
            parent_excerpt = _make_excerpt(parent_text, 260)

            rv = []
            try:
                async for reply in tg_client.iter_messages(entity, reply_to=post_id, limit=limit):
                    ri = _video_info_for_message(
                        reply,
                        entity_id,
                        f"评论@帖子{post_id}",
                        {
                            "parent_post_id": post_id,
                            "parent_text": parent_text,
                            "parent_text_excerpt": parent_excerpt,
                        },
                    )
                    if ri:
                        rv.append(ri)
            except Exception as e:
                print(f"扫描帖子 {post_id} 评论失败: {e}")
            return rv

        replies_videos = run_async(scan_one_post_replies)
        with cache_lock:
            _replies_cache[cache_key] = {"videos": replies_videos, "time": time.time()}
            while len(_replies_cache) > MAX_REPLY_CACHE_SIZE:
                _replies_cache.pop(next(iter(_replies_cache)), None)
        return jsonify({"videos": replies_videos, "cached": False})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumb/<int:msg_id>")
def api_thumb(msg_id):
    entity_id = request.args.get("entity", type=int)
    thumb_key = f"{entity_id}_{msg_id}" if entity_id is not None else f"unknown_{msg_id}"
    thumb_path = os.path.join(THUMB_DIR, f"{thumb_key}.jpg")
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype="image/jpeg")
    message = _get_cached_message(msg_id, entity_id)
    if not message:
        return Response(status=404)
    try:
        data = run_async(lambda: tg_client.download_media(message, file=bytes, thumb=-1), allow_reconnect=False)
        if not data:
            return Response(status=404)
        with open(thumb_path, "wb") as f:
            f.write(data)
        return Response(data, mimetype="image/jpeg")
    except Exception:
        return Response(status=404)


@app.route("/api/online-play-url")
def api_online_play_url():
    entity_id = request.args.get("entity_id", type=int)
    msg_id = request.args.get("msg_id", type=int)
    file_name = request.args.get("filename", "").strip()
    if entity_id is None or msg_id is None:
        return jsonify({"error": "缺少消息标识"}), 400
    if not RELAY_TOKEN_SECRET:
        return jsonify({"error": "Relay 未配置，无法在线播放"}), 503
    try:
        if not file_name:
            message = _resolve_message(entity_id, msg_id, force_refresh=True)
            info = get_video_info(message) if message else None
            if not info:
                return jsonify({"error": "消息不包含可播放视频"}), 404
            file_name = info["filename"]
        return jsonify({
            "ok": True,
            "url": build_relay_url(entity_id, msg_id, file_name, base_url=request.host_url.rstrip("/")),
            "filename": file_name,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json or {}
    message_ids = data.get("message_ids", [])
    dialog_name = data.get("dialog_name", "unknown")
    entity_id = data.get("entity_id") or _current_entity_cache.get("entity_id")
    if not message_ids:
        return jsonify({"error": "参数不完整"}), 400
    if entity_id is None:
        return jsonify({"error": "缺少对话 ID"}), 400
    tasks = []
    task_ids = {}
    errors = []
    for mid in message_ids:
        task_id = _make_task_id(entity_id, mid)
        if not task_id:
            continue

        existing_state = _copy_task_state(task_id)
        if existing_state and existing_state.get("status") not in TERMINAL_STATES:
            continue
        msg = _get_cached_message(mid, entity_id)
        if not msg:
            try:
                msg = _resolve_message(entity_id, mid)
            except Exception:
                msg = None
        fname = "unknown"
        info = None
        if msg:
            info = get_video_info(msg)
            if info:
                fname = info["filename"]
        total_bytes = info.get("size") if info else 0
        _set_task_state(task_id, {
            "filename": fname,
            "progress": 0,
            "status": "submitting",
            "downloaded": "0B" if total_bytes else "",
            "total": format_size(total_bytes) if total_bytes else "",
            "error": "",
            "speed": "",
            "msg_id": mid,
            "entity_id": entity_id,
            "dialog_name": dialog_name,
            "downloaded_bytes": 0,
            "total_bytes": total_bytes,
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
            "downloader": "tdl" if _supports_tdl_download(entity_id) else "telegram",
        })
        _clear_download_cancelled(task_id)
        tasks.append((task_id, mid, info))

    if not tasks:
        return jsonify({"error": "未找到可下载的消息，请刷新后重试"}), 400

    global _last_download_dialog
    _last_download_dialog = dialog_name
    for task_id, mid, info in tasks:
        try:
            task_ids[task_id] = enqueue_download(task_id, entity_id, mid, dialog_name, info)
        except Exception as exc:
            errors.append(str(exc))
            _update_task_state(
                task_id,
                status="error",
                error=str(exc),
                finish_time=time.time(),
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
            )

    if not task_ids and errors:
        return jsonify({"error": errors[0]}), 502

    return jsonify({
        "status": "submitted",
        "count": len(tasks),
        "task_ids": task_ids,
        "errors": errors,
    })


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    data = request.json or {}
    task_id = data.get("task_id")
    entity_id = data.get("entity_id")
    msg_id = data.get("msg_id")
    if not task_id and msg_id is not None and entity_id is not None:
        task_id = _make_task_id(entity_id, msg_id)
    if task_id:
        _mark_download_cancelled(task_id)
        remove_from_queue(task_id)
        state = _copy_task_state(task_id) or {}
        proc = _get_tdl_process(task_id)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception as exc:
                log_warning(f"[{task_id}] tdl cancel failed: {exc}")
        _update_task_state(
            task_id,
            status="cancelled",
            error="已取消",
            speed="",
            speed_bps=0.0,
            queue_position=None,
            queue_size=0,
        )
    return jsonify({"ok": True})


@app.route("/api/retry", methods=["POST"])
def api_retry():
    _recover_stalled_tasks()
    data = request.json or {}
    task_id = data.get("task_id")
    dialog_name = data.get("dialog_name") or _last_download_dialog
    entity_id = data.get("entity_id")
    msg_id = data.get("msg_id")

    if task_id:
        eid, mid = _parse_task_id(task_id)
        if entity_id is None:
            entity_id = eid
        if msg_id is None:
            msg_id = mid
    elif msg_id is not None and entity_id is not None:
        task_id = _make_task_id(entity_id, msg_id)
    elif msg_id is not None:
        with status_lock:
            status_items = list(download_status.items())
        for tid, info in status_items:
            if info.get("msg_id") == msg_id:
                task_id = tid
                if entity_id is None:
                    entity_id = info.get("entity_id")
                if not dialog_name:
                    dialog_name = info.get("dialog_name", dialog_name)
                break

    if not task_id or entity_id is None or msg_id is None:
        return jsonify({"error": "消息未找到"}), 400

    try:
        return jsonify(_resume_task(task_id, dialog_name=dialog_name, auto=False))
    except Exception as exc:
        _update_task_state(task_id, status="error", error=str(exc), finish_time=time.time())
        return jsonify({"error": str(exc)}), 500


@app.route("/api/retry_all", methods=["POST"])
def api_retry_all():
    result = _resume_all_incomplete_tasks(auto=False)
    return jsonify({"ok": True, **result})


@app.route("/api/queue_action", methods=["POST"])
def api_queue_action():
    data = request.json or {}
    task_id = data.get("task_id")
    action = data.get("action")
    if not task_id or action not in {"pause", "resume", "delete", "top", "up", "down"}:
        return jsonify({"error": "参数不完整"}), 400
    state = _copy_task_state(task_id) or {}
    if action == "resume":
        try:
            return jsonify(_resume_task(task_id, auto=False))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
    if action in {"top", "up", "down"}:
        if not move_queued_task(task_id, action):
            return jsonify({"error": "任务不在等待队列中"}), 409
        return jsonify({"ok": True})
    if state.get("status") == "downloading":
        return jsonify({"error": "下载中的任务请使用取消，已下载部分会保留"}), 409
    remove_from_queue(task_id)
    if action == "pause":
        _update_task_state(task_id, status="paused", error="已暂停", speed="", speed_bps=0.0, queue_position=None, queue_size=0)
        return jsonify({"ok": True})
    _drop_task_state(task_id)
    _clear_download_cancelled(task_id)
    _clear_tdl_error(task_id)
    clear_resume_info(task_id)
    return jsonify({"ok": True})


@app.route("/api/recovery_candidates")
def api_recovery_candidates():
    return jsonify({"candidates": _log_recovery_candidates(request.args.get("limit", 200, type=int))})


@app.route("/api/recover_candidates", methods=["POST"])
def api_recover_candidates():
    data = request.json or {}
    task_ids = data.get("task_ids") or []
    dialog_name = data.get("dialog_name") or "日志恢复"
    submitted = []
    errors = {}
    allowed = {item["task_id"] for item in _log_recovery_candidates(500)}
    for task_id in task_ids:
        if task_id not in allowed:
            errors[task_id] = "任务不在可恢复日志列表中"
            continue
        try:
            result = _resume_task(task_id, dialog_name=dialog_name, auto=False)
            if result.get("ok"):
                submitted.append(task_id)
            else:
                errors[task_id] = result.get("error", "恢复失败")
        except Exception as exc:
            errors[task_id] = str(exc)
    return jsonify({"ok": True, "submitted": submitted, "errors": errors})


@app.route("/api/history")
def api_history():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 30, type=int)
    items, total = _query_task_history(
        status=request.args.get("status", "").strip(),
        query=request.args.get("q", "").strip(),
        page=page,
        per_page=per_page,
    )
    return jsonify({"items": items, "total": total, "page": page, "per_page": per_page})


@app.route("/api/download_status")
def api_download_status():
    _recover_stalled_tasks()
    _restore_resume_tasks_into_memory()
    # Auto-cleanup: remove terminal tasks older than 1 hour
    now = time.time()
    with status_lock:
        stale = [k for k, v in list(download_status.items())
                 if v.get("status") in TERMINAL_STATES
                 and v.get("finish_time") and now - v["finish_time"] > 3600]
        for k in stale:
            _drop_task_state(k)
        tasks = {k: dict(v) for k, v in list(download_status.items())}
    return jsonify({"tasks": tasks, "queue": get_queue_status()})


@app.route("/api/clear_tasks", methods=["POST"])
def api_clear_tasks():
    data = request.json or {}
    task_ids = data.get("task_ids")
    clearable_statuses = {"error", "cancelled"}
    with status_lock:
        if task_ids is None:
            task_ids = [k for k, v in list(download_status.items()) if v.get("status") in clearable_statuses]
        cleared = 0
        skipped = 0
        for tid in list(task_ids):
            state = download_status.get(tid)
            if not state:
                continue
            if state.get("status") not in clearable_statuses:
                skipped += 1
                continue
            _drop_task_state(tid)
            _clear_download_cancelled(tid)
            _clear_tdl_error(tid)
            clear_resume_info(tid)
            cleared += 1
    return jsonify({"ok": True, "cleared": cleared, "skipped": skipped})


def _download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath):
    ensure_tg_connection(allow_reconnect=True)
    total_bytes = info.get("size") or 0
    existing_size = _detect_resume_offset(task_id, filepath, total_bytes)
    if existing_size > 0 and total_bytes > 0 and existing_size < total_bytes:
        log_info(f"[{task_id}] 发现部分文件 {info['filename']}: {format_size(existing_size)}/{format_size(total_bytes)}，将续传")

    start_offset = existing_size if (existing_size > 0 and total_bytes > 0 and existing_size < total_bytes) else 0
    init_pct = int(start_offset / total_bytes * 100) if total_bytes and start_offset else 0
    _set_task_state(task_id, {
        "filename": info["filename"],
        "progress": init_pct,
        "status": "downloading",
        "downloaded": format_size(start_offset) if start_offset else "0B",
        "total": format_size(total_bytes) if total_bytes else "",
        "error": f"续传 {format_size(start_offset)}" if start_offset else "",
        "speed": "",
        "msg_id": msg_id,
        "entity_id": entity_id,
        "dialog_name": dialog_name,
        "downloaded_bytes": start_offset,
        "total_bytes": total_bytes,
        "speed_bps": 0.0,
        "queue_position": None,
        "queue_size": 0,
        "downloader": "telegram",
    })

    retry_count = 0
    while True:
        # Telegram file references expire. Fetch a fresh message for every
        # attempt so large downloads can resume after a reference refresh.
        message = _resolve_message(entity_id, msg_id, force_refresh=True)
        if not message or not getattr(getattr(message, "media", None), "document", None):
            raise RuntimeError("消息不包含可下载视频")

        start_offset = _detect_resume_offset(task_id, filepath, total_bytes)

        async def _runner():
            written = start_offset
            last_bytes = start_offset
            last_time = time.time()
            last_save_time = time.time()
            mode = "ab" if start_offset else "wb"
            with open(filepath, mode) as output:
                iterator = tg_client.iter_download(
                    message.media.document,
                    offset=start_offset,
                    file_size=total_bytes or None,
                    request_size=512 * 1024,
                )
                while True:
                    try:
                        chunk = await _next_telegram_chunk(iterator, timeout=TELEGRAM_CHUNK_TIMEOUT)
                    except StopAsyncIteration:
                        break
                    if _get_download_cancelled(task_id):
                        raise RuntimeError("下载已取消")
                    if not chunk:
                        continue
                    output.write(chunk)
                    output.flush()
                    written += len(chunk)
                    now = time.time()
                    speed_bps = 0.0
                    speed_label = ""
                    elapsed = now - last_time
                    if elapsed >= 0.5:
                        delta = written - last_bytes
                        speed_bps = delta / elapsed if elapsed > 0 else 0.0
                        speed_label = format_size(speed_bps) + "/s" if speed_bps > 0 else ""
                        last_bytes = written
                        last_time = now
                    pct = int(written / total_bytes * 100) if total_bytes else 0
                    _update_task_state(
                        task_id,
                        progress=min(pct, 99) if total_bytes and written < total_bytes else pct,
                        status="downloading",
                        downloaded=format_size(written),
                        downloaded_bytes=written,
                        error="",
                        speed=speed_label,
                        speed_bps=speed_bps,
                    )
                    if now - last_save_time >= 10:
                        save_resume_info(task_id, {
                            "filepath": filepath,
                            "filename": info["filename"],
                            "offset": written,
                            "total": total_bytes,
                            "entity_id": entity_id,
                            "msg_id": msg_id,
                            "dialog_name": dialog_name,
                        })
                        last_save_time = now
            return written

        timeout = _calc_download_timeout(max(total_bytes - start_offset, 0) or total_bytes)
        try:
            final_size = run_async(lambda: _runner(), timeout=timeout, allow_reconnect=True)
            break
        except Exception as exc:
            if _get_download_cancelled(task_id) or "取消" in str(exc):
                raise
            if (
                retry_count >= TELEGRAM_MAX_RETRY_ATTEMPTS
                or not _should_retry_telegram_download_error(exc)
            ):
                raise
            retry_count += 1
            current_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
            save_resume_info(task_id, {
                "filepath": filepath,
                "filename": info["filename"],
                "offset": current_size,
                "total": total_bytes,
                "entity_id": entity_id,
                "msg_id": msg_id,
                "dialog_name": dialog_name,
            })
            _update_task_state(
                task_id,
                status="downloading",
                downloaded=format_size(current_size),
                downloaded_bytes=current_size,
                error=f"连接中断，刷新媒体引用后自动续传（第 {retry_count} 次）",
                speed="",
                speed_bps=0.0,
            )
            log_warning(
                f"[{task_id}] Telegram 下载中断，刷新媒体引用后自动续传"
                f"（第 {retry_count} 次，已保留 {format_size(current_size)}）: {exc}"
            )
            time.sleep(min(2 * retry_count, 20))

    completion_error = _validate_tdl_completion(total_bytes=total_bytes, final_size=final_size)
    if completion_error:
        raise RuntimeError(completion_error)
    if total_bytes and final_size <= 0:
        raise RuntimeError("Telegram 未产生有效下载数据")

    _update_task_state(
        task_id,
        progress=100,
        status="done",
        finish_time=time.time(),
        downloaded=format_size(final_size),
        total=format_size(final_size),
        error="",
        speed="",
        downloaded_bytes=final_size,
        total_bytes=final_size,
        expected_bytes=total_bytes,
        final_bytes=final_size,
        document_id=str(info.get("document_id") or ""),
        integrity="ok",
        speed_bps=0.0,
        queue_position=None,
        queue_size=0,
    )
    clear_resume_info(task_id)
    log_info(f"下载完成 [{task_id}] {info['filename']} ({format_size(final_size)})")


@app.route("/api/progress")
def api_progress():
    def snapshot():
        try:
            # list() 防止迭代期间字典被其他线程修改导致 RuntimeError
            with status_lock:
                tasks = {key: dict(value) for key, value in list(download_status.items())}
        except Exception:
            tasks = {}
        complete = bool(tasks) and all(
            state.get("status") in TERMINAL_STATES for state in tasks.values()
        )
        return {
            "tasks": tasks,
            "queue": get_queue_status(),
            "complete": complete,
            "timestamp": time.time(),
        }

    def generate():
        while True:
            try:
                payload = snapshot()
                yield f"data: {json.dumps(payload)}\n\n"
                if payload["complete"]:
                    break
            except Exception:
                break
            time.sleep(0.8)

    return Response(
        generate(),
        mimetype="text-event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _do_download(task_items, dialog_name):
    global active_downloads
    
    try:
        save_dir = _download_dir_for_dialog(dialog_name)
        os.makedirs(save_dir, exist_ok=True)
    except Exception as _e:
        log_error(f"_do_download: 创建目录失败: {_e}")
        with queue_lock:
            active_downloads = max(0, active_downloads - 1)
            for task in task_items:
                scheduled_task_ids.discard(task.get("task_id"))
        process_queue()
        return

    for task in task_items:
        task_id = task.get("task_id")
        entity_id = task.get("entity_id")
        msg_id = task.get("msg_id")
        if not task_id or entity_id is None or msg_id is None:
            continue

        if _get_download_cancelled(task_id):
            state = _copy_task_state(task_id) or {}
            state["status"] = "cancelled"
            state["error"] = "已取消"
            state["speed"] = ""
            state["speed_bps"] = 0.0
            state["queue_position"] = None
            state["queue_size"] = 0
            _set_task_state(task_id, state)
            continue

        info = task.get("info")
        if not info:
            message = _get_cached_message(msg_id, entity_id)
            if not message:
                try:
                    log_info(f"[{task_id}] 缓存未命中，重新获取消息 entity={entity_id} msg={msg_id}")
                    message = _resolve_message(entity_id, msg_id)
                except Exception as e:
                    log_error(f"[{task_id}] 重新获取消息失败: {e}")
                    message = None
            info = get_video_info(message) if message else None
        if not info:
            _update_task_state(
                task_id,
                status="error",
                error="消息不包含可下载视频",
                finish_time=time.time(),
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
            )
            continue

        filepath = os.path.join(save_dir, info["filename"])

        if os.path.exists(filepath) and os.path.getsize(filepath) == info["size"]:
            log_info(f"跳过(已存在) [{task_id}] {info['filename']}")
            final_size = info.get("size") or os.path.getsize(filepath)
            _set_task_state(task_id, {
                "filename": info["filename"],
                "progress": 100,
                "status": "skipped",
                "finish_time": time.time(),
                "downloaded": format_size(final_size),
                "total": format_size(final_size),
                "error": "",
                "speed": "",
                "msg_id": msg_id,
                "entity_id": entity_id,
                "dialog_name": dialog_name,
                "downloaded_bytes": final_size,
                "total_bytes": final_size,
                "expected_bytes": info.get("size") or final_size,
                "final_bytes": final_size,
                "document_id": str(info.get("document_id") or ""),
                "integrity": "ok",
                "speed_bps": 0.0,
                "queue_position": None,
                "queue_size": 0,
            })
            continue

        downloader = task.get("downloader") or ("tdl" if _supports_tdl_download(entity_id) else "telegram")
        _update_task_state(task_id, downloader=downloader)
        total_bytes = info.get("size") or 0
        if downloader == "telegram":
            try:
                _download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath)
            except Exception as e:
                err = str(e)
                log_error(f"下载失败 [{task_id}] {info.get('filename','?')}: {err}")
                cur_file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                if _get_download_cancelled(task_id) or "取消" in err:
                    _update_task_state(task_id, status="cancelled", error="已取消", finish_time=time.time())
                    if cur_file_size > 0:
                        save_resume_info(task_id, {
                            "filepath": filepath,
                            "filename": info["filename"],
                            "offset": cur_file_size,
                            "total": total_bytes,
                            "entity_id": entity_id,
                            "msg_id": msg_id,
                            "dialog_name": dialog_name,
                        })
                else:
                    _update_task_state(task_id, status="error", error=err, finish_time=time.time())
                    if cur_file_size > 0:
                        save_resume_info(task_id, {
                            "filepath": filepath,
                            "filename": info["filename"],
                            "offset": cur_file_size,
                            "total": total_bytes,
                            "entity_id": entity_id,
                            "msg_id": msg_id,
                            "dialog_name": dialog_name,
                        })
                _update_task_state(task_id, speed="", speed_bps=0.0, queue_position=None, queue_size=0)
            continue

        message_url = ""
        try:
            message_url = build_tdl_message_url(entity_id, msg_id)
        except Exception as exc:
            _update_task_state(
                task_id,
                status="error",
                error=str(exc),
                finish_time=time.time(),
                speed="",
                speed_bps=0.0,
                queue_position=None,
                queue_size=0,
            )
            continue

        existing_size = _detect_resume_offset(task_id, filepath, total_bytes)
        if existing_size > 0 and total_bytes > 0 and existing_size < total_bytes:
            log_info(f"[{task_id}] 发现部分文件 {info['filename']}: {format_size(existing_size)}/{format_size(total_bytes)}，将续传")

        start_offset = existing_size if (existing_size > 0 and total_bytes > 0 and existing_size < total_bytes) else 0
        init_pct = int(start_offset / total_bytes * 100) if total_bytes and start_offset else 0

        _set_task_state(task_id, {
            "filename": info["filename"],
            "progress": init_pct,
            "status": "downloading",
            "downloaded": format_size(start_offset) if start_offset else "0B",
            "total": format_size(total_bytes) if total_bytes else "",
            "error": f"续传 {format_size(start_offset)}" if start_offset else "",
            "speed": "",
            "msg_id": msg_id,
            "entity_id": entity_id,
            "dialog_name": dialog_name,
            "downloaded_bytes": start_offset,
            "total_bytes": total_bytes,
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
        })

        retry_count = 0
        last_retry_size = start_offset
        try:
            while True:
                try:
                    command = build_tdl_download_command(message_url, save_dir, info["filename"])
                    _clear_tdl_error(task_id)
                    process = subprocess.Popen(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    _register_tdl_process(task_id, process)

                    def _drain_output():
                        try:
                            if not process.stdout:
                                return
                            for line in process.stdout:
                                line = line.strip()
                                if line:
                                    log_info(f"[tdl:{task_id}] {line}")
                                    if _should_capture_tdl_error_line(line):
                                        current_error = tdl_last_errors.get(task_id, "")
                                        _set_tdl_error(task_id, _choose_more_specific_tdl_error(current_error, line))
                        except Exception:
                            pass

                    output_thread = threading.Thread(target=_drain_output, daemon=True)
                    output_thread.start()

                    if retry_count > 0:
                        progress_path = _resolve_tdl_progress_path(filepath)
                        resumed_size = os.path.getsize(progress_path) if os.path.exists(progress_path) else start_offset
                        written = resumed_size
                        last_bytes = resumed_size
                    else:
                        written = start_offset
                        last_bytes = start_offset
                    allow_offset_correction = start_offset > 0
                    last_time = time.time()
                    last_save_time = time.time()
                    restart_logged = False
                    last_progress_written = written
                    last_progress_time = time.time()
                    TDL_STALL_TIMEOUT = 600  # 10 minutes with zero progress = stalled

                    while True:
                        if _get_download_cancelled(task_id):
                            _stop_tdl_process(process)
                            raise Exception("下载已取消")

                        progress_path = _resolve_tdl_progress_path(filepath)
                        current_size = os.path.getsize(progress_path) if os.path.exists(progress_path) else written
                        current_size, allow_offset_correction = _reconcile_tdl_progress_size(
                            current_size=current_size,
                            written=written,
                            allow_offset_correction=allow_offset_correction,
                        )
                        written = current_size

                        # Track stall: if no progress for 5 minutes, kill tdl
                        if written > last_progress_written:
                            last_progress_written = written
                            last_progress_time = time.time()
                        elif time.time() - last_progress_time > TDL_STALL_TIMEOUT:
                            log_warning(f"[{task_id}] tdl 下载停滞超过 {TDL_STALL_TIMEOUT}s，终止进程")
                            _stop_tdl_process(process)
                            raise RuntimeError("下载停滞，连接可能已断开")

                        # If we expected resume but tdl started from scratch,
                        # just log it once and reset start_offset so progress displays correctly.
                        # Let tdl continue downloading from scratch - don't kill it.
                        if (not restart_logged
                                and start_offset > TDL_RESTART_RESET_MIN_BYTES
                                and time.time() - last_save_time > 10
                                and written < int(start_offset * 0.5)):
                            restart_logged = True
                            log_warning(
                                f"[{task_id}] tdl 未续传（期望 {format_size(start_offset)}，"
                                f"当前 {format_size(written)}），将从头下载"
                            )
                            start_offset = 0
                            last_retry_size = 0

                        now = time.time()
                        elapsed = now - last_time
                        speed_bps = 0.0
                        speed_label = ""
                        if elapsed >= 0.5:
                            delta = written - last_bytes
                            speed_bps = delta / elapsed if elapsed > 0 else 0.0
                            speed_label = format_size(speed_bps) + "/s" if speed_bps > 0 else ""
                            last_bytes = written
                            last_time = now

                        pct = int(written / total_bytes * 100) if total_bytes else 0
                        state = _copy_task_state(task_id)
                        if state and state.get("status") not in TERMINAL_STATES:
                            updates = {
                                "progress": min(pct, 99) if total_bytes and written < total_bytes else pct,
                                "status": "downloading",
                                "downloaded": format_size(written),
                                "downloaded_bytes": written,
                                "error": "",
                            }
                            if speed_label:
                                updates["speed"] = speed_label
                                updates["speed_bps"] = speed_bps
                            _update_task_state(task_id, **updates)

                        if now - last_save_time >= 10:
                            save_resume_info(task_id, {
                                "filepath": filepath,
                                "filename": info["filename"],
                                "offset": written,
                                "total": total_bytes,
                                "entity_id": entity_id,
                                "msg_id": msg_id,
                                "dialog_name": dialog_name,
                            })
                            last_save_time = now

                        retcode = process.poll()
                        if retcode is not None:
                            break
                        time.sleep(0.5)

                    final_progress_path = _resolve_tdl_progress_path(filepath)
                    final_size = os.path.getsize(final_progress_path) if os.path.exists(final_progress_path) else written
                    output_thread.join(timeout=0.5)
                    if process.returncode != 0:
                        last_error = tdl_last_errors.get(task_id, "")
                        raise RuntimeError(last_error or f"tdl 退出码 {process.returncode}")

                    # tdl success: prefer the final file over .tmp for size check
                    tmp_path = filepath + ".tmp"
                    if os.path.exists(filepath):
                        final_size = os.path.getsize(filepath)
                        # Clean up stale .tmp if final file exists
                        if os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                                log_info(f"[{task_id}] 清理残留 .tmp 文件")
                            except Exception:
                                pass
                    elif os.path.exists(tmp_path):
                        final_size = os.path.getsize(tmp_path)

                    completion_error = _validate_tdl_completion(total_bytes=total_bytes, final_size=final_size)
                    if completion_error:
                        raise RuntimeError(completion_error)
                    if total_bytes and final_size <= 0:
                        raise RuntimeError("tdl 未产生有效下载数据")

                    _update_task_state(
                        task_id,
                        progress=100,
                        status="done",
                        finish_time=time.time(),
                        downloaded=format_size(final_size),
                        total=format_size(final_size),
                        speed="",
                        downloaded_bytes=final_size,
                        total_bytes=final_size,
                        expected_bytes=total_bytes,
                        final_bytes=final_size,
                        document_id=str(info.get("document_id") or ""),
                        integrity="ok",
                        speed_bps=0.0,
                        queue_position=None,
                        queue_size=0,
                    )
                    clear_resume_info(task_id)
                    log_info(f"下载完成 [{task_id}] {info['filename']} ({format_size(final_size)})")
                    break
                except Exception as e:
                    err = str(e)
                    cur_progress_path = _resolve_tdl_progress_path(filepath)
                    cur_file_size = os.path.getsize(cur_progress_path) if os.path.exists(cur_progress_path) else 0
                    if _did_tdl_restart_from_scratch(
                        retry_count=retry_count,
                        previous_size=last_retry_size,
                        current_size=cur_file_size,
                        start_offset=start_offset,
                    ):
                        log_warning(
                            f"[{task_id}] 断点失效，tdl 从头开始下载"
                            f"（期望续传 {format_size(last_retry_size or start_offset)}，"
                            f"实际 {format_size(cur_file_size)}）"
                        )
                        # Reset offsets so next retry doesn't keep detecting restart-from-scratch
                        start_offset = 0
                        last_retry_size = cur_file_size
                        retry_count += 1
                        time.sleep(min(2 * retry_count, 10))
                        continue
                    if _should_retry_tdl_error(
                        err,
                        retry_count,
                        current_size=cur_file_size,
                        last_retry_size=last_retry_size,
                    ) and not _get_download_cancelled(task_id):
                        retry_count += 1
                        last_retry_size = cur_file_size
                        save_resume_info(task_id, {
                            "filepath": filepath,
                            "filename": info["filename"],
                            "offset": cur_file_size,
                            "total": total_bytes,
                            "entity_id": entity_id,
                            "msg_id": msg_id,
                            "dialog_name": dialog_name,
                        })
                        _update_task_state(
                            task_id,
                            status="downloading",
                            error=f"连接中断，正在续传（第 {retry_count} 次，已保留 {format_size(cur_file_size)}）",
                            speed="",
                            speed_bps=0.0,
                        )
                        log_warning(f"[{task_id}] tdl 下载中断，准备自动续传: {err}")
                        time.sleep(min(2 * retry_count, 10))
                        continue
                    raise
        except Exception as e:
            err = str(e)
            log_error(f"下载失败 [{task_id}] {info.get('filename','?')}: {err}")
            cur_progress_path = _resolve_tdl_progress_path(filepath)
            cur_file_size = os.path.getsize(cur_progress_path) if os.path.exists(cur_progress_path) else 0
            if _should_fallback_from_tdl(err) and not _get_download_cancelled(task_id):
                _remember_tdl_fallback_channel(entity_id, err)
                log_warning(f"[{task_id}] tdl 无法解析消息链接，切换 Telegram 直连: {err}")
                _update_task_state(
                    task_id,
                    status="downloading",
                    error="tdl 解析失败，切换 Telegram 直连",
                    speed="",
                    speed_bps=0.0,
                    queue_position=None,
                    queue_size=0,
                    downloader="telegram",
                )
                try:
                    _clear_tdl_error(task_id)
                    filepath = _prepare_telegram_fallback_target(filepath)
                    _download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath)
                    continue
                except Exception as fallback_exc:
                    err = str(fallback_exc)
                    log_error(f"[{task_id}] Telegram 直连回退失败: {err}")
                    cur_file_size = os.path.getsize(filepath) if os.path.exists(filepath) else cur_file_size
            if _get_download_cancelled(task_id) or "取消" in err:
                _update_task_state(task_id, status="cancelled", error="已取消", finish_time=time.time())
                if cur_file_size > 0:
                    save_resume_info(task_id, {
                        "filepath": filepath,
                        "filename": info["filename"],
                        "offset": cur_file_size,
                        "total": total_bytes,
                        "entity_id": entity_id,
                        "msg_id": msg_id,
                        "dialog_name": dialog_name,
                    })
                    log_info(f"[{task_id}] 已取消，保留部分文件 {format_size(cur_file_size)}")
            else:
                _update_task_state(task_id, status="error", error=err, finish_time=time.time())
                if cur_file_size > 0:
                    save_resume_info(task_id, {
                        "filepath": filepath,
                        "filename": info["filename"],
                        "offset": cur_file_size,
                        "total": total_bytes,
                        "entity_id": entity_id,
                        "msg_id": msg_id,
                        "dialog_name": dialog_name,
                    })
            _update_task_state(task_id, speed="", speed_bps=0.0, queue_position=None, queue_size=0)
        finally:
            proc = _get_tdl_process(task_id)
            _stop_tdl_process(proc)
            _drop_tdl_process(task_id)

    # 所有任务完成，减少活跃数并处理队列
    with queue_lock:
        active_downloads = max(0, active_downloads - 1)
        for task in task_items:
            scheduled_task_ids.discard(task.get("task_id"))
    process_queue()


@app.route("/api/stream/<path:filepath>")
def api_stream(filepath):
    """流式传输视频文件用于浏览器预览"""
    full_path = os.path.join(DOWNLOAD_DIR, filepath)
    full_path = os.path.realpath(full_path)
    if os.path.commonpath([os.path.realpath(DOWNLOAD_DIR), full_path]) != os.path.realpath(DOWNLOAD_DIR):
        return jsonify({"error": "非法路径"}), 403
    if not os.path.isfile(full_path):
        return jsonify({"error": "文件不存在"}), 404

    file_size = os.path.getsize(full_path)
    range_header = request.headers.get("Range")

    if range_header:
        byte_start = int(range_header.replace("bytes=", "").split("-")[0])
        byte_end = min(byte_start + 4 * 1024 * 1024, file_size)  # 4MB chunks
        content_length = byte_end - byte_start

        def generate():
            with open(full_path, "rb") as f:
                f.seek(byte_start)
                remaining = content_length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return Response(generate(), status=206, mimetype="video/mp4", headers={
            "Content-Range": f"bytes {byte_start}-{byte_end - 1}/{file_size}",
            "Content-Length": content_length,
            "Accept-Ranges": "bytes",
        })
    else:
        return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path),
                                   mimetype="video/mp4")


# Relay 并发控制
MAX_CONCURRENT_RELAYS = 2
active_relays = 0
relay_lock = threading.Lock()

@app.route("/relay/<signed_int:entity_id>/<int:msg_id>")
def relay_media(entity_id, msg_id):
    global active_relays
    if not RELAY_TOKEN_SECRET:
        return jsonify({"error": "relay token secret is not configured"}), 503

    # 并发限制检查
    with relay_lock:
        if active_relays >= MAX_CONCURRENT_RELAYS:
            log_warning(f"[relay:{entity_id}:{msg_id}] 并发数已达上限 {MAX_CONCURRENT_RELAYS}，请在 aria2 限制任务数")
            return jsonify({"error": "relay concurrency limit reached"}), 503
        active_relays += 1

    try:
        file_name = request.args.get("file_name", "")
        token = request.args.get("token", "")
        if not file_name or not token:
            return jsonify({"error": "missing relay parameters"}), 400

        verify_relay_token(
            secret=RELAY_TOKEN_SECRET,
            token=token,
            entity_id=entity_id,
            message_id=msg_id,
            file_name=file_name,
            now_ts=int(time.time()),
        )

        media = get_relay_media(entity_id, msg_id)
        if media.get("file_name") != file_name:
            return jsonify({"error": "file name mismatch"}), 403

        total_size = int(media.get("size") or 0)
        start_offset, end_offset, status_code = _parse_range(request.headers.get("Range"), total_size)
        content_length = end_offset - start_offset + 1
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}",
        }
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start_offset}-{end_offset}/{total_size}"

        def _generate_with_cleanup():
            try:
                for chunk in iter_relay_bytes(media, start_offset, end_offset):
                    yield chunk
            finally:
                global active_relays
                with relay_lock:
                    active_relays = max(0, active_relays - 1)
                log_info(f"[relay:{entity_id}:{msg_id}] 传输结束，释放槽位 (当前活跃: {active_relays})")

        return Response(
            _generate_with_cleanup(),
            status=status_code,
            mimetype=media.get("mime_type") or "application/octet-stream",
            headers=headers,
        )
    except Exception as exc:
        with relay_lock:
            active_relays = max(0, active_relays - 1)
        log_error(f"[relay:{entity_id}:{msg_id}] relay route failed: {exc}")
        if "token" in str(exc).lower():
            return jsonify({"error": str(exc)}), 403
        return jsonify({"error": str(exc)}), 502


@app.route("/api/files")
def api_files():
    files = []
    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    per_page = min(max(request.args.get("per_page", default=100, type=int) or 100, 10), 500)
    if not os.path.exists(DOWNLOAD_DIR):
        return jsonify({"files": [], "page": page, "per_page": per_page, "total": 0, "pages": 0})
    for folder in sorted(os.listdir(DOWNLOAD_DIR)):
        folder_path = os.path.join(DOWNLOAD_DIR, folder)
        if not os.path.isdir(folder_path):
            continue
        for fname in sorted(os.listdir(folder_path)):
            fpath = os.path.join(folder_path, fname)
            if os.path.isfile(fpath):
                files.append({
                    "folder": folder, "filename": fname,
                    "size": format_size(os.path.getsize(fpath)),
                    "modified": datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M"),
                    "modified_ts": os.path.getmtime(fpath),
                })
    files.sort(key=lambda item: item["modified_ts"], reverse=True)
    total = len(files)
    start = (page - 1) * per_page
    items = files[start:start + per_page]
    for item in items:
        item.pop("modified_ts", None)
    return jsonify({
        "files": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page,
    })


@app.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    full_path = os.path.join(DOWNLOAD_DIR, filepath)
    full_path = os.path.realpath(full_path)
    if os.path.commonpath([os.path.realpath(DOWNLOAD_DIR), full_path]) != os.path.realpath(DOWNLOAD_DIR):
        return jsonify({"error": "非法路径"}), 403
    if not os.path.isfile(full_path):
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path), as_attachment=True)


def start_tg_client():
    global tg_connected, tg_connect_error, tg_user_info
    asyncio.set_event_loop(tg_loop)

    max_retries = 0  # 无限重试
    retry_count = 0
    retry_delay = 5

    while True:
        try:
            tg_connected = False
            tg_connect_error = "正在连接 Telegram..."
            print(f"正在连接 Telegram... (第 {retry_count + 1} 次)")
            log_info(f"正在连接 Telegram... (第 {retry_count + 1} 次)")

            # 带超时的连接
            tg_loop.run_until_complete(
                asyncio.wait_for(tg_client.connect(), timeout=30)
            )

            if not tg_loop.run_until_complete(tg_client.is_user_authorized()):
                tg_connect_error = "Telegram 未登录！请先运行 downloader.py 完成登录。"
                print(f"错误: {tg_connect_error}")
                sys.exit(1)

            me = tg_loop.run_until_complete(tg_client.get_me())
            tg_user_info = _format_user_display(me)
            tg_connected = True
            tg_connect_error = ""
            retry_count = 0
            print(f"Telegram 已连接: {tg_user_info}")
            log_info(f"Telegram 已连接: {tg_user_info}")
            init_tg_health_checker()
            tg_loop.run_forever()
            break  # run_forever 不会正常返回，除非 loop.stop()

        except asyncio.TimeoutError:
            retry_count += 1
            tg_connect_error = f"连接超时，{retry_delay}秒后重试... (已重试 {retry_count} 次)"
            print(tg_connect_error)
            # 断开可能的半连接状态
            try:
                tg_loop.run_until_complete(tg_client.disconnect())
            except Exception:
                pass
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)  # 逐步增加重试间隔，最大60秒

        except Exception as e:
            retry_count += 1
            tg_connect_error = f"连接失败: {e}，{retry_delay}秒后重试..."
            print(tg_connect_error)
            try:
                tg_loop.run_until_complete(tg_client.disconnect())
            except Exception:
                pass
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)


def start_relay_tg_client():
    global relay_connected, relay_connect_error, relay_tg_client
    asyncio.set_event_loop(relay_loop)

    retry_count = 0
    retry_delay = 5

    while True:
        try:
            relay_connected = False
            relay_connect_error = "正在连接 Relay Telegram..."
            log_info(f"正在连接 Relay Telegram... (第 {retry_count + 1} 次)")

            if not _wait_for_main_tg_ready(timeout=60):
                raise Exception(tg_connect_error or "主 Telegram 未就绪")

            relay_tg_client = _recreate_relay_client_from_main_session()

            relay_loop.run_until_complete(
                asyncio.wait_for(relay_tg_client.connect(), timeout=30)
            )

            if not relay_loop.run_until_complete(relay_tg_client.is_user_authorized()):
                relay_connect_error = "Relay Telegram 未登录"
                log_error(relay_connect_error)
                return

            relay_connected = True
            relay_connect_error = ""
            retry_count = 0
            log_info("Relay Telegram 已连接")
            relay_loop.run_forever()
            break

        except asyncio.TimeoutError:
            retry_count += 1
            relay_connect_error = f"Relay 连接超时，{retry_delay}秒后重试..."
            log_warning(relay_connect_error)
            try:
                relay_loop.run_until_complete(relay_tg_client.disconnect())
            except Exception:
                pass
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)

        except Exception as e:
            retry_count += 1
            relay_connect_error = f"Relay 连接失败: {e}，{retry_delay}秒后重试..."
            log_warning(relay_connect_error)
            try:
                relay_loop.run_until_complete(relay_tg_client.disconnect())
            except Exception:
                pass
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)


def auto_resume_incomplete_tasks():
    if not _wait_for_main_tg_ready(timeout=180):
        log_warning(f"启动自动恢复跳过: {tg_connect_error or 'Telegram 未就绪'}")
        return
    result = _resume_all_incomplete_tasks(auto=True)
    if result["submitted"] or result["errors"]:
        log_info(f"启动自动恢复完成: submitted={len(result['submitted'])}, errors={len(result['errors'])}")


def start_background_clients():
    tg_thread = threading.Thread(target=start_tg_client, daemon=True)
    tg_thread.start()
    return {"tg_thread": tg_thread}


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    restored_states = _load_persisted_task_states()
    if restored_states:
        log_info(f"已加载持久化下载任务: {restored_states}")
    _restore_resume_tasks_into_memory()
    start_background_clients()
    threading.Thread(target=auto_resume_incomplete_tasks, daemon=True).start()
    download_watchdog.start()
    threading.Thread(target=run_thumbnail_cleanup_loop, daemon=True).start()
    threading.Thread(target=run_task_database_backup_loop, daemon=True).start()
    time.sleep(3)
    if not API_ID or not API_HASH:
        raise RuntimeError("Missing TG_API_ID/TG_API_HASH environment variables")
    if not _is_local_bind_only() and (not WEB_AUTH_USERNAME or not WEB_AUTH_PASSWORD):
        raise RuntimeError("Non-local binding requires WEB_AUTH_USERNAME and WEB_AUTH_PASSWORD")
    print(f"Web UI 启动: http://{WEB_BIND_HOST}:{WEB_BIND_PORT}")
    app.run(host=WEB_BIND_HOST, port=WEB_BIND_PORT, threaded=True)
