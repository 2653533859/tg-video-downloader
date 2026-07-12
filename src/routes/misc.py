"""
文件服务和其他路由 Blueprint
包含文件列表、下载、历史记录等
"""
from flask import Blueprint, jsonify, request, send_from_directory, Response, send_file
import os

from src.files import (
    iter_file_chunks,
    list_download_files,
    local_stream_range,
    resolve_file_path,
)

bp = Blueprint('fileservice', __name__)

# 需要从主 app 注入的依赖
_DOWNLOAD_DIR = None
_format_size = None
_query_task_history = None
_get_download_status = None
_clear_all_tasks = None
_clear_task_ids = None
_get_recovery_candidates = None
_recover_candidates = None
_abort_if_debug_disabled = None
_resolve_download_path = None
_debug_service = None


def init_blueprint(
    download_dir,
    format_size_func,
    query_task_history_func,
    get_download_status_func,
    clear_all_tasks_func,
    get_recovery_candidates_func,
    recover_candidates_func,
    abort_debug_func,
    resolve_download_path_func,
    clear_task_ids_func=None,
    debug_service=None,
):
    """初始化 Blueprint 依赖"""
    global _DOWNLOAD_DIR, _format_size, _query_task_history
    global _get_download_status, _clear_all_tasks, _clear_task_ids, _get_recovery_candidates
    global _recover_candidates, _abort_if_debug_disabled, _resolve_download_path
    global _debug_service

    _DOWNLOAD_DIR = download_dir
    _format_size = format_size_func
    _query_task_history = query_task_history_func
    _get_download_status = get_download_status_func
    _clear_all_tasks = clear_all_tasks_func
    _clear_task_ids = clear_task_ids_func
    _get_recovery_candidates = get_recovery_candidates_func
    _recover_candidates = recover_candidates_func
    _abort_if_debug_disabled = abort_debug_func
    _resolve_download_path = resolve_download_path_func
    _debug_service = debug_service


def _find_matching_download_task(folder, filename):
    tasks = (_get_download_status or (lambda: {}))()
    for task in tasks.values():
        if task.get("filename") != filename:
            continue
        dialog_name = str(task.get("dialog_name") or "")
        if dialog_name and dialog_name != folder:
            continue
        return dict(task)
    return None


def _annotate_download_file_item(item):
    task = _find_matching_download_task(item.get("folder"), item.get("filename"))
    actual_bytes = int(item.get("size_bytes") or 0)
    if not task:
        item["playable"] = actual_bytes > 0
        if actual_bytes <= 0:
            item["play_block_reason"] = "文件大小为 0B，无法播放"
        return item

    status = task.get("status") or ""
    expected_bytes = int(task.get("expected_bytes") or task.get("total_bytes") or 0)
    complete = status == "done"
    if expected_bytes > 0 and actual_bytes < expected_bytes:
        complete = False

    item["task_status"] = status
    item["task_progress"] = task.get("progress")
    item["expected_bytes"] = expected_bytes
    item["playable"] = complete and actual_bytes > 0
    if not item["playable"]:
        if status not in {"done", "skipped", "error", "cancelled"}:
            item["play_block_reason"] = "文件仍在下载中，完成后才能播放"
        elif actual_bytes <= 0:
            item["play_block_reason"] = "文件大小为 0B，无法播放"
        elif expected_bytes > 0 and actual_bytes < expected_bytes:
            item["play_block_reason"] = "文件未下载完整，无法播放"
        else:
            item["play_block_reason"] = task.get("error") or "文件状态异常，无法播放"
    return item


def _download_file_play_block_reason(full_path, size_bytes):
    rel_path = os.path.relpath(full_path, os.path.realpath(_DOWNLOAD_DIR))
    rel_parts = rel_path.split(os.sep, 1)
    if len(rel_parts) != 2:
        return "" if size_bytes > 0 else "文件大小为 0B，无法播放"
    item = _annotate_download_file_item({
        "folder": rel_parts[0],
        "filename": rel_parts[1],
        "size_bytes": size_bytes,
    })
    return item.get("play_block_reason") if not item.get("playable") else ""


