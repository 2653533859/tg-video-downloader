#!/usr/bin/env python3
"""Telegram 视频下载器 - Web UI"""

import os
import sys
import asyncio
import threading
import time
import json
import re
import queue
import shutil
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
    ALLOWED_PROXY_TYPES,
    API_ID,
    API_HASH,
    DEBUG_API_ENABLED,
    DOWNLOAD_DIR,
    OPEN_FOLDER_ENABLED,
    PUBLIC_BASE_URL,
    PROXY_CONFIG,
    TELETHON_PROXY_CONFIG,
    build_proxy_config,
    build_telethon_proxy_config,
    normalize_proxy_type,
    proxy_config_label,
    RELAY_TOKEN_SECRET,
    RELAY_TOKEN_TTL,
    SESSION_NAME,
    TDL_BINARY,
    TDL_CHAT_ID_OVERRIDES,
    TDL_LIMIT,
    TDL_NAMESPACE,
    TDL_STORAGE_PATH,
    TDL_THREADS,
    TRUST_FORWARDED_FOR,
    WEB_AUTH_PASSWORD,
    WEB_AUTH_USERNAME,
    WEB_BIND_HOST,
    WEB_BIND_PORT,
)
from relay_tokens import build_relay_token, verify_relay_token
from src.download.paths import (
    download_dir_for_dialog,
    prepare_telegram_fallback_target,
    resolve_tdl_progress_path,
    sanitize_dialog_name,
)
from src.download.manager import DownloadManager
from src.download.resume import ResumeStore
from src.download.scheduler import DownloadScheduler
from src.download.status import build_download_status_payload
from src.download.task_actions import (
    clear_tasks_by_scope,
    query_task_history_payload as build_task_history_payload,
    recover_candidate_tasks,
)
from src.download.tdl import TdlRuntime
from src.download.tdl_executor import TdlDownloadExecutor
from src.download import tdl_rules
from src.download.telegram_downloader import TelegramDirectDownloader
from src.download.watchdog import DownloadWatchdog
from src.download.worker import DownloadWorker
from src.files import (
    cleanup_thumbnail_cache as cleanup_thumbnail_cache_under,
    delete_download_file,
    iter_file_chunks,
    list_download_files,
    local_stream_range,
    prepare_open_folder,
    rename_download_file,
    resolve_download_path as resolve_download_path_under,
    resolve_file_path,
    thumbnail_cache_path,
    write_thumbnail,
)
from src.relay import parse_range
from src.security import (
    is_local_bind_only,
    request_ip_is_local,
    require_web_auth,
)
from src.state.persistence import TaskStatePersistence
from src.system import SystemStatusService, start_runtime_services, validate_runtime_config
from src.telegram import (
    TelegramDebugService,
    TelegramHealthChecker,
    TelegramVideoService,
    run_main_telegram_client,
    run_relay_telegram_client,
)
from src.telegram.runtime import TelegramRuntime

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
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop, proxy=build_telethon_proxy_config(PROXY_CONFIG))
# Relay client will be initialized with a StringSession to avoid database file lock conflicts
relay_loop = asyncio.new_event_loop()
relay_tg_client = TelegramClient(StringSession(), API_ID, API_HASH, loop=relay_loop, proxy=build_telethon_proxy_config(PROXY_CONFIG))
tg_runtime = TelegramRuntime(tg_client, tg_loop)
relay_runtime = TelegramRuntime(relay_tg_client, relay_loop)

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
TDL_MAX_EOF_RETRIES = 15
TDL_RESTART_RESET_MIN_BYTES = 64 * 1024 * 1024
tdl_runtime = TdlRuntime(
    binary=TDL_BINARY,
    namespace=TDL_NAMESPACE,
    storage_path=TDL_STORAGE_PATH,
    threads=TDL_THREADS,
    limit=TDL_LIMIT,
    proxy_config=PROXY_CONFIG,
    has_fallback_channel=lambda entity_id: _has_tdl_fallback_channel(entity_id),
    chat_id_overrides=TDL_CHAT_ID_OVERRIDES,
)



def _watchdog_tasks_snapshot():
    with status_lock:
        return [(task_id, dict(task)) for task_id, task in download_status.items()]


def _restart_stalled_download(task_id, task):
    dialog_name = task.get('dialog_name', '')

    with status_lock:
        if task_id in download_status:
            download_status[task_id]['status'] = 'error'
            download_status[task_id]['error'] = 'watchdog 检测到停滞，已释放槽位并重新排队...'

    _mark_download_cancelled(task_id)
    released = download_scheduler.release_scheduled_task(task_id)
    if released:
        log_warning(f"[watchdog] 任务 {task_id} 已释放下载槽位，准备重新排队")
    else:
        log_warning(f"[watchdog] 任务 {task_id} 未占用调度槽位，直接尝试重新排队")
    process_queue()
    time.sleep(2)
    _clear_download_cancelled(task_id)
    result = _resume_task(task_id, dialog_name=dialog_name, auto=True)
    if not result.get("ok"):
        process_queue()
    return result


# 全局看门狗实例
download_watchdog = DownloadWatchdog(
    check_interval=60,      # 每 60 秒检查一次
    stall_timeout=300,      # 5 分钟无进度视为停滞
    get_tasks_callback=_watchdog_tasks_snapshot,
    restart_task_callback=_restart_stalled_download,
    log_info=log_info,
    log_warning=log_warning,
    log_error=log_error,
)



# 全局健康检查实例（需要在 tg_client 初始化后创建）
tg_health_checker = None


def _mark_tg_reconnected():
    global tg_connected
    tg_connected = True


def init_tg_health_checker():
    """初始化 Telegram 健康检查器"""
    global tg_health_checker
    if tg_health_checker is None:
        tg_health_checker = TelegramHealthChecker(
            client=tg_client,
            loop=tg_loop,
            check_interval=120,    # 每 2 分钟检查一次
            max_retry=3,           # 连续 3 次失败触发重连
            on_reconnect_callback=_mark_tg_reconnected,
            log_info=log_info,
            log_warning=log_warning,
            log_error=log_error,
        )
        tg_health_checker.start()


