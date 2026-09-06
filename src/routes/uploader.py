"""Google Drive / rclone upload route Blueprint."""

import os
from flask import Blueprint, jsonify, request

bp = Blueprint("uploader", __name__)

_upload_manager = None
_resolve_download_path = None


def init_blueprint(deps: dict):
    """
    Initialize Blueprint dependencies.
    Expected keys in deps:
      - upload_manager: UploadManager instance
      - resolve_download_path: Callable[[str, ...], str]
    """
    global _upload_manager, _resolve_download_path
    _upload_manager = deps["upload_manager"]
    _resolve_download_path = deps["resolve_download_path"]


@bp.route("/api/gdrive/status", methods=["GET"])
def api_gdrive_status():
    """Get real-time upload queue and active task status."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500
    return jsonify(_upload_manager.get_status_payload())


@bp.route("/api/gdrive/upload", methods=["POST"])
def api_gdrive_upload():
    """
    Manually enqueue a downloaded file to upload to Google Drive.
    JSON payload: {"folder": "channel_name", "filename": "video.mp4", "delete_local": false}
    """
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500

    if not _upload_manager.auth.get_info().get("configured"):
        return jsonify({"error": "尚未绑定 Google 云盘，请先在上方点击【云盘登录】完成授权绑定"}), 400

    data = request.json or {}
    folder = data.get("folder", "").strip()
    filename = data.get("filename", "").strip()
    delete_local = data.get("delete_local", None)

    if not folder or not filename:
        return jsonify({"error": "缺少 folder 或 filename 参数"}), 400

    try:
        filepath = _resolve_download_path(folder, filename, must_exist=True)
    except (ValueError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 404
    except Exception as exc:
        return jsonify({"error": f"定位文件失败: {exc}"}), 500

    try:
        task = _upload_manager.enqueue(
            filepath=filepath,
            folder=folder,
            filename=filename,
            delete_local=delete_local,
        )
        return jsonify({"ok": True, "task": task})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@bp.route("/api/gdrive/cancel", methods=["POST"])
def api_gdrive_cancel():
    """Cancel an active or queued upload task."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500

    data = request.json or {}
    upload_id = data.get("upload_id", "").strip()
    if not upload_id:
        return jsonify({"error": "缺少 upload_id 参数"}), 400

    success = _upload_manager.cancel(upload_id)
    return jsonify({"ok": success})


@bp.route("/api/gdrive/retry", methods=["POST"])
def api_gdrive_retry():
    """Retry a failed or cancelled upload task."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500

    data = request.json or {}
    upload_id = data.get("upload_id", "").strip()
    if not upload_id:
        return jsonify({"error": "缺少 upload_id 参数"}), 400

    task = _upload_manager.retry(upload_id)
    if not task:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"ok": True, "task": task})


@bp.route("/api/gdrive/test", methods=["GET"])
def api_gdrive_test():
    """Test remote connection and rclone configuration."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500

    try:
        result = _upload_manager.uploader.test_connection()
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@bp.route("/api/gdrive/auth/info", methods=["GET"])
def api_gdrive_auth_info():
    """Get rclone configuration info, remote status, and login helper."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500
    return jsonify(_upload_manager.auth.get_info())


@bp.route("/api/gdrive/auth/save_token", methods=["POST"])
def api_gdrive_auth_save_token():
    """Save Google Drive OAuth token JSON to rclone.conf."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500
    data = request.json or {}
    token = data.get("token", "").strip()
    scope = data.get("scope", "drive").strip()
    team_drive = data.get("team_drive", "").strip()
    if not token:
        return jsonify({"ok": False, "error": "Token 不能为空"}), 400

    res = _upload_manager.auth.save_token(token, scope=scope, team_drive_id=team_drive)
    if res.get("ok"):
        _upload_manager.config.enabled = True
    return jsonify(res)


@bp.route("/api/gdrive/auth/save_config", methods=["POST"])
def api_gdrive_auth_save_config():
    """Save raw rclone.conf content."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500
    data = request.json or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"ok": False, "error": "配置内容不能为空"}), 400
    res = _upload_manager.auth.save_raw_config(content)
    if res.get("ok"):
        _upload_manager.config.enabled = True
    return jsonify(res)


@bp.route("/api/gdrive/auth/unbind", methods=["POST"])
def api_gdrive_auth_unbind():
    """Remove remote configuration."""
    if _upload_manager is None:
        return jsonify({"error": "UploadManager 未初始化"}), 500
    return jsonify(_upload_manager.auth.unbind())
