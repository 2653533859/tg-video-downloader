#!/usr/bin/env python3
"""Telegram 视频下载器 - Web UI"""

import os
import sys
import asyncio
import threading
import time
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request, Response, send_from_directory
from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)
from config import API_ID, API_HASH, DOWNLOAD_DIR, SESSION_NAME

app = Flask(__name__)

tg_loop = asyncio.new_event_loop()
tg_client = TelegramClient(SESSION_NAME, API_ID, API_HASH, loop=tg_loop)

download_status = {}


def run_async(coro):
    future = asyncio.run_coroutine_threadsafe(coro, tg_loop)
    return future.result(timeout=600)


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


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/dialogs")
def api_dialogs():
    try:
        dialogs = run_async(tg_client.get_dialogs())
        _dialogs_cache.clear()
        _dialogs_cache.extend(dialogs)
        result = []
        for i, d in enumerate(dialogs[:50]):
            dtype = "频道" if d.is_channel else "群组" if d.is_group else "私聊"
            result.append({"index": i, "name": d.name, "id": d.id, "type": dtype,
                           "is_channel": d.is_channel})
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "请输入搜索内容"}), 400
    try:
        entity = run_async(tg_client.get_entity(query))
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

    try:
        if source == "search" and "search_entity" in _current_entity_cache:
            entity = _current_entity_cache["search_entity"]
            name = _current_entity_cache.get("search_name", "unknown")
        elif source == "search" and entity_id:
            entity = run_async(tg_client.get_entity(entity_id))
            name = getattr(entity, "title", None) or "unknown"
        elif dialog_index is not None and dialog_index < len(_dialogs_cache):
            entity = _dialogs_cache[dialog_index].entity
            name = _dialogs_cache[dialog_index].name
        else:
            return jsonify({"error": "无效的对话"}), 400

        _current_entity_cache["entity"] = entity
        _current_entity_cache["name"] = name

        async def scan():
            videos = []
            async for message in tg_client.iter_messages(entity, limit=limit):
                info = get_video_info(message)
                if info:
                    _messages_cache[message.id] = message
                    info["size_fmt"] = format_size(info["size"])
                    info["duration_fmt"] = format_duration(info["duration"])
                    info["source"] = "主消息"
                    videos.append(info)

                if include_replies and message.replies and message.replies.replies > 0:
                    try:
                        async for reply in tg_client.iter_messages(entity, reply_to=message.id, limit=200):
                            rinfo = get_video_info(reply)
                            if rinfo:
                                _messages_cache[reply.id] = reply
                                rinfo["size_fmt"] = format_size(rinfo["size"])
                                rinfo["duration_fmt"] = format_duration(rinfo["duration"])
                                rinfo["source"] = f"评论@帖子{message.id}"
                                videos.append(rinfo)
                    except Exception:
                        pass
            return videos

        return jsonify(run_async(scan()))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def api_download():
    data = request.json
    message_ids = data.get("message_ids", [])
    dialog_name = data.get("dialog_name", "unknown")
    if not message_ids:
        return jsonify({"error": "参数不完整"}), 400

    for mid in message_ids:
        msg = _messages_cache.get(mid)
        fname = "unknown"
        if msg:
            info = get_video_info(msg)
            if info:
                fname = info["filename"]
        download_status[mid] = {
            "filename": fname, "progress": 0, "status": "waiting",
            "downloaded": "", "total": "", "error": "",
        }

    thread = threading.Thread(target=_do_download, args=(message_ids, dialog_name), daemon=True)
    thread.start()
    return jsonify({"status": "started", "count": len(message_ids)})


@app.route("/api/progress")
def api_progress():
    def generate():
        while True:
            data = json.dumps(download_status)
            yield f"data: {data}\n\n"
            if download_status and all(
                s["status"] in ("done", "skipped", "error")
                for s in download_status.values()
            ):
                yield f"data: {json.dumps({'_complete': True, **download_status})}\n\n"
                break
            time.sleep(1)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _do_download(message_ids, dialog_name):
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in dialog_name)
    save_dir = os.path.join(DOWNLOAD_DIR, safe_name)
    os.makedirs(save_dir, exist_ok=True)

    for msg_id in message_ids:
        message = _messages_cache.get(msg_id)
        if not message:
            download_status[msg_id] = {
                "filename": "unknown", "progress": 0,
                "status": "error", "downloaded": "", "total": "",
                "error": "消息未找到，请重新扫描",
            }
            continue

        info = get_video_info(message)
        if not info:
            continue

        filepath = os.path.join(save_dir, info["filename"])

        if os.path.exists(filepath) and os.path.getsize(filepath) == info["size"]:
            download_status[msg_id] = {
                "filename": info["filename"], "progress": 100,
                "status": "skipped", "downloaded": "", "total": "", "error": "",
            }
            continue

        download_status[msg_id] = {
            "filename": info["filename"], "progress": 0,
            "status": "downloading", "downloaded": "0B",
            "total": format_size(info["size"]), "error": "",
        }

        def make_cb(mid, fname, total):
            def cb(current, _total):
                pct = int(current / total * 100) if total else 0
                download_status[mid] = {
                    "filename": fname, "progress": pct,
                    "status": "downloading",
                    "downloaded": format_size(current),
                    "total": format_size(total), "error": "",
                }
            return cb

        try:
            run_async(tg_client.download_media(
                message, file=filepath,
                progress_callback=make_cb(msg_id, info["filename"], info["size"]),
            ))
            download_status[msg_id]["progress"] = 100
            download_status[msg_id]["status"] = "done"
        except Exception as e:
            download_status[msg_id]["status"] = "error"
            download_status[msg_id]["error"] = str(e)


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
    """下载已保存的文件到浏览器"""
    # filepath 格式: folder/filename
    full_path = os.path.join(DOWNLOAD_DIR, filepath)
    full_path = os.path.realpath(full_path)
    # 安全检查：确保路径在 DOWNLOAD_DIR 内
    if not full_path.startswith(os.path.realpath(DOWNLOAD_DIR)):
        return jsonify({"error": "非法路径"}), 403
    directory = os.path.dirname(full_path)
    filename = os.path.basename(full_path)
    if not os.path.isfile(full_path):
        return jsonify({"error": "文件不存在"}), 404
    return send_from_directory(directory, filename, as_attachment=True)


def start_tg_client():
    asyncio.set_event_loop(tg_loop)
    tg_loop.run_until_complete(tg_client.connect())
    if not tg_loop.run_until_complete(tg_client.is_user_authorized()):
        print("错误: Telegram 未登录！请先运行 downloader.py 完成登录。")
        sys.exit(1)
    me = tg_loop.run_until_complete(tg_client.get_me())
    print(f"Telegram 已连接: {me.first_name} (@{me.username})")
    tg_loop.run_forever()


if __name__ == "__main__":
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    tg_thread = threading.Thread(target=start_tg_client, daemon=True)
    tg_thread.start()
    time.sleep(3)
    print("Web UI 启动: http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)