TDL_MAX_RETRY_ATTEMPTS = 5
TDL_MAX_STALLED_EOF_RETRIES = 2



def _is_local_bind_only():
    return is_local_bind_only(WEB_BIND_HOST)


def _request_ip_is_local():
    return current_request_is_local()


def current_request_is_local():
    return request_ip_is_local(
        request.remote_addr or "",
        request.headers.get("X-Forwarded-For", ""),
        TRUST_FORWARDED_FOR,
    )


def _require_web_auth():
    return require_web_auth(
        request,
        WEB_BIND_HOST,
        WEB_AUTH_USERNAME,
        WEB_AUTH_PASSWORD,
        trust_forwarded=TRUST_FORWARDED_FOR,
    )


def abort_if_debug_disabled():
    if DEBUG_API_ENABLED:
        return None
    return jsonify({"error": "Debug API disabled"}), 404


def _abort_if_debug_disabled():
    return abort_if_debug_disabled()


def _resolve_download_path(*parts, must_exist=False):
    return resolve_current_download_path(*parts, must_exist=must_exist)


def resolve_current_download_path(*parts, must_exist=False):
    return resolve_download_path_under(DOWNLOAD_DIR, *parts, must_exist=must_exist)


def copy_task_state(task_id):
    with status_lock:
        state = download_status.get(task_id)
        return dict(state) if state else None


def _copy_task_state(task_id):
    return copy_task_state(task_id)


TASK_STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".task_state")
task_persistence = TaskStatePersistence(
    state_dir=TASK_STATE_DIR,
    terminal_states=TERMINAL_STATES,
    warning_logger=log_warning,
)


def _get_task_state_file(task_id):
    return task_persistence.legacy_state_file(task_id)


def _task_persistence_enabled():
    return task_persistence.enabled()


def _migrate_legacy_task_state_files():
    task_persistence.migrate_legacy_state_files()


def _persist_task_state(task_id, state):
    task_persistence.persist_state(task_id, state)


def _delete_persisted_task_state(task_id):
    task_persistence.delete_state(task_id)


def load_persisted_task_states():
    states, loaded = task_persistence.load_states()
    download_status.update(states)
    return loaded


def _load_persisted_task_states():
    return load_persisted_task_states()


def _count_persisted_task_states():
    return task_persistence.count_states()


def backup_task_database():
    return task_persistence.backup_database()


def run_task_database_backup_loop():
    while True:
        backup_task_database()
        time.sleep(24 * 3600)


def _query_task_history(status="", query="", page=1, per_page=30):
    with status_lock:
        live_items = [(task_id, dict(state)) for task_id, state in download_status.items()]
    return task_persistence.query_history(live_items, status, query, page, per_page)


def _remember_tdl_fallback_channel(entity_id, reason):
    task_persistence.remember_tdl_fallback_channel(entity_id, reason)


def _has_tdl_fallback_channel(entity_id):
    return task_persistence.has_tdl_fallback_channel(entity_id)


def set_task_state(task_id, state):
    with status_lock:
        state = dict(state)
        state["updated_at"] = time.time()
        download_status[task_id] = state
        _persist_task_state(task_id, state)
        return dict(state)


def _set_task_state(task_id, state):
    return set_task_state(task_id, state)


def _find_matching_download_task(folder, filename):
    with status_lock:
        tasks = list(download_status.values())
    for task in tasks:
        if task.get("filename") != filename:
            continue
        dialog_name = str(task.get("dialog_name") or "")
        if dialog_name and dialog_name != folder:
            continue
        return dict(task)
    return None


def _annotate_download_file_item(item):
    task = _find_matching_download_task(item.get("folder"), item.get("filename"))
    if not task:
        item["playable"] = item.get("size_bytes", 0) > 0
        return item

    status = task.get("status") or ""
    expected_bytes = int(task.get("expected_bytes") or task.get("total_bytes") or 0)
    actual_bytes = int(item.get("size_bytes") or task.get("final_bytes") or 0)
    complete = status in TERMINAL_STATES and status == "done"
    if expected_bytes > 0 and actual_bytes < expected_bytes:
        complete = False

    item["task_status"] = status
    item["task_progress"] = task.get("progress")
    item["expected_bytes"] = expected_bytes
    item["playable"] = complete and actual_bytes > 0
    if not item["playable"]:
        if status not in TERMINAL_STATES:
            item["play_block_reason"] = "文件仍在下载中，完成后才能播放"
        elif actual_bytes <= 0:
            item["play_block_reason"] = "文件大小为 0B，无法播放"
        elif expected_bytes > 0 and actual_bytes < expected_bytes:
            item["play_block_reason"] = "文件未下载完整，无法播放"
        else:
            item["play_block_reason"] = task.get("error") or "文件状态异常，无法播放"
    return item


def _download_file_play_block_reason(folder, filename, size_bytes):
    item = _annotate_download_file_item({"folder": folder, "filename": filename, "size_bytes": size_bytes})
    return item.get("play_block_reason") if not item.get("playable") else ""


def update_task_state(task_id, **updates):
    with status_lock:
        state = download_status.get(task_id)
        if state is None:
            return None
        state.update(updates)
        state["updated_at"] = time.time()
        _persist_task_state(task_id, state)
        return dict(state)


def _update_task_state(task_id, **updates):
    return update_task_state(task_id, **updates)


def drop_task_state(task_id):
    with status_lock:
        state = download_status.pop(task_id, None)
        _delete_persisted_task_state(task_id)
        return state


def _drop_task_state(task_id):
    return drop_task_state(task_id)


def _get_download_cancelled(task_id):
    with status_lock:
        return bool(download_cancel.get(task_id))


def mark_download_cancelled(task_id):
    with status_lock:
        download_cancel[task_id] = True


def _mark_download_cancelled(task_id):
    mark_download_cancelled(task_id)


def clear_download_cancelled(task_id):
    with status_lock:
        download_cancel.pop(task_id, None)


def _clear_download_cancelled(task_id):
    clear_download_cancelled(task_id)


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
download_scheduler = DownloadScheduler(max_concurrent=MAX_CONCURRENT_DOWNLOADS)
TASK_STALL_TIMEOUT = 600
TELEGRAM_CHUNK_TIMEOUT = 60
TELEGRAM_MAX_RETRY_ATTEMPTS = 12


