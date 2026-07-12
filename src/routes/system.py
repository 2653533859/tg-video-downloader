"""
系统状态路由 Blueprint
包含系统状态和健康检查 API
"""
from flask import Blueprint, jsonify

from src.system import SystemStatusService

bp = Blueprint('system', __name__)

_status_service = None


def init_blueprint(
    ensure_tg_conn_func,
    get_tg_connected_func,
    get_tg_error_func,
    get_tg_user_func,
    get_queue_func,
    get_tdl_func,
    proxy_config,
    tdl_binary
):
    """
    初始化 Blueprint 依赖

    Args:
        ensure_tg_conn_func: 确保 Telegram 连接的函数
        get_tg_connected_func: 获取连接状态的函数
        get_tg_error_func: 获取连接错误的函数
        get_tg_user_func: 获取用户信息的函数
        get_queue_func: 获取队列状态的函数
        get_tdl_func: 获取 TDL 状态的函数
        proxy_config: 代理配置
        tdl_binary: TDL 二进制路径
    """
    global _status_service
    _status_service = SystemStatusService(
        ensure_tg_connection=ensure_tg_conn_func,
        get_tg_connected=get_tg_connected_func,
        get_tg_error=get_tg_error_func,
        get_tg_user=get_tg_user_func,
        get_queue_status=get_queue_func,
        get_tdl_status=get_tdl_func,
        proxy_config=proxy_config,
        tdl_binary=tdl_binary,
    )


@bp.route("/api/status")
def api_status():
    """系统状态"""
    return jsonify(_status_service.status_payload())


@bp.route("/api/health")
def api_health():
    """健康检查"""
    return jsonify(_status_service.health_payload())
