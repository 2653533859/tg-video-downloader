#!/usr/bin/env python3
"""Telegram 视频下载器 - Web UI"""

import os
import sys
import asyncio
import threading
import time
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, send_from_directory, send_file
from telethon import TelegramClient
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

from config import API_ID, API_HASH, DOWNLOAD_DIR, SESSION_NAME

app = Flask(__name__)

tg_loop = asyncio.new_event_loop()
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop, proxy=("socks5", "127.0.0.1", 7891))

# 连接状态
tg_connected = False
tg_connect_error = ""
tg_user_info = ""

# 下载状态: task_id(entity_id:msg_id) -> {filename, progress, status, downloaded, total, error, speed, entity_id, msg_id, dialog_name, downloaded_bytes, total_bytes, speed_bps, queue_position}
download_status = {}
# 下载取消标记: task_id -> True
download_cancel = {}
# 终止态集合，避免回调在任务完成后继续覆盖状态
TERMINAL_STATES = {"done", "skipped", "error", "cancelled"}

# ==================== 下载队列系统 ====================
MAX_CONCURRENT_DOWNLOADS = 3
download_queue = []
active_downloads = 0
queue_lock = threading.Lock()


def _update_queue_positions_locked():
    """刷新队列中任务的排位信息，需在持有 queue_lock 时调用"""
    queue_length = len(download_queue)
    for idx, task in enumerate(download_queue, start=1):
        tid = task.get("task_id")
        if not tid or tid not in download_status:
            continue
        state = download_status[tid]
        state["queue_position"] = idx
        state["queue_size"] = queue_length
        if state.get("status") not in TERMINAL_STATES and state.get("status") != "downloading":
            state["status"] = "queued"

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

def add_to_queue(task):
    with queue_lock:
        download_queue.append(task)
        _update_queue_positions_locked()

def get_next_from_queue():
    with queue_lock:
        if download_queue and active_downloads < MAX_CONCURRENT_DOWNLOADS:
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


def remove_from_queue(task_id):
    with queue_lock:
        for i, task in enumerate(download_queue):
            if task.get("task_id") == task_id:
                download_queue.pop(i)
                _update_queue_positions_locked()
                return True
    return False


THUMB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".thumbs")
os.makedirs(THUMB_DIR, exist_ok=True)

_reconnect_lock = threading.Lock()
_last_reconnect_attempt = 0.0


def _format_user_display(me):
    username = getattr(me, "username", None)
    if username:
        return f"{me.first_name} (@{username})"
    return me.first_name


def ensure_tg_connection(allow_reconnect=True):
    global tg_connected, tg_connect_error, tg_user_info, _last_reconnect_attempt

    if tg_client.is_connected():
        tg_connected = True
        if tg_connect_error.startswith("Telegram 已断开"):
            tg_connect_error = ""
        return True

    tg_connected = False

    if not tg_loop.is_running():
        tg_connect_error = "Telegram 客户端尚未启动，请稍后重试..."
        return False

    if not allow_reconnect:
        if not tg_connect_error:
            tg_connect_error = "Telegram 未连接，请等待重连..."
        return False

    now = time.time()
    if now - _last_reconnect_attempt < 8:
        if not tg_connect_error:
            tg_connect_error = "Telegram 重连中，请稍后重试..."
        return False

    with _reconnect_lock:
        now = time.time()
        if tg_client.is_connected():
            tg_connected = True
            tg_connect_error = ""
            return True

        if now - _last_reconnect_attempt < 8:
            tg_connect_error = tg_connect_error or "Telegram 重连中，请稍后重试..."
            return False

        _last_reconnect_attempt = now
        tg_connect_error = "Telegram 已断开，正在重连..."

        try:
            async def _reconnect():
                await tg_client.connect()
                if not await tg_client.is_user_authorized():
                    raise Exception("Telegram 未登录，请先运行 downloader.py 登录。")
                me = await tg_client.get_me()
                return _format_user_display(me)

            tg_user_info = asyncio.run_coroutine_threadsafe(_reconnect(), tg_loop).result(timeout=45)
            tg_connected = True
            tg_connect_error = ""
            return True
        except Exception as e:
            tg_connected = False
            tg_connect_error = f"Telegram 重连失败: {e}"
            return False