def _is_task_queued_locked(task_id):
    return download_scheduler.is_queued(task_id)


def _recover_stalled_tasks(force=False, timeout=TASK_STALL_TIMEOUT):
    now = time.time()
    repaired = []

    with download_scheduler.lock:
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
                download_scheduler.release_scheduled_task(task_id)
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
    """刷新队列中任务的排位信息。"""
    def update_task(tid, idx, queue_length):
        with status_lock:
            state = download_status.get(tid)
            if not state:
                return
            state["queue_position"] = idx
            state["queue_size"] = queue_length
            if state.get("status") not in TERMINAL_STATES and state.get("status") != "downloading":
                state["status"] = "queued"
            state["updated_at"] = time.time()
            _persist_task_state(tid, state)

    download_scheduler.update_positions(update_task)

RESUME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".resume")
resume_store = ResumeStore(RESUME_DIR, progress_path_func=resolve_tdl_progress_path)

def _get_resume_file(task_id):
    return resume_store.path_for(task_id)

def save_resume_info(task_id, info):
    resume_store.save(task_id, info)

def load_resume_info(task_id):
    return resume_store.load(task_id)

def clear_resume_info(task_id):
    resume_store.clear(task_id)


def restore_resume_tasks_into_memory():
    for task_id in resume_store.list_task_ids():
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


def _restore_resume_tasks_into_memory():
    return restore_resume_tasks_into_memory()


def add_to_queue(task):
    download_scheduler.add_task(task, update_positions=_update_queue_positions_locked)

def get_next_from_queue():
    return download_scheduler.get_next_task(update_positions=_update_queue_positions_locked)

def get_queue_status():
    return download_scheduler.get_status()

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


def resume_task(task_id, dialog_name=None, auto=False):
    return _resume_task(task_id, dialog_name=dialog_name, auto=auto)


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


def resume_all_incomplete_tasks(auto=False):
    return _resume_all_incomplete_tasks(auto=auto)


def remove_from_queue(task_id):
    return download_scheduler.remove_task(task_id, update_positions=_update_queue_positions_locked)


def move_queued_task(task_id, action):
    return download_scheduler.move_task(task_id, action, update_positions=_update_queue_positions_locked)


def log_recovery_candidates(limit=200):
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


def _log_recovery_candidates(limit=200):
    return log_recovery_candidates(limit)


THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)
THUMB_MAX_AGE_SECONDS = 30 * 24 * 3600
THUMB_MAX_BYTES = 128 * 1024 * 1024


def cleanup_thumbnail_cache():
    try:
        result = cleanup_thumbnail_cache_under(
            THUMB_DIR,
            max_age_seconds=THUMB_MAX_AGE_SECONDS,
            max_bytes=THUMB_MAX_BYTES,
        )
        log_info(f"缩略图缓存清理完成: {format_size(result['bytes'])}")
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


tg_runtime.format_user_display = _format_user_display
relay_runtime.format_user_display = _format_user_display


def _sync_tg_runtime_state():
    global tg_connected, tg_connect_error, tg_user_info
    tg_connected = tg_runtime.connected
    tg_connect_error = tg_runtime.connect_error
    tg_user_info = tg_runtime.user_info


def _mark_tg_connecting(message):
    global tg_connected, tg_connect_error
    tg_connected = False
    tg_connect_error = message


def _mark_tg_connected(user_info):
    global tg_connected, tg_connect_error, tg_user_info
    tg_connected = True
    tg_connect_error = ""
    tg_user_info = user_info


def _mark_tg_error(message):
    global tg_connected, tg_connect_error
    tg_connected = False
    tg_connect_error = message


def _sync_relay_runtime_state():
    global relay_connected, relay_connect_error
    relay_connected = relay_runtime.connected
    relay_connect_error = relay_runtime.connect_error


def _mark_relay_connecting(message):
    global relay_connected, relay_connect_error
    relay_connected = False
    relay_connect_error = message


def _mark_relay_connected():
    global relay_connected, relay_connect_error
    relay_connected = True
    relay_connect_error = ""


def _mark_relay_error(message):
    global relay_connected, relay_connect_error
    relay_connected = False
    relay_connect_error = message


def _set_relay_client(client):
    global relay_tg_client
    relay_tg_client = client


def ensure_tg_connection(allow_reconnect=True):
    ok = tg_runtime.ensure_connection(allow_reconnect=allow_reconnect)
    _sync_tg_runtime_state()
    return ok


def ensure_relay_connection(allow_reconnect=True):
    ok = relay_runtime.ensure_connection(allow_reconnect=allow_reconnect)
    _sync_relay_runtime_state()
    return ok


def run_async(coro_factory, timeout=600, allow_reconnect=True):
    try:
        return tg_runtime.run_async(coro_factory, timeout=timeout, allow_reconnect=allow_reconnect)
    finally:
        _sync_tg_runtime_state()


def relay_run_async(coro_factory, timeout=600, allow_reconnect=True):
    try:
        return relay_runtime.run_async(
            coro_factory,
            timeout=timeout,
            allow_reconnect=allow_reconnect,
            error_label="Relay Telegram",
        )
    finally:
        _sync_relay_runtime_state()


def message_text(message):
    raw = getattr(message, "message", None) or getattr(message, "text", None) or ""
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw


def _message_text(message):
    return message_text(message)


