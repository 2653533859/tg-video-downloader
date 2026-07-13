"""
系统状态路由 Blueprint
包含系统状态和健康检查 API
"""
from flask import Blueprint, jsonify

from src.system import SystemStatusService

bp = Blueprint('system', __name__)

_status_service = None


def init_blueprint(deps):
    """初始化 Blueprint 依赖（单一 deps 映射注入）。

    keys: ensure_tg_conn_func, get_tg_connected_func, get_tg_error_func,
          get_tg_user_func, get_queue_func, get_tdl_func, proxy_config, tdl_binary
    """
    global _status_service
    _status_service = SystemStatusService(
        ensure_tg_connection=deps["ensure_tg_conn_func"],
        get_tg_connected=deps["get_tg_connected_func"],
        get_tg_error=deps["get_tg_error_func"],
        get_tg_user=deps["get_tg_user_func"],
        get_queue_status=deps["get_queue_func"],
        get_tdl_status=deps["get_tdl_func"],
        proxy_config=deps["proxy_config"],
        tdl_binary=deps["tdl_binary"],
    )


@bp.route("/api/status")
def api_status():
    """系统状态"""
    return jsonify(_status_service.status_payload())


@bp.route("/api/health")
def api_health():
    """健康检查（完整信息，含 degraded 列表）"""
    return jsonify(_status_service.health_payload())


@bp.route("/api/health/live")
def api_health_live():
    """存活探针（liveness）：进程存活即 200，不触碰外部依赖"""
    return jsonify(_status_service.liveness_payload())


@bp.route("/api/health/ready")
def api_health_ready():
    """就绪探针（readiness）：主 Telegram 未就绪返回 503"""
    payload, status = _status_service.readiness_payload()
    return jsonify(payload), status