def run_async(coro_factory, timeout=600, allow_reconnect=True):
    global tg_connected, tg_connect_error

    if not callable(coro_factory):
        raise TypeError("run_async expects a callable returning coroutine")

    if not ensure_tg_connection(allow_reconnect=allow_reconnect):
        raise Exception(tg_connect_error or "Telegram 未连接，请等待重连...")

    future = asyncio.run_coroutine_threadsafe(coro_factory(), tg_loop)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        msg = str(e).lower()
        if "disconnected" in msg or "connection reset" in msg or "could not connect to proxy" in msg:
            tg_connected = False
            tg_connect_error = f"Telegram 连接中断: {e}"
        raise


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
    return {
        "id": message.id,
        "filename": filename,
        "size": doc.size,
        "duration": duration,
        "date": message.date.strftime("%Y-%m-%d %H:%M"),
        "has_thumb": bool(doc.thumbs),
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
_messages_cache = {}
_current_entity_cache = {}
_videos_cache = {}
_last_download_dialog = ""

# 缓存大小上限，防止内存无限增长
MAX_MSG_CACHE_SIZE = 2000
MAX_VIDEO_CACHE_SIZE = 30


def _get_entity_id(entity):
    if not entity:
        return None
    return getattr(entity, "id", None)


def _make_msg_cache_key(entity_id, msg_id):
    if entity_id is None or msg_id is None:
        return None
    return (int(entity_id), int(msg_id))


def _make_task_id(entity_id, msg_id):
    if entity_id is None or msg_id is None:
        return None
    return f"{int(entity_id)}:{int(msg_id)}"


def _parse_task_id(task_id):
    if not task_id or ":" not in task_id:
        return (None, None)
    left, right = task_id.split(":", 1)
    try:
        return int(left), int(right)
    except ValueError:
        return (None, None)


def _get_cached_message(msg_id, entity_id=None):
    """根据 msg_id/对话 ID 从缓存里取消息，避免 ID 冲突"""
    key = _make_msg_cache_key(entity_id, msg_id)
    if key and key in _messages_cache:
        return _messages_cache[key]
    if entity_id is None:
        last_eid = _current_entity_cache.get("entity_id")
        key = _make_msg_cache_key(last_eid, msg_id)
        if key and key in _messages_cache:
            return _messages_cache[key]
    for (eid, mid), message in _messages_cache.items():
        if mid == msg_id:
            return message
    return None


def _cache_key(entity_id, limit, include_replies):
    return f"{entity_id}:{limit}:{include_replies}"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    ensure_tg_connection(allow_reconnect=True)
    return jsonify({
        "connected": tg_connected,
        "error": tg_connect_error,
        "user": tg_user_info,
        "queue": get_queue_status(),
    })


@app.route("/api/dialogs")
def api_dialogs():
    try:
        dialogs = run_async(lambda: tg_client.get_dialogs())
        _dialogs_cache.clear()
        _dialogs_cache.extend(dialogs)
        result = []
        for i, d in enumerate(dialogs):
            dtype = "频道" if d.is_channel else "群组" if d.is_group else "私聊"
            name = d.name
            # 识别个人收藏 (Saved Messages)
            is_saved = False
            try:
                if getattr(d.entity, "is_self", False):
                    name = "⭐ 个人收藏 (Saved Messages)"
                    is_saved = True
            except:
                pass
            
            result.append({
                "index": i, 
                "name": name, 
                "id": d.id, 
                "type": dtype, 
                "is_channel": d.is_channel,
                "is_saved": is_saved
            })
        
        # 将个人收藏置顶
        result.sort(key=lambda x: not x["is_saved"])
        
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "请输入搜索内容"}), 400
    try:
        entity = run_async(lambda: tg_client.get_entity(query))
        name = getattr(entity, "title", None) or getattr(entity, "first_name", query)
        _current_entity_cache["search_entity"] = entity
        _current_entity_cache["search_name"] = name
        return jsonify({"name": name, "id": getattr(entity, "id", 0), "source": "search"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/videos")
def api_videos():
    dialog_index = request.args.get("dialog_index", type=int)
    entity_id = request.args.get("entity_id", type=int)
    source = request.args.get("source", "dialog")
    limit = request.args.get("limit", 100, type=int)
    include_replies = request.args.get("include_replies", "false") == "true"
    refresh = request.args.get("refresh", "false") == "true"

    try:
        if source == "search" and "search_entity" in _current_entity_cache:
            entity = _current_entity_cache["search_entity"]
            name = _current_entity_cache.get("search_name", "unknown")
        elif source == "search" and entity_id:
            entity = run_async(lambda: tg_client.get_entity(entity_id))
            name = getattr(entity, "title", None) or "unknown"
        elif dialog_index is not None and dialog_index < len(_dialogs_cache):
            entity = _dialogs_cache[dialog_index].entity
            name = _dialogs_cache[dialog_index].name
        else:
            return jsonify({"error": "无效的对话"}), 400

        _current_entity_cache["entity"] = entity
        _current_entity_cache["name"] = name

        eid = getattr(entity, "id", entity_id)
        if eid is None:
            return jsonify({"error": "无法确定对话 ID"}), 400
        _current_entity_cache["entity_id"] = eid
        ck = _cache_key(eid, limit, include_replies)
        if not refresh and ck in _videos_cache:
            return jsonify({
                "videos": _videos_cache[ck].get("videos", []), 
                "posts_with_replies": _videos_cache[ck].get("posts_with_replies", []), 
                "cached": True
            })

        async def scan():
            videos = []
            posts_with_replies = []
            async for message in tg_client.iter_messages(entity, limit=limit):
                info = get_video_info(message)
                if info:
                    key = _make_msg_cache_key(eid, message.id)
                    if key:
                        _messages_cache[key] = message
                        if len(_messages_cache) > MAX_MSG_CACHE_SIZE:
                            for _ in range(min(100, len(_messages_cache) - MAX_MSG_CACHE_SIZE + 50)):
                                _messages_cache.pop(next(iter(_messages_cache)), None)
                    info["size_fmt"] = format_size(info["size"])
                    info["duration_fmt"] = format_duration(info["duration"])
                    info["source"] = "主消息"
                    videos.append(info)
                if include_replies and message.replies and message.replies.replies > 0:
                    posts_with_replies.append({"id": message.id, "count": message.replies.replies})
            return videos, posts_with_replies

        videos, posts_with_replies = run_async(scan)
        _videos_cache[ck] = {"videos": videos, "posts_with_replies": posts_with_replies, "time": time.time()}
        while len(_videos_cache) > MAX_VIDEO_CACHE_SIZE:
            _videos_cache.pop(next(iter(_videos_cache)))
        return jsonify({
            "videos": videos, 
            "posts_with_replies": posts_with_replies if include_replies else [],
            "cached": False
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies")
def api_replies():
    entity_id = request.args.get("entity_id", type=int)
    post_id = request.args.get("post_id", type=int)
    if not entity_id or not post_id:
        return jsonify({"error": "缺少参数"}), 400
    
    try:
        async def scan_one_post_replies():
            entity = _current_entity_cache.get("entity")
            if not entity or getattr(entity, "id", 0) != entity_id:
                entity = await tg_client.get_entity(entity_id)
            
            rv = []
            try:
                async for reply in tg_client.iter_messages(entity, reply_to=post_id, limit=100):
                    ri = get_video_info(reply)
                    if ri:
                        key = _make_msg_cache_key(getattr(entity, "id", entity_id), reply.id)
                        if key:
                            _messages_cache[key] = reply
                        ri["size_fmt"] = format_size(ri["size"])
                        ri["duration_fmt"] = format_duration(ri["duration"])
                        ri["source"] = f"评论@帖子{post_id}"
                        rv.append(ri)
            except Exception as e:
                print(f"扫描帖子 {post_id} 评论失败: {e}")
            return rv

        replies_videos = run_async(scan_one_post_replies)
        return jsonify({"videos": replies_videos})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumb/<int:msg_id>")
def api_thumb(msg_id):
    entity_id = request.args.get("entity", type=int)
    thumb_path = os.path.join(THUMB_DIR, f"{msg_id}.jpg")
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


@app.route("/api/download", methods=["POST"])
def api_download():
    global _last_download_dialog
    data = request.json
    message_ids = data.get("message_ids", [])
    dialog_name = data.get("dialog_name", "unknown")
    entity_id = data.get("entity_id") or _current_entity_cache.get("entity_id")
    if not message_ids:
        return jsonify({"error": "参数不完整"}), 400
    if entity_id is None:
        return jsonify({"error": "缺少对话 ID"}), 400

    _last_download_dialog = dialog_name
    tasks = []
    for mid in message_ids:
        task_id = _make_task_id(entity_id, mid)
        if not task_id:
            continue
        msg = _get_cached_message(mid, entity_id)
        fname = "unknown"
        info = None
        if msg:
            info = get_video_info(msg)
            if info:
                fname = info["filename"]
        total_bytes = info.get("size") if info else 0
        download_status[task_id] = {
            "filename": fname,
            "progress": 0,
            "status": "waiting",
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
        }
        download_cancel.pop(task_id, None)
        tasks.append({"task_id": task_id, "entity_id": entity_id, "msg_id": mid})

    if not tasks:
        return jsonify({"error": "未找到可下载的消息，请刷新后重试"}), 400

    # 根据队列状态决定立即下载或加入队列
    queue_status = get_queue_status()
    
    if queue_status["active"] < MAX_CONCURRENT_DOWNLOADS:
        thread = threading.Thread(target=_do_download, args=(tasks, dialog_name), daemon=True)
        thread.start()
    else:
        for task in tasks:
            task["dialog_name"] = dialog_name
            add_to_queue(task)
    
    return jsonify({
        "status": "started", 
        "count": len(tasks),
        "queued": queue_status["queued"],
        "active": queue_status["active"],
        "max": MAX_CONCURRENT_DOWNLOADS
    })


@app.route("/api/cancel", methods=["POST"])
def api_cancel():
    data = request.json
    task_id = data.get("task_id")
    entity_id = data.get("entity_id")
    msg_id = data.get("msg_id")
    if not task_id and msg_id is not None and entity_id is not None:
        task_id = _make_task_id(entity_id, msg_id)
    if task_id:
        download_cancel[task_id] = True
        removed = remove_from_queue(task_id)
        if task_id in download_status:
            download_status[task_id]["status"] = "cancelled"
            download_status[task_id]["error"] = "已取消"
            download_status[task_id]["speed"] = ""
            download_status[task_id]["speed_bps"] = 0.0
            download_status[task_id]["queue_position"] = None
            download_status[task_id]["queue_size"] = 0
    return jsonify({"ok": True})


@app.route("/api/retry", methods=["POST"])
def api_retry():
    data = request.json
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
        for tid, info in download_status.items():
            if info.get("msg_id") == msg_id:
                task_id = tid
                if entity_id is None:
                    entity_id = info.get("entity_id")
                if not dialog_name:
                    dialog_name = info.get("dialog_name", dialog_name)
                break

    if not task_id or entity_id is None or msg_id is None:
        return jsonify({"error": "消息未找到"}), 400

    stored = download_status.get(task_id, {})
    if not dialog_name:
        dialog_name = stored.get("dialog_name", _last_download_dialog)

    msg = _get_cached_message(msg_id, entity_id)
    if not msg:
        return jsonify({"error": "消息未找到，请重新扫描"}), 400

    info = get_video_info(msg)
    fname = info["filename"] if info else stored.get("filename", "unknown")
    total_bytes = info.get("size") if info else stored.get("total_bytes", 0) or 0
    total_display = format_size(total_bytes) if total_bytes else stored.get("total", "")

    download_cancel.pop(task_id, None)
    download_status[task_id] = {
        "filename": fname,
        "progress": 0,
        "status": "waiting",
        "downloaded": "0B" if total_bytes else "",
        "total": total_display,
        "error": "",
        "speed": "",
        "msg_id": msg_id,
        "entity_id": entity_id,
        "dialog_name": dialog_name,
        "downloaded_bytes": 0,
        "total_bytes": total_bytes,
        "speed_bps": 0.0,
        "queue_position": None,
        "queue_size": 0,
    }
    task_item = {"task_id": task_id, "entity_id": entity_id, "msg_id": msg_id}
    queue_status = get_queue_status()
    if queue_status["active"] < MAX_CONCURRENT_DOWNLOADS:
        threading.Thread(target=_do_download, args=([task_item], dialog_name), daemon=True).start()
    else:
        task_item["dialog_name"] = dialog_name
        add_to_queue(task_item)
    return jsonify({"ok": True})


@app.route("/api/download_status")
def api_download_status():
    tasks = {k: dict(v) for k, v in list(download_status.items())}
    return jsonify({"tasks": tasks, "queue": get_queue_status()})


@app.route("/api/clear_tasks", methods=["POST"])
def api_clear_tasks():
    data = request.json or {}
    task_ids = data.get("task_ids")
    if task_ids is None:
        task_ids = [k for k, v in list(download_status.items()) if v.get("status") in TERMINAL_STATES]
    cleared = 0
    for tid in list(task_ids):
        if download_status.pop(tid, None) is not None:
            cleared += 1
    return jsonify({"ok": True, "cleared": cleared})


@app.route("/api/progress")
def api_progress():
    def snapshot():
        try:
            # list() 防止迭代期间字典被其他线程修改导致 RuntimeError
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
    
    # 等待获取下载槽位
    while True:
        with queue_lock:
            if active_downloads < MAX_CONCURRENT_DOWNLOADS:
                active_downloads += 1
                break
        time.sleep(2)
    
    try:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in dialog_name)
        save_dir = os.path.join(DOWNLOAD_DIR, safe_name)
        os.makedirs(save_dir, exist_ok=True)
    except Exception as _e:
        log_error(f"_do_download: 创建目录失败: {_e}")
        with queue_lock:
            active_downloads -= 1
        process_queue()
        return

    for task in task_items:
        task_id = task.get("task_id")
        entity_id = task.get("entity_id")
        msg_id = task.get("msg_id")
        if not task_id or entity_id is None or msg_id is None:
            continue

        if download_cancel.get(task_id):
            state = download_status.get(task_id, {})
            state["status"] = "cancelled"
            state["error"] = "已取消"
            state["speed"] = ""
            state["speed_bps"] = 0.0
            state["queue_position"] = None
            state["queue_size"] = 0
            download_status[task_id] = state
            continue

        message = _get_cached_message(msg_id, entity_id)
        if not message:
            download_status[task_id] = {
                "filename": "unknown",
                "progress": 0,
                "status": "error",
                "downloaded": "",
                "total": "",
                "error": "消息未找到，请重新扫描",
                "speed": "",
                "msg_id": msg_id,
                "entity_id": entity_id,
                "dialog_name": dialog_name,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "speed_bps": 0.0,
                "queue_position": None,
                "queue_size": 0,
            }
            continue

        info = get_video_info(message)
        if not info:
            continue

        filepath = os.path.join(save_dir, info["filename"])

        if os.path.exists(filepath) and os.path.getsize(filepath) == info["size"]:
            final_size = info.get("size") or os.path.getsize(filepath)
            download_status[task_id] = {
                "filename": info["filename"],
                "progress": 100,
                "status": "skipped",
                "downloaded": format_size(final_size),
                "total": format_size(final_size),
                "error": "",
                "speed": "",
                "msg_id": msg_id,
                "entity_id": entity_id,
                "dialog_name": dialog_name,
                "downloaded_bytes": final_size,
                "total_bytes": final_size,
                "speed_bps": 0.0,
                "queue_position": None,
                "queue_size": 0,
            }
            continue

        total_bytes = info.get("size") or 0
        download_status[task_id] = {
            "filename": info["filename"],
            "progress": 0,
            "status": "downloading",
            "downloaded": "0B",
            "total": format_size(total_bytes) if total_bytes else "",
            "error": "",
            "speed": "",
            "msg_id": msg_id,
            "entity_id": entity_id,
            "dialog_name": dialog_name,
            "downloaded_bytes": 0,
            "total_bytes": total_bytes,
            "speed_bps": 0.0,
            "queue_position": None,
            "queue_size": 0,
        }

        def make_cb(tid, fname, expected_total):
            last = {"bytes": 0, "time": time.time()}

            def cb(current, _total):
                if download_cancel.get(tid):
                    raise Exception("下载已取消")
                state = download_status.get(tid)
                if state and state.get("status") in TERMINAL_STATES:
                    return
                total = _total or expected_total or (state.get("total_bytes") if state else 0) or 0
                now = time.time()
                elapsed = now - last["time"]
                speed_label = state.get("speed", "") if state else ""
                speed_bps = state.get("speed_bps", 0.0) if state else 0.0
                if elapsed >= 0.5:
                    delta = max(0, current - last["bytes"])
                    speed_bps = delta / elapsed if elapsed > 0 else 0.0
                    speed_label = format_size(speed_bps) + "/s" if speed_bps else ""
                    last["bytes"] = current
                    last["time"] = now
                pct = int(current / total * 100) if total else 0
                state = state or {
                    "filename": fname,
                    "progress": 0,
                    "status": "waiting",
                    "downloaded": "0B",
                    "total": format_size(total) if total else "",
                    "error": "",
                    "speed": "",
                    "msg_id": msg_id,
                    "entity_id": entity_id,
                    "dialog_name": dialog_name,
                    "downloaded_bytes": 0,
                    "total_bytes": total,
                    "speed_bps": 0.0,
                    "queue_position": None,
                    "queue_size": 0,
                }
                state.update({
                    "filename": fname,
                    "progress": pct,
                    "status": "downloading",
                    "downloaded": format_size(current),
                    "total": format_size(total) if total else state.get("total", ""),
                    "error": "",
                    "speed": speed_label,
                    "downloaded_bytes": current,
                    "total_bytes": total,
                    "speed_bps": speed_bps,
                })
                download_status[tid] = state

            return cb

        try:
            run_async(
                lambda: tg_client.download_media(
                    message, file=filepath,
                    progress_callback=make_cb(task_id, info["filename"], total_bytes),
                ),
                allow_reconnect=False,
            )
            final_size = info.get("size") or os.path.getsize(filepath)
            download_status[task_id]["progress"] = 100
            download_status[task_id]["status"] = "done"
            download_status[task_id]["downloaded"] = format_size(final_size)
            download_status[task_id]["total"] = format_size(final_size)
            download_status[task_id]["speed"] = ""
            download_status[task_id]["downloaded_bytes"] = final_size
            download_status[task_id]["total_bytes"] = final_size
            download_status[task_id]["speed_bps"] = 0.0
            download_status[task_id]["queue_position"] = None
            download_status[task_id]["queue_size"] = 0
        except Exception as e:
            err = str(e)
            if download_cancel.get(task_id) or "取消" in err:
                download_status[task_id]["status"] = "cancelled"
                download_status[task_id]["error"] = "已取消"
                if os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
            else:
                download_status[task_id]["status"] = "error"
                download_status[task_id]["error"] = err
            download_status[task_id]["speed"] = ""
            download_status[task_id]["speed_bps"] = 0.0
            download_status[task_id]["queue_position"] = None
            download_status[task_id]["queue_size"] = 0

    # 所有任务完成，减少活跃数并处理队列
    with queue_lock:
        active_downloads -= 1
    process_queue()


@app.route("/api/stream/<path:filepath>")
def api_stream(filepath):
    """流式传输视频文件用于浏览器预览"""
    full_path = os.path.join(DOWNLOAD_DIR, filepath)
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(DOWNLOAD_DIR)):
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


@app.route("/api/files")
def api_files():
    files = []
    if not os.path.exists(DOWNLOAD_DIR):
        return jsonify(files)
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
                })
    return jsonify(files)


@app.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    full_path = os.path.join(DOWNLOAD_DIR, filepath)
    full_path = os.path.realpath(full_path)
    if not full_path.startswith(os.path.realpath(DOWNLOAD_DIR)):
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


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    tg_thread = threading.Thread(target=start_tg_client, daemon=True)
    tg_thread.start()
    time.sleep(3)
    print("Web UI 启动: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)