def make_excerpt(text, limit=180):
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _make_excerpt(text, limit=180):
    return make_excerpt(text, limit)


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
    raw_text = message_text(message)
    return {
        "id": message.id,
        "document_id": str(getattr(doc, "id", "")),
        "filename": filename,
        "size": doc.size,
        "duration": duration,
        "date": message.date.strftime("%Y-%m-%d %H:%M"),
        "has_thumb": bool(doc.thumbs),
        "text": raw_text,
        "text_excerpt": make_excerpt(raw_text, 220),
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


_dialogs_cache = tg_runtime.dialogs_cache
_dialogs_serialized_cache = tg_runtime.dialogs_serialized_cache
_messages_cache = tg_runtime.messages_cache
_current_entity_cache = tg_runtime.current_entity_cache
_videos_cache = tg_runtime.videos_cache
_replies_cache = tg_runtime.replies_cache
_last_download_dialog = tg_runtime.last_download_dialog
dialogs_cache = _dialogs_cache
current_entity_cache = _current_entity_cache
videos_cache = _videos_cache
replies_cache = _replies_cache
last_download_dialog = _last_download_dialog

# 缓存大小上限，防止内存无限增长
MAX_DIALOG_CACHE_AGE = tg_runtime.max_dialog_cache_age
DIALOGS_FETCH_MAX = tg_runtime.dialog_fetch_max
MAX_MSG_CACHE_SIZE = tg_runtime.max_message_cache_size
MAX_VIDEO_CACHE_SIZE = 30
MAX_REPLY_CACHE_SIZE = 500


def _serialize_dialogs(dialogs):
    return tg_runtime.serialize_dialogs(dialogs)


def dialogs_cache_snapshot():
    return tg_runtime.dialogs_snapshot()


def _dialogs_cache_snapshot():
    return dialogs_cache_snapshot()


def _set_dialogs_refresh_error(message):
    tg_runtime.set_dialogs_refresh_error(message)


async def _collect_dialogs():
    return await tg_runtime.collect_dialogs()


def _refresh_dialogs_cache():
    tg_runtime.refresh_dialogs_cache()


def kickoff_dialogs_refresh(force=False):
    return tg_runtime.kickoff_dialogs_refresh(force=force)


def _kickoff_dialogs_refresh(force=False):
    return kickoff_dialogs_refresh(force=force)


def _get_entity_id(entity):
    return tg_runtime.entity_id(entity)


def _message_entity_id(message, fallback_entity_id=None):
    return tg_runtime.message_entity_id(message, fallback_entity_id)


def _make_msg_cache_key(entity_id, msg_id):
    return tg_runtime.make_msg_cache_key(entity_id, msg_id)


def make_task_id(entity_id, msg_id):
    return tg_runtime.make_task_id(entity_id, msg_id)


def _make_task_id(entity_id, msg_id):
    return make_task_id(entity_id, msg_id)


def _cache_message(message, entity_id):
    tg_runtime.cache_message(message, entity_id)


def video_info_for_message(message, current_entity_id, source="主消息", extra=None):
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


def _video_info_for_message(message, current_entity_id, source="主消息", extra=None):
    return video_info_for_message(message, current_entity_id, source=source, extra=extra)


telegram_video_service = TelegramVideoService(
    client=tg_client,
    run_async=run_async,
    resolve_requested_entity=lambda source="dialog", dialog_index=None, entity_id=None: resolve_requested_entity(
        source, dialog_index, entity_id
    ),
    video_info_for_message=video_info_for_message,
    message_text=message_text,
    make_excerpt=make_excerpt,
    cache_lock=cache_lock,
    current_entity_cache=_current_entity_cache,
    videos_cache=_videos_cache,
    replies_cache=_replies_cache,
    max_video_cache_size=MAX_VIDEO_CACHE_SIZE,
    max_reply_cache_size=MAX_REPLY_CACHE_SIZE,
    log_warning=log_warning,
)


telegram_debug_service = TelegramDebugService(
    client=tg_client,
    run_async=run_async,
    dialogs_cache=_dialogs_cache,
    cache_lock=cache_lock,
)


def resolve_requested_entity(source="dialog", dialog_index=None, entity_id=None):
    return tg_runtime.resolve_requested_entity(source, dialog_index, entity_id)


def _resolve_requested_entity(source="dialog", dialog_index=None, entity_id=None):
    return resolve_requested_entity(source, dialog_index, entity_id)


def _parse_task_id(task_id):
    if not task_id or ":" not in task_id:
        return (None, None)
    left, right = task_id.split(":", 1)
    try:
        return int(left), int(right)
    except ValueError:
        return (None, None)


def _sanitize_dialog_name(dialog_name):
    return sanitize_dialog_name(dialog_name)


def _public_base_url():
    base_url = (PUBLIC_BASE_URL or "").strip()
    if base_url:
        if base_url.startswith("https://"):
            base_url = "http://" + base_url[len("https://"):]
        return base_url.rstrip("/")
    return f"http://{request.host}".rstrip("/")


def _download_dir_for_dialog(dialog_name):
    return download_dir_for_dialog(DOWNLOAD_DIR, dialog_name)


def _tdl_proxy_url():
    return tdl_runtime.proxy_url()


def _resolve_tdl_progress_path(filepath):
    return resolve_tdl_progress_path(filepath)


def _prepare_telegram_fallback_target(filepath):
    return prepare_telegram_fallback_target(filepath)


def _detect_resume_offset(task_id, filepath, total_bytes=0):
    return resume_store.detect_offset(task_id, filepath, total_bytes)


def _should_retry_tdl_error(error_message, retry_count, current_size=0, last_retry_size=0):
    return tdl_rules.should_retry_tdl_error(
        error_message,
        retry_count,
        max_eof_retries=TDL_MAX_EOF_RETRIES,
        max_retry_attempts=TDL_MAX_RETRY_ATTEMPTS,
        max_stalled_eof_retries=TDL_MAX_STALLED_EOF_RETRIES,
        current_size=current_size,
        last_retry_size=last_retry_size,
    )


def _should_fallback_from_tdl(error_message):
    return tdl_rules.should_fallback_from_tdl(error_message)


def _classify_tdl_error(error_message):
    return tdl_rules.classify_tdl_error(error_message)


def _should_capture_tdl_error_line(line):
    return tdl_rules.should_capture_tdl_error_line(line)


def _reconcile_tdl_progress_size(current_size, written, allow_offset_correction):
    return tdl_rules.reconcile_tdl_progress_size(current_size, written, allow_offset_correction)


def _did_tdl_restart_from_scratch(retry_count, previous_size, current_size, start_offset=0):
    return tdl_rules.did_tdl_restart_from_scratch(
        retry_count,
        previous_size,
        current_size,
        start_offset=start_offset,
        restart_reset_min_bytes=TDL_RESTART_RESET_MIN_BYTES,
    )


def _validate_tdl_completion(total_bytes, final_size):
    return tdl_rules.validate_tdl_completion(total_bytes, final_size, format_size)


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
    tdl_runtime.clear_error(task_id)


def clear_tdl_error(task_id):
    _clear_tdl_error(task_id)


def _error_priority(message):
    return tdl_rules.error_priority(message)


def _choose_more_specific_tdl_error(current_message, candidate_message):
    return tdl_rules.choose_more_specific_tdl_error(current_message, candidate_message)


def get_tdl_status():
    return tdl_runtime.status()


def build_tdl_message_url(entity_id, msg_id):
    return tdl_runtime.build_message_url(entity_id, msg_id)


def supports_tdl_download(entity_id):
    return tdl_runtime.supports_download(entity_id)


def _supports_tdl_download(entity_id):
    return supports_tdl_download(entity_id)


def build_tdl_download_command(message_url, download_dir, output_name):
    return tdl_runtime.build_download_command(message_url, download_dir, output_name)


def _register_tdl_process(task_id, process):
    tdl_runtime.register_process(task_id, process)


def _drop_tdl_process(task_id):
    return tdl_runtime.drop_process(task_id)


def get_tdl_process(task_id):
    return tdl_runtime.get_process(task_id)


def _get_tdl_process(task_id):
    return get_tdl_process(task_id)


def _set_tdl_error(task_id, message):
    tdl_runtime.set_error(task_id, message)


download_manager = None


def get_download_manager():
    global download_manager
    if download_manager is None:
        download_manager = DownloadManager(
            tdl_binary=TDL_BINARY,
            supports_tdl_download=_supports_tdl_download,
            add_to_queue=add_to_queue,
            process_queue=process_queue,
        )
    return download_manager


def enqueue_tdl_download(task_id, entity_id, msg_id, dialog_name, info):
    return get_download_manager().enqueue_tdl(task_id, entity_id, msg_id, dialog_name, info)


def enqueue_telegram_download(task_id, entity_id, msg_id, dialog_name, info):
    return get_download_manager().enqueue_telegram(task_id, entity_id, msg_id, dialog_name, info)


def enqueue_download(task_id, entity_id, msg_id, dialog_name, info):
    return get_download_manager().enqueue(task_id, entity_id, msg_id, dialog_name, info)


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


def resolve_message(entity_id, msg_id, force_refresh=False):
    return tg_runtime.resolve_message(entity_id, msg_id, force_refresh=force_refresh)


def _resolve_message(entity_id, msg_id, force_refresh=False):
    return resolve_message(entity_id, msg_id, force_refresh=force_refresh)


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
            proxy=build_telethon_proxy_config(PROXY_CONFIG),
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
    return parse_range(range_header, total_size)


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


def get_cached_message(msg_id, entity_id=None):
    """根据 msg_id/对话 ID 从缓存里取消息，避免 ID 冲突"""
    return tg_runtime.get_cached_message(msg_id, entity_id)


def _get_cached_message(msg_id, entity_id=None):
    return get_cached_message(msg_id, entity_id)


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
        payload, status_code = prepare_open_folder(
            _resolve_download_path,
            folder,
            OPEN_FOLDER_ENABLED,
            _request_ip_is_local(),
        )
        return jsonify(payload), status_code
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
    try:
        rename_download_file(_resolve_download_path, folder, old_name, new_name)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except FileExistsError as e:
        return jsonify({"error": str(e)}), 400
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
        delete_download_file(_resolve_download_path, folder, filename)
        return jsonify({"ok": True})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


system_status_service = None
proxy_switch_lock = threading.RLock()

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_configured_proxy_type():
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == "TG_PROXY_TYPE":
                    try:
                        return normalize_proxy_type(value.split("#", 1)[0].strip())
                    except ValueError:
                        return value.split("#", 1)[0].strip().lower()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log_warning(f"读取代理配置失败: {exc}")
    if PROXY_CONFIG:
        return PROXY_CONFIG[0]
    return ""


def _write_configured_proxy_type(proxy_type):
    proxy_type = normalize_proxy_type(proxy_type)
    lines = []
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []

    updated = False
    output = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("TG_PROXY_TYPE="):
            output.append(f"TG_PROXY_TYPE={proxy_type}\n")
            updated = True
        else:
            output.append(line)
    if not updated:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.append(f"TG_PROXY_TYPE={proxy_type}\n")

    tmp_path = f"{ENV_PATH}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.writelines(output)
    os.replace(tmp_path, ENV_PATH)
    return proxy_type


def _proxy_settings_payload(configured_type=None):
    active_type = PROXY_CONFIG[0] if PROXY_CONFIG else ""
    configured_type = configured_type or _read_configured_proxy_type()
    configured_proxy = None
    configured_label = "未启用"
    try:
        configured_proxy = build_proxy_config(configured_type) if configured_type else None
        configured_label = proxy_config_label(configured_proxy)
    except ValueError:
        configured_label = configured_type or "未启用"
    return {
        "proxy_type": configured_type,
        "configured_proxy_type": configured_type,
        "active_proxy_type": active_type,
        "allowed_types": list(ALLOWED_PROXY_TYPES),
        "label": proxy_config_label(PROXY_CONFIG),
        "configured_label": configured_label,
        "restart_required": False,
    }


def _active_download_tasks():
    with status_lock:
        return [
            task_id
            for task_id, state in download_status.items()
            if state.get("status") not in TERMINAL_STATES and state.get("status") != "paused"
        ]


def _refresh_proxy_dependents(proxy_config):
    if system_status_service is not None:
        system_status_service.proxy_config = proxy_config
        with system_status_service._health_cache_lock:
            system_status_service._health_cache.clear()
    try:
        from src.routes import system as system_routes
        if getattr(system_routes, "_status_service", None) is not None:
            system_routes._status_service.proxy_config = proxy_config
            with system_routes._status_service._health_cache_lock:
                system_routes._status_service._health_cache.clear()
    except Exception as exc:
        log_warning(f"刷新系统状态代理配置失败: {exc}")
    try:
        from src.routes import telegram as telegram_routes
        telegram_routes._tg_client = tg_client
        if getattr(telegram_routes, "_video_service", None) is not None:
            telegram_routes._video_service.client = tg_client
    except Exception as exc:
        log_warning(f"刷新 Telegram 路由代理配置失败: {exc}")
    tdl_runtime.proxy_config = proxy_config


def _apply_proxy_type(proxy_type):
    global PROXY_CONFIG, TELETHON_PROXY_CONFIG, tg_client, relay_tg_client, tg_health_checker
    global tg_connected, tg_connect_error, tg_user_info, relay_connected, relay_connect_error

    new_proxy_config = build_proxy_config(proxy_type)
    new_telethon_proxy = build_telethon_proxy_config(new_proxy_config)
    if new_proxy_config == PROXY_CONFIG:
        _refresh_proxy_dependents(new_proxy_config)
        return {"reconnected": False}

    active_tasks = _active_download_tasks()
    if active_tasks:
        raise RuntimeError(f"当前有 {len(active_tasks)} 个下载/排队任务，请完成或暂停后再切换代理")

    with proxy_switch_lock:
        active_tasks = _active_download_tasks()
        if active_tasks:
            raise RuntimeError(f"当前有 {len(active_tasks)} 个下载/排队任务，请完成或暂停后再切换代理")

        log_info(f"正在切换 Telegram 代理到 {proxy_config_label(new_proxy_config)}")
        _mark_tg_connecting("正在切换 Telegram 代理...")
        _mark_relay_connecting("正在切换 Relay 代理...")

        old_health_checker = tg_health_checker
        if old_health_checker is not None:
            old_health_checker.stop()

        old_tg_client = tg_client
        old_relay_client = relay_tg_client

        async def _switch_main():
            try:
                await old_tg_client.disconnect()
            except Exception:
                pass
            client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop, proxy=new_telethon_proxy)
            await client.connect()
            if not await client.is_user_authorized():
                raise Exception("Telegram 未登录，请先运行 login.py 完成登录。")
            user = await client.get_me()
            return client, _format_user_display(user)

        new_tg_client, user_info = asyncio.run_coroutine_threadsafe(_switch_main(), tg_loop).result(timeout=60)

        try:
            async def _switch_relay():
                try:
                    await old_relay_client.disconnect()
                except Exception:
                    pass
                session_string = StringSession.save(new_tg_client.session)
                client = TelegramClient(StringSession(session_string), API_ID, API_HASH, loop=relay_loop, proxy=new_telethon_proxy)
                await client.connect()
                if not await client.is_user_authorized():
                    raise Exception("Relay Telegram 未登录")
                return client

            new_relay_client = asyncio.run_coroutine_threadsafe(_switch_relay(), relay_loop).result(timeout=60)
            relay_tg_client = new_relay_client
            relay_runtime.client = new_relay_client
            relay_runtime.mark_connected()
            _mark_relay_connected()
        except Exception as exc:
            relay_runtime.mark_error(f"Relay 代理切换失败: {exc}")
            _mark_relay_error(f"Relay 代理切换失败: {exc}")
            log_warning(f"Relay 代理切换失败: {exc}")

        tg_client = new_tg_client
        tg_runtime.client = new_tg_client
        tg_runtime.mark_connected(user_info)
        _mark_tg_connected(user_info)

        PROXY_CONFIG = new_proxy_config
        TELETHON_PROXY_CONFIG = new_telethon_proxy
        _refresh_proxy_dependents(new_proxy_config)

        tg_health_checker = None
        init_tg_health_checker()

        log_info(f"Telegram 代理已切换到 {proxy_config_label(new_proxy_config)}")
        return {"reconnected": True}


