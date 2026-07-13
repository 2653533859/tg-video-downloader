"""
Relay 路由 Blueprint
处理媒体文件的代理传输
"""
from flask import Blueprint, jsonify, request, Response
import time
import threading
from urllib.parse import quote

bp = Blueprint('relay', __name__)

# 需要从主 app 注入的依赖
_RELAY_TOKEN_SECRET = None
_MAX_CONCURRENT_RELAYS = 5
_verify_relay_token = None
_get_relay_media = None
_parse_range = None
_iter_relay_bytes = None
_log_warning = None
_log_info = None
_log_error = None

# 并发控制
relay_lock = threading.RLock()
active_relays = 0


def init_blueprint(deps):
    """初始化 Blueprint 依赖（单一 deps 映射注入）。

    keys: relay_token_secret, max_concurrent_relays, verify_relay_token_func,
          get_relay_media_func, parse_range_func, iter_relay_bytes_func,
          log_warning_func, log_info_func, log_error_func
    """
    global _RELAY_TOKEN_SECRET, _MAX_CONCURRENT_RELAYS
    global _verify_relay_token, _get_relay_media, _parse_range
    global _iter_relay_bytes, _log_warning, _log_info, _log_error

    _RELAY_TOKEN_SECRET = deps["relay_token_secret"]
    _MAX_CONCURRENT_RELAYS = deps["max_concurrent_relays"]
    _verify_relay_token = deps["verify_relay_token_func"]
    _get_relay_media = deps["get_relay_media_func"]
    _parse_range = deps["parse_range_func"]
    _iter_relay_bytes = deps["iter_relay_bytes_func"]
    _log_warning = deps["log_warning_func"]
    _log_info = deps["log_info_func"]
    _log_error = deps["log_error_func"]


@bp.route("/relay/<signed_int:entity_id>/<int:msg_id>")
def relay_media(entity_id, msg_id):
    """
    Relay 媒体文件
    支持 Range 请求，用于流式传输
    """
    global active_relays

    if not _RELAY_TOKEN_SECRET:
        return jsonify({"error": "relay token secret is not configured"}), 503

    # 并发限制检查
    with relay_lock:
        if active_relays >= _MAX_CONCURRENT_RELAYS:
            _log_warning(
                f"[relay:{entity_id}:{msg_id}] 并发数已达上限 {_MAX_CONCURRENT_RELAYS}，"
                f"请降低并发请求数"
            )
            return jsonify({"error": "relay concurrency limit reached"}), 503
        active_relays += 1

    # streaming=True 时把槽位所有权移交给生成器的 finally 释放；其余任何提前
    # 返回/异常路径由本函数 finally 释放，避免 400/403 早退导致的槽位泄漏。
    streaming = False
    try:
        file_name = request.args.get("file_name", "")
        token = request.args.get("token", "")

        if not file_name or not token:
            return jsonify({"error": "missing relay parameters"}), 400

        # 验证 token
        _verify_relay_token(
            secret=_RELAY_TOKEN_SECRET,
            token=token,
            entity_id=entity_id,
            message_id=msg_id,
            file_name=file_name,
            now_ts=int(time.time()),
        )

        # 获取媒体信息
        media = _get_relay_media(entity_id, msg_id)

        if media.get("file_name") != file_name:
            return jsonify({"error": "file name mismatch"}), 403

        total_size = int(media.get("size") or 0)

        # 解析 Range 请求
        start_offset, end_offset, status_code = _parse_range(
            request.headers.get("Range"),
            total_size
        )

        content_length = end_offset - start_offset + 1

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name)}",
        }

        if status_code == 206:
            headers["Content-Range"] = f"bytes {start_offset}-{end_offset}/{total_size}"

        def _generate_with_cleanup():
            """生成器，确保完成后释放槽位"""
            try:
                for chunk in _iter_relay_bytes(media, start_offset, end_offset):
                    yield chunk
            finally:
                global active_relays
                with relay_lock:
                    active_relays = max(0, active_relays - 1)
                _log_info(
                    f"[relay:{entity_id}:{msg_id}] 传输结束，"
                    f"释放槽位 (当前活跃: {active_relays})"
                )

        response = Response(
            _generate_with_cleanup(),
            status=status_code,
            mimetype=media.get("mime_type") or "application/octet-stream",
            headers=headers,
        )
        streaming = True
        return response

    except Exception as exc:
        _log_error(f"[relay:{entity_id}:{msg_id}] relay route failed: {exc}")

        if "token" in str(exc).lower():
            return jsonify({"error": str(exc)}), 403

        return jsonify({"error": str(exc)}), 502

    finally:
        if not streaming:
            with relay_lock:
                active_relays = max(0, active_relays - 1)
