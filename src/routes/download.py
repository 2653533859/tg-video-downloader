"""
下载管理路由 Blueprint
包含下载相关的 API 端点
"""
from flask import Blueprint, jsonify, request, Response
import json
import time

bp = Blueprint('download', __name__)

# 需要从主 app 注入的依赖
_current_entity_cache = None
_make_task_id = None
_copy_task_state = None
_set_task_state = None
_update_task_state = None
_get_cached_message = None
_resolve_message = None
_mark_download_cancelled = None
_clear_download_cancelled = None
_supports_tdl_download = None
_enqueue_download = None
_remove_from_queue = None
_get_tdl_process = None
_get_download_status = None
_format_size = None
_get_video_info = None
_TERMINAL_STATES = None
_last_download_dialog = None
_resume_all_incomplete_tasks = None
_resume_task = None
_move_queued_task = None
_drop_task_state = None
_clear_tdl_error = None
_clear_resume_info = None
_get_queue_status = None
_status_lock = None
_download_status = None


def init_blueprint(
    current_entity_cache,
    make_task_id_func,
    copy_task_state_func,
    set_task_state_func,
    update_task_state_func,
    get_cached_message_func,
    resolve_message_func,
    mark_cancelled_func,
    clear_cancelled_func,
    supports_tdl_func,
    enqueue_download_func,
    remove_from_queue_func,
    get_tdl_process_func,
    get_download_status_func,
    format_size_func,
    get_video_info_func,
    terminal_states,
    last_download_dialog_ref,
    resume_all_func=None,
    resume_task_func=None,
    move_queued_task_func=None,
    drop_task_state_func=None,
    clear_tdl_error_func=None,
    clear_resume_info_func=None,
    get_queue_status_func=None,
    status_lock=None,
    download_status_ref=None,
):
    """初始化 Blueprint 依赖"""
    global _current_entity_cache, _make_task_id, _copy_task_state
    global _set_task_state, _update_task_state, _get_cached_message
    global _resolve_message, _mark_download_cancelled, _clear_download_cancelled
    global _supports_tdl_download, _enqueue_download, _remove_from_queue
    global _get_tdl_process, _get_download_status, _format_size
    global _get_video_info, _TERMINAL_STATES, _last_download_dialog
    global _resume_all_incomplete_tasks, _resume_task, _move_queued_task
    global _drop_task_state, _clear_tdl_error, _clear_resume_info
    global _get_queue_status, _status_lock, _download_status

    _current_entity_cache = current_entity_cache
    _make_task_id = make_task_id_func
    _copy_task_state = copy_task_state_func
    _set_task_state = set_task_state_func
    _update_task_state = update_task_state_func
    _get_cached_message = get_cached_message_func
    _resolve_message = resolve_message_func
    _mark_download_cancelled = mark_cancelled_func
    _clear_download_cancelled = clear_cancelled_func
    _supports_tdl_download = supports_tdl_func
    _enqueue_download = enqueue_download_func
    _remove_from_queue = remove_from_queue_func
    _get_tdl_process = get_tdl_process_func
    _get_download_status = get_download_status_func
    _format_size = format_size_func
    _get_video_info = get_video_info_func
    _TERMINAL_STATES = terminal_states
    _last_download_dialog = last_download_dialog_ref
    _resume_all_incomplete_tasks = resume_all_func
    _resume_task = resume_task_func
    _move_queued_task = move_queued_task_func
    _drop_task_state = drop_task_state_func
    _clear_tdl_error = clear_tdl_error_func
    _clear_resume_info = clear_resume_info_func
    _get_queue_status = get_queue_status_func
    _status_lock = status_lock
    _download_status = download_status_ref