@app.route("/api/settings/proxy", methods=["GET"])
def api_get_proxy_settings():
    return jsonify(_proxy_settings_payload())


@app.route("/api/settings/proxy", methods=["POST"])
def api_set_proxy_settings():
    data = request.json or {}
    try:
        proxy_type = normalize_proxy_type(data.get("proxy_type"))
    except ValueError:
        return jsonify({
            "ok": False,
            "error": "代理模式已固定为 http",
            "allowed_types": list(ALLOWED_PROXY_TYPES),
        }), 400

    try:
        apply_result = _apply_proxy_type(proxy_type)
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 409
    except Exception as exc:
        log_error(f"代理切换失败: {exc}")
        return jsonify({"ok": False, "error": f"代理切换失败: {exc}"}), 500

    try:
        _write_configured_proxy_type(proxy_type)
    except OSError as exc:
        return jsonify({"ok": False, "error": f"代理已切换，但保存配置失败: {exc}"}), 500

    payload = _proxy_settings_payload(configured_type=proxy_type)
    payload.update({
        "ok": True,
        "applied": True,
        "reconnected": apply_result.get("reconnected", False),
        "message": "代理已切换并保存",
    })
    return jsonify(payload)


def get_system_status_service():
    global system_status_service
    if system_status_service is None:
        system_status_service = SystemStatusService(
            ensure_tg_connection=ensure_tg_connection,
            get_tg_connected=lambda: tg_connected,
            get_tg_error=lambda: tg_connect_error,
            get_tg_user=lambda: tg_user_info,
            get_queue_status=get_queue_status,
            get_tdl_status=get_tdl_status,
            proxy_config=PROXY_CONFIG,
            tdl_binary=TDL_BINARY,
            get_tasks_persisted=_count_persisted_task_states,
            get_resume_count=resume_store.count,
            get_relay_status=lambda: {
                "connected": bool(tg_connected),
                "error": tg_connect_error,
                "mode": "main-client",
                "active": active_relays if "active_relays" in globals() else 0,
                "max": MAX_CONCURRENT_RELAYS if "MAX_CONCURRENT_RELAYS" in globals() else 0,
            },
        )
    return system_status_service


