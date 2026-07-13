"""网页会话登录路由 Blueprint（混合鉴权）。

在保留 HTTP Basic Auth（healthcheck / API 客户端零改动）的前提下，提供
基于 cookie 会话的网页登录页与登出能力。校验一律复用
src/security/access.py 的纯函数，不重造凭据比对逻辑。
"""
from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)

from src.security.access import request_ip_is_local, verify_basic_auth

bp = Blueprint("auth", __name__)


# 依赖注入槽位
_expected_username = ""
_expected_password = ""
_trust_forwarded = False


def init_blueprint(deps):
    """初始化 Blueprint 依赖（单一 deps 映射注入）。

    keys: auth_username, auth_password, trust_forwarded
    """
    global _expected_username, _expected_password, _trust_forwarded
    _expected_username = deps["auth_username"]
    _expected_password = deps["auth_password"]
    _trust_forwarded = deps.get("trust_forwarded", False)


def _request_is_local():
    return request_ip_is_local(
        remote_addr=request.remote_addr or "",
        forwarded_for=request.headers.get("X-Forwarded-For", ""),
        trust_forwarded=_trust_forwarded,
    )


def _auth_required():
    """是否需要网页登录：未配置 Basic 凭据时无需登录（本地开发/fail-open 前置）。"""
    return bool(_expected_username and _expected_password)


def _is_authed():
    if not _auth_required():
        return True
    if _request_is_local():
        return True
    return bool(session.get("authed"))


@bp.route("/login", methods=["GET"])
def login_page():
    """登录页：已登录或本地请求直接回首页。"""
    if _is_authed():
        return redirect("/")
    return render_template("login.html")


@bp.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if verify_basic_auth(username, password, _expected_username, _expected_password):
        session["authed"] = True
        session.permanent = True
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "用户名或密码错误"}), 401


@bp.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True})


@bp.route("/api/auth/status", methods=["GET"])
def api_auth_status():
    """供前端决定是否显示登出/登录入口。"""
    return jsonify({
        "authed": _is_authed(),
        "local": _request_is_local(),
        "auth_required": _auth_required(),
    })