@bp.route("/api/download", methods=["POST"])
def api_download():
    """开始下载"""
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
        if existing_state and existing_state.get("status") not in _TERMINAL_STATES:
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
            info = _get_video_info(msg)
            if info:
                fname = info["filename"]

        total_bytes = info.get("size") if info else 0

        _set_task_state(task_id, {
            "filename": fname,
            "progress": 0,
            "status": "submitting",
            "downloaded": "0B" if total_bytes else "",
            "total": _format_size(total_bytes) if total_bytes else "",
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

    # 更新最后下载对话
    global _last_download_dialog
    _last_download_dialog = dialog_name

    for task_id, mid, info in tasks:
        try:
            task_ids[task_id] = _enqueue_download(task_id, entity_id, mid, dialog_name, info)
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


@bp.route("/api/cancel", methods=["POST"])
def api_cancel():
    """取消下载"""
    data = request.json or {}
    task_id = data.get("task_id")
    entity_id = data.get("entity_id")
    msg_id = data.get("msg_id")

    if not task_id and msg_id is not None and entity_id is not None:
        task_id = _make_task_id(entity_id, msg_id)

    if task_id:
        _mark_download_cancelled(task_id)
        _remove_from_queue(task_id)

        state = _copy_task_state(task_id) or {}
        proc = _get_tdl_process(task_id)

        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except:
                pass

        _update_task_state(
            task_id,
            status="cancelled",
            error="",
            finish_time=time.time(),
            speed="",
            speed_bps=0.0,
            queue_position=None,
            queue_size=0,
        )

        return jsonify({"status": "cancelled", "task_id": task_id})

    return jsonify({"error": "任务不存在"}), 404


@bp.route("/api/retry", methods=["POST"])
def api_retry():
    """重试下载"""
    data = request.json or {}
    task_id = data.get("task_id")
    entity_id = data.get("entity_id")
    msg_id = data.get("msg_id")

    if not task_id and msg_id is not None and entity_id is not None:
        task_id = _make_task_id(entity_id, msg_id)

    if not task_id:
        return jsonify({"error": "参数不完整"}), 400

    state = _copy_task_state(task_id)
    if not state:
        return jsonify({"error": "任务不存在"}), 404

    # 重置状态并重新加入队列
    _update_task_state(
        task_id,
        status="submitting",
        progress=0,
        error="",
        speed="",
        speed_bps=0.0,
        queue_position=None,
        queue_size=0,
    )

    _clear_download_cancelled(task_id)

    try:
        # 重新加入下载队列
        entity_id = state.get("entity_id")
        msg_id = state.get("msg_id")
        dialog_name = state.get("dialog_name", "unknown")

        _enqueue_download(task_id, entity_id, msg_id, dialog_name, None)

        return jsonify({"status": "retrying", "task_id": task_id})
    except Exception as exc:
        _update_task_state(
            task_id,
            status="error",
            error=str(exc),
            finish_time=time.time(),
        )
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/retry_all", methods=["POST"])
def api_retry_all():
    """重试所有失败的下载"""
    result = _resume_all_incomplete_tasks(auto=False)
    return jsonify({"ok": True, **result})


@bp.route("/api/queue_action", methods=["POST"])
def api_queue_action():
    """队列操作"""
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
        if not _move_queued_task(task_id, action):
            return jsonify({"error": "任务不在等待队列中"}), 409
        return jsonify({"ok": True})

    if state.get("status") == "downloading":
        return jsonify({"error": "下载中的任务请使用取消，已下载部分会保留"}), 409

    _remove_from_queue(task_id)
    if action == "pause":
        _update_task_state(
            task_id,
            status="paused",
            error="已暂停",
            speed="",
            speed_bps=0.0,
            queue_position=None,
            queue_size=0,
        )
        return jsonify({"ok": True})

    _drop_task_state(task_id)
    _clear_download_cancelled(task_id)
    _clear_tdl_error(task_id)
    _clear_resume_info(task_id)
    return jsonify({"ok": True})


@bp.route("/api/download_status")
def api_download_status():
    """获取下载状态"""
    return jsonify(_get_download_status())


@bp.route("/api/progress")
def api_progress():
    """获取进度（SSE流）"""
    def snapshot():
        try:
            with _status_lock:
                tasks = {key: dict(value) for key, value in list(_download_status.items())}
        except Exception:
            tasks = {}

        complete = bool(tasks) and all(
            state.get("status") in _TERMINAL_STATES for state in tasks.values()
        )
        return {
            "tasks": tasks,
            "queue": _get_queue_status(),
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