@app.route("/api/status")
def api_status():
    return jsonify(get_system_status_service().status_payload())


def _proxy_status():
    return get_system_status_service().proxy_status()


def _tdl_version_summary():
    return get_system_status_service().tdl_version_summary()


@app.route("/api/health")
def api_health():
    return jsonify(get_system_status_service().health_payload())


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
    payload, status = telegram_debug_service.inspect_messages(request.args.get("dialog_index", type=int), limit=20)
    return jsonify(payload), status


@app.route("/api/debug_replies")
def api_debug_replies():
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error
    payload, status = telegram_debug_service.inspect_messages(
        request.args.get("dialog_index", type=int),
        limit=20,
        reply_to=request.args.get("post_id", type=int),
    )
    return jsonify(payload), status



@app.route("/api/debug_full")
def api_debug_full():
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error
    payload, status = telegram_debug_service.inspect_full_messages(request.args.get("dialog_index", type=int))
    return jsonify(payload), status

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
        payload, status = telegram_video_service.list_videos(
            dialog_index=dialog_index,
            entity_id=entity_id,
            source=source,
            limit=limit,
            include_replies=include_replies,
            reply_post_limit=reply_post_limit,
            refresh=refresh,
        )
        return jsonify(payload), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/video_search")
def api_video_search():
    query = request.args.get("q", "").strip()
    dialog_index = request.args.get("dialog_index", type=int)
    entity_id = request.args.get("entity_id", type=int)
    source = request.args.get("source", "dialog")
    limit = min(max(request.args.get("limit", 200, type=int), 10), 1000)
    scan_limit = min(max(request.args.get("scan_limit", 1000, type=int), limit), 5000)
    include_comments = request.args.get("include_comments", "true") == "true"
    comment_post_limit = min(max(request.args.get("comment_post_limit", 80, type=int), 0), 300)
    comment_limit = min(max(request.args.get("comment_limit", 100, type=int), 10), 300)

    try:
        payload, status = telegram_video_service.search_videos(
            query=query,
            dialog_index=dialog_index,
            entity_id=entity_id,
            source=source,
            limit=limit,
            scan_limit=scan_limit,
            include_comments=include_comments,
            comment_post_limit=comment_post_limit,
            comment_limit=comment_limit,
        )
        return jsonify(payload), status
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
        payload, status = telegram_video_service.list_replies(
            entity_id=entity_id,
            post_id=post_id,
            limit=limit,
            refresh=refresh,
        )
        return jsonify(payload), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumb/<int:msg_id>")
