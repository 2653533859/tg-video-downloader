"""
Telegram API 路由 Blueprint
包含 Telegram 相关的 API 端点
"""
from flask import Blueprint, jsonify, request, send_file, Response
import os
import re
import threading
from src.files.thumbnails import thumbnail_cache_path, write_thumbnail
from src.telegram.video_service import TelegramVideoService

bp = Blueprint('telegram', __name__)

# 需要从主 app 注入的依赖
_tg_client = None
_run_async = None
_kickoff_dialogs_refresh = None
_dialogs_cache_snapshot = None
_resolve_requested_entity = None
_video_info_for_message = None
_make_excerpt = None
_message_text = None
_get_cached_message = None
_resolve_message = None
_abort_if_debug_disabled = None
_THUMB_DIR = None
_RELAY_TOKEN_SECRET = None
_video_service = None
_get_video_info = None
_build_relay_url = None

# 缓存
cache_lock = threading.RLock()
_current_entity_cache = {}
_videos_cache = {}
_replies_cache = {}
_dialogs_cache = []

MAX_VIDEO_CACHE_SIZE = 100
MAX_REPLY_CACHE_SIZE = 50


def init_blueprint(deps):
    """初始化 Blueprint 依赖（单一 deps 映射注入）。

    keys: tg_client, run_async_func, kickoff_dialogs_func, dialogs_snapshot_func,
          resolve_entity_func, video_info_func, make_excerpt_func, message_text_func,
          get_cached_message_func, resolve_message_func, abort_debug_func, thumb_dir,
          relay_token_secret, dialogs_cache_ref, current_entity_cache_ref,
          videos_cache_ref, replies_cache_ref, video_service(可选),
          get_video_info_func(可选), build_relay_url_func(可选)
    """
    global _tg_client, _run_async, _kickoff_dialogs_refresh, _dialogs_cache_snapshot
    global _resolve_requested_entity, _video_info_for_message, _make_excerpt
    global _message_text, _get_cached_message, _resolve_message
    global _abort_if_debug_disabled, _THUMB_DIR, _RELAY_TOKEN_SECRET
    global _dialogs_cache, _current_entity_cache, _videos_cache, _replies_cache
    global _video_service
    global _get_video_info, _build_relay_url

    _tg_client = deps["tg_client"]
    _run_async = deps["run_async_func"]
    _kickoff_dialogs_refresh = deps["kickoff_dialogs_func"]
    _dialogs_cache_snapshot = deps["dialogs_snapshot_func"]
    _resolve_requested_entity = deps["resolve_entity_func"]
    _video_info_for_message = deps["video_info_func"]
    _make_excerpt = deps["make_excerpt_func"]
    _message_text = deps["message_text_func"]
    _get_cached_message = deps["get_cached_message_func"]
    _resolve_message = deps["resolve_message_func"]
    _abort_if_debug_disabled = deps["abort_debug_func"]
    _THUMB_DIR = deps["thumb_dir"]
    _RELAY_TOKEN_SECRET = deps["relay_token_secret"]
    _get_video_info = deps.get("get_video_info_func")
    _build_relay_url = deps.get("build_relay_url_func")

    # 使用引用，避免复制
    _dialogs_cache = deps["dialogs_cache_ref"]
    _current_entity_cache = deps["current_entity_cache_ref"]
    _videos_cache = deps["videos_cache_ref"]
    _replies_cache = deps["replies_cache_ref"]
    _video_service = deps.get("video_service") or TelegramVideoService(
        client=_tg_client,
        run_async=_run_async,
        resolve_requested_entity=_resolve_requested_entity,
        video_info_for_message=_video_info_for_message,
        message_text=_message_text,
        make_excerpt=_make_excerpt,
        cache_lock=cache_lock,
        current_entity_cache=_current_entity_cache,
        videos_cache=_videos_cache,
        replies_cache=_replies_cache,
        max_video_cache_size=MAX_VIDEO_CACHE_SIZE,
        max_reply_cache_size=MAX_REPLY_CACHE_SIZE,
    )


@bp.route("/api/dialogs")
def api_dialogs():
    """获取对话列表"""
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


@bp.route("/api/search")
def api_search():
    """搜索 Telegram 实体"""
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
        entity = _run_async(lambda: _tg_client.get_entity(query))
        name = getattr(entity, "title", None) or getattr(entity, "first_name", str(query))

        with cache_lock:
            _current_entity_cache["search_entity"] = entity
            _current_entity_cache["search_name"] = name

        return jsonify({
            "name": name,
            "id": getattr(entity, "id", 0),
            "source": "search"
        })
    except Exception as e:
        return jsonify({"error": f"解析失败: {str(e)}"}), 500


@bp.route("/api/videos")
def api_videos():
    """获取视频列表"""
    dialog_index = request.args.get("dialog_index", type=int)
    entity_id = request.args.get("entity_id", type=int)
    source = request.args.get("source", "dialog")
    limit = request.args.get("limit", 100, type=int)
    include_replies = request.args.get("include_replies", "false") == "true"
    reply_post_limit = min(max(request.args.get("reply_post_limit", 50, type=int), 0), 500)
    refresh = request.args.get("refresh", "false") == "true"

    try:
        payload, status = _video_service.list_videos(
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


@bp.route("/api/video_search")
def api_video_search():
    """搜索视频"""
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
        payload, status = _video_service.search_videos(
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


@bp.route("/api/replies")
def api_replies():
    """获取评论/回复"""
    entity_id = request.args.get("entity_id", type=int)
    post_id = request.args.get("post_id", type=int)
    limit = min(max(request.args.get("limit", 100, type=int), 10), 300)
    refresh = request.args.get("refresh", "false") == "true"

    if not entity_id or not post_id:
        return jsonify({"error": "缺少参数"}), 400

    try:
        payload, status = _video_service.list_replies(
            entity_id=entity_id,
            post_id=post_id,
            limit=limit,
            refresh=refresh,
        )
        return jsonify(payload), status
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/thumb/<int:msg_id>")
def api_thumb(msg_id):
    """获取缩略图"""
    entity_id = request.args.get("entity", type=int)
    thumb_path = thumbnail_cache_path(_THUMB_DIR, entity_id, msg_id)

    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype="image/jpeg")

    message = _get_cached_message(msg_id, entity_id)
    if not message:
        return Response(status=404)

    try:
        data = _run_async(lambda: _tg_client.download_media(message, file=bytes, thumb=-1), allow_reconnect=False)
        if not data:
            return Response(status=404)

        write_thumbnail(_THUMB_DIR, entity_id, msg_id, data)

        return Response(data, mimetype="image/jpeg")
    except Exception:
        return Response(status=404)


@bp.route("/api/online-play-url")
def api_online_play_url():
    """获取在线播放 URL"""
    entity_id = request.args.get("entity_id", type=int)
    msg_id = request.args.get("msg_id", type=int)
    file_name = request.args.get("filename", "").strip()

    if entity_id is None or msg_id is None:
        return jsonify({"error": "缺少消息标识"}), 400

    if not _RELAY_TOKEN_SECRET:
        return jsonify({"error": "Relay 未配置，无法在线播放"}), 503

    try:
        if not file_name:
            message = _resolve_message(entity_id, msg_id, force_refresh=True)
            info = _get_video_info(message) if message and _get_video_info else None
            if not info:
                return jsonify({"error": "消息不包含可播放视频"}), 404
            file_name = info["filename"]

        return jsonify({
            "ok": True,
            "url": _build_relay_url(entity_id, msg_id, file_name),
            "filename": file_name,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