@bp.route("/api/files")
def api_files():
    """获取文件列表"""
    page = max(request.args.get("page", default=1, type=int) or 1, 1)
    per_page = min(max(request.args.get("per_page", default=100, type=int) or 100, 10), 500)
    payload = list_download_files(_DOWNLOAD_DIR, _format_size, page, per_page)
    payload["files"] = [_annotate_download_file_item(item) for item in payload.get("files", [])]
    return jsonify(payload)


@bp.route("/api/file/<path:filepath>")
def api_file_download(filepath):
    """下载文件"""
    try:
        full_path = resolve_file_path(_DOWNLOAD_DIR, filepath)
    except ValueError:
        return jsonify({"error": "非法路径"}), 403
    except FileNotFoundError:
        return jsonify({"error": "文件不存在"}), 404

    return send_from_directory(
        os.path.dirname(full_path),
        os.path.basename(full_path),
        as_attachment=True
    )


@bp.route("/api/stream/<path:filepath>")
def api_stream(filepath):
    """流式传输视频"""
    try:
        full_path = resolve_file_path(_DOWNLOAD_DIR, filepath)

        if not os.path.isfile(full_path):
            return jsonify({"error": "文件不存在"}), 404

        file_size = os.path.getsize(full_path)
        block_reason = _download_file_play_block_reason(full_path, file_size)
        if block_reason:
            return jsonify({"error": block_reason}), 409
        stream_range = local_stream_range(file_size, request.headers.get("Range"))

        if stream_range:
            return Response(
                iter_file_chunks(full_path, stream_range["start"], stream_range["content_length"]),
                206,
                mimetype='video/mp4',
                direct_passthrough=True,
                headers={
                    "Content-Range": stream_range["content_range"],
                    "Accept-Ranges": "bytes",
                    "Content-Length": str(stream_range["content_length"]),
                },
            )
        else:
            return send_file(full_path, mimetype='video/mp4')

    except FileNotFoundError:
        return jsonify({"error": "文件不存在"}), 404
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/api/history")
def api_history():
    """查询历史记录"""
    status = request.args.get("status", "")
    query = request.args.get("q", "")
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 30, type=int)

    result = _query_task_history(status, query, page, per_page)
    return jsonify(result)


@bp.route("/api/clear_tasks", methods=["POST"])
def api_clear_tasks():
    """清理任务"""
    data = request.json or {}
    task_ids = data.get("task_ids")
    if task_ids is not None and _clear_task_ids is not None:
        return jsonify(_clear_task_ids(task_ids))

    scope = data.get("scope", "terminal")

    count = _clear_all_tasks(scope)
    return jsonify({"ok": True, "cleared": count, "skipped": 0})


@bp.route("/api/recovery_candidates")
def api_recovery_candidates():
    """获取可恢复的候选任务"""
    candidates = _get_recovery_candidates()
    return jsonify({"candidates": candidates})


@bp.route("/api/recover_candidates", methods=["POST"])
def api_recover_candidates():
    """恢复候选任务"""
    data = request.json or {}
    task_ids = data.get("task_ids", [])

    if not task_ids:
        return jsonify({"error": "未指定任务"}), 400

    return jsonify(_recover_candidates(task_ids))


@bp.route("/api/debug")
def api_debug():
    """调试信息"""
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error

    payload, status = _debug_service.inspect_messages(
        request.args.get("dialog_index", type=int),
        limit=20,
    )
    return jsonify(payload), status


@bp.route("/api/debug_replies")
def api_debug_replies():
    """调试回复"""
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error

    payload, status = _debug_service.inspect_messages(
        request.args.get("dialog_index", type=int),
        limit=20,
        reply_to=request.args.get("post_id", type=int),
    )
    return jsonify(payload), status


@bp.route("/api/debug_full")
def api_debug_full():
    """完整调试信息"""
    debug_error = _abort_if_debug_disabled()
    if debug_error is not None:
        return debug_error

    payload, status = _debug_service.inspect_full_messages(
        request.args.get("dialog_index", type=int),
    )
    return jsonify(payload), status