def api_thumb(msg_id):
    entity_id = request.args.get("entity", type=int)
    thumb_path = thumbnail_cache_path(THUMB_DIR, entity_id, msg_id)
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype="image/jpeg")
    message = _get_cached_message(msg_id, entity_id)
    if not message:
        return Response(status=404)
    try:
        data = run_async(lambda: tg_client.download_media(message, file=bytes, thumb=-1), allow_reconnect=False)
        if not data:
            return Response(status=404)
        write_thumbnail(THUMB_DIR, entity_id, msg_id, data)
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
            "url": build_relay_url(entity_id, msg_id, file_name),
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
    return jsonify(recover_tasks_from_candidates(task_ids, dialog_name=dialog_name))


@app.route("/api/history")
def api_history():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 30, type=int)
    return jsonify(get_task_history_payload(
        status=request.args.get("status", "").strip(),
        query=request.args.get("q", "").strip(),
        page=page,
        per_page=per_page,
    ))


def clear_tasks_for_scope(scope="terminal"):
    return clear_tasks_by_scope(
        scope=scope,
        terminal_states=TERMINAL_STATES,
        status_lock=status_lock,
        download_status=download_status,
        drop_task_state=_drop_task_state,
        clear_download_cancelled=_clear_download_cancelled,
        clear_tdl_error=_clear_tdl_error,
        clear_resume_info=clear_resume_info,
    )


def get_task_history_payload(status="", query="", page=1, per_page=30):
    return build_task_history_payload(_query_task_history, status, query, page, per_page)


def recover_tasks_from_candidates(task_ids, dialog_name="日志恢复"):
    return recover_candidate_tasks(
        task_ids=task_ids,
        get_recovery_candidates=_log_recovery_candidates,
        resume_task=_resume_task,
        dialog_name=dialog_name,
    )


def clear_task_ids(task_ids):
    clearable_statuses = {"error", "cancelled"}
    with status_lock:
        cleared = 0
        skipped = 0
        for tid in list(task_ids or []):
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
    return {"ok": True, "cleared": cleared, "skipped": skipped}


def get_download_status_payload():
    return build_download_status_payload(
        recover_stalled_tasks=_recover_stalled_tasks,
        restore_resume_tasks=_restore_resume_tasks_into_memory,
        status_lock=status_lock,
        download_status=download_status,
        terminal_states=TERMINAL_STATES,
        drop_task_state=_drop_task_state,
        get_queue_status=get_queue_status,
    )


@app.route("/api/download_status")
def api_download_status():
    return jsonify(get_download_status_payload())


@app.route("/api/clear_tasks", methods=["POST"])
def api_clear_tasks():
    data = request.json or {}
    task_ids = data.get("task_ids")
    if task_ids is not None:
        return jsonify(clear_task_ids(task_ids))
    return jsonify({"ok": True, "cleared": clear_tasks_for_scope("terminal"), "skipped": 0})


telegram_direct_downloader = None


def get_telegram_direct_downloader():
    global telegram_direct_downloader
    if telegram_direct_downloader is None:
        telegram_direct_downloader = TelegramDirectDownloader(
            tg_client=tg_client,
            ensure_connection=ensure_tg_connection,
            run_async=run_async,
            resolve_message=_resolve_message,
            next_chunk=_next_telegram_chunk,
            detect_resume_offset=_detect_resume_offset,
            save_resume_info=save_resume_info,
            clear_resume_info=clear_resume_info,
            set_task_state=_set_task_state,
            update_task_state=_update_task_state,
            is_cancelled=_get_download_cancelled,
            should_retry_error=_should_retry_telegram_download_error,
            validate_completion=_validate_tdl_completion,
            calc_timeout=_calc_download_timeout,
            format_size=format_size,
            log_info=log_info,
            log_warning=log_warning,
            max_retry_attempts=TELEGRAM_MAX_RETRY_ATTEMPTS,
            chunk_timeout=TELEGRAM_CHUNK_TIMEOUT,
        )
    return telegram_direct_downloader


def _download_with_telegram(task_id, entity_id, msg_id, dialog_name, info, filepath):
    return get_telegram_direct_downloader().download(task_id, entity_id, msg_id, dialog_name, info, filepath)


tdl_download_executor = None


def get_tdl_download_executor():
    global tdl_download_executor
    if tdl_download_executor is None:
        tdl_download_executor = TdlDownloadExecutor(
            build_message_url=build_tdl_message_url,
            build_command=build_tdl_download_command,
            clear_tdl_error=_clear_tdl_error,
            register_process=_register_tdl_process,
            drop_process=_drop_tdl_process,
            get_process=_get_tdl_process,
            set_tdl_error=_set_tdl_error,
            last_tdl_error=tdl_runtime.last_error,
            stop_process=_stop_tdl_process,
            detect_resume_offset=_detect_resume_offset,
            resolve_progress_path=_resolve_tdl_progress_path,
            prepare_telegram_fallback_target=_prepare_telegram_fallback_target,
            save_resume_info=save_resume_info,
            clear_resume_info=clear_resume_info,
            update_task_state=_update_task_state,
            set_task_state=_set_task_state,
            copy_task_state=_copy_task_state,
            is_cancelled=_get_download_cancelled,
            should_capture_error_line=_should_capture_tdl_error_line,
            choose_more_specific_error=_choose_more_specific_tdl_error,
            reconcile_progress_size=_reconcile_tdl_progress_size,
            did_restart_from_scratch=_did_tdl_restart_from_scratch,
            should_retry_error=_should_retry_tdl_error,
            should_fallback=_should_fallback_from_tdl,
            remember_fallback_channel=_remember_tdl_fallback_channel,
            validate_completion=_validate_tdl_completion,
            download_with_telegram=_download_with_telegram,
            format_size=format_size,
            log_info=log_info,
            log_warning=log_warning,
            log_error=log_error,
            restart_reset_min_bytes=TDL_RESTART_RESET_MIN_BYTES,
        )
    return tdl_download_executor


download_worker = None


def get_download_worker():
    global download_worker
    if download_worker is None:
        download_worker = DownloadWorker(
            download_dir_for_dialog=_download_dir_for_dialog,
            release_tasks=download_scheduler.release_tasks,
            process_queue=process_queue,
            copy_task_state=_copy_task_state,
            set_task_state=_set_task_state,
            update_task_state=_update_task_state,
            is_cancelled=_get_download_cancelled,
            get_cached_message=_get_cached_message,
            resolve_message=_resolve_message,
            get_video_info=get_video_info,
            supports_tdl_download=_supports_tdl_download,
            download_with_telegram=_download_with_telegram,
            tdl_executor=get_tdl_download_executor,
            save_resume_info=save_resume_info,
            format_size=format_size,
            log_info=log_info,
            log_error=log_error,
        )
    return download_worker


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
    return get_download_worker().run(task_items, dialog_name)


@app.route("/api/stream/<path:filepath>")
def api_stream(filepath):
    """流式传输视频文件用于浏览器预览"""
    try:
        full_path = resolve_file_path(DOWNLOAD_DIR, filepath)
    except ValueError:
        return jsonify({"error": "非法路径"}), 403
    except FileNotFoundError:
        return jsonify({"error": "文件不存在"}), 404

    file_size = os.path.getsize(full_path)
    rel_path = os.path.relpath(full_path, os.path.realpath(DOWNLOAD_DIR))
    rel_parts = rel_path.split(os.sep, 1)
    if len(rel_parts) == 2:
        block_reason = _download_file_play_block_reason(rel_parts[0], rel_parts[1], file_size)
        if block_reason:
            return jsonify({"error": block_reason}), 409
    try:
        stream_range = local_stream_range(file_size, request.headers.get("Range"))
    except ValueError:
        return jsonify({"error": "invalid range"}), 416

    if stream_range:
        return Response(
            iter_file_chunks(full_path, stream_range["start"], stream_range["content_length"]),
            status=206,
            mimetype="video/mp4",
            headers={
            "Content-Range": stream_range["content_range"],
            "Content-Length": stream_range["content_length"],
            "Accept-Ranges": "bytes",
            },
        )
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
            log_warning(f"[relay:{entity_id}:{msg_id}] 并发数已达上限 {MAX_CONCURRENT_RELAYS}，请降低并发请求数")
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
    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    per_page = min(max(request.args.get("per_page", default=100, type=int) or 100, 10), 500)
    payload = list_download_files(DOWNLOAD_DIR, format_size, page, per_page)
    payload["files"] = [_annotate_download_file_item(item) for item in payload.get("files", [])]
    return jsonify(payload)


@app.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    try:
        full_path = resolve_file_path(DOWNLOAD_DIR, filepath)
    except ValueError:
        return jsonify({"error": "非法路径"}), 403
    except FileNotFoundError:
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(os.path.dirname(full_path), os.path.basename(full_path), as_attachment=True)


def start_tg_client():
    run_main_telegram_client(
        client=tg_client,
        loop=tg_loop,
        runtime=tg_runtime,
        format_user_display=_format_user_display,
        init_health_checker=init_tg_health_checker,
        on_connecting=_mark_tg_connecting,
        on_connected=_mark_tg_connected,
        on_error=_mark_tg_error,
        log_info=log_info,
    )


def start_relay_tg_client():
    run_relay_telegram_client(
        loop=relay_loop,
        runtime=relay_runtime,
        wait_for_main_ready=_wait_for_main_tg_ready,
        get_main_error=lambda: tg_connect_error,
        recreate_client=_recreate_relay_client_from_main_session,
        on_client_recreated=_set_relay_client,
        on_connecting=_mark_relay_connecting,
        on_connected=_mark_relay_connected,
        on_error=_mark_relay_error,
        log_info=log_info,
        log_warning=log_warning,
        log_error=log_error,
    )


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
    sys.modules.setdefault("app", sys.modules[__name__])
    import app_new

    app_new.validate_config()
    app_new.start_runtime()
    time.sleep(3)
    print(f"Web UI 启动: http://{WEB_BIND_HOST}:{WEB_BIND_PORT}")
    app_new.app.run(host=WEB_BIND_HOST, port=WEB_BIND_PORT, threaded=True)
