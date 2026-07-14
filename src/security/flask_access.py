"""Flask access-control adapters."""

from flask import Response, jsonify

from .access import web_auth_failure_kind


def require_web_auth(
    request,
    bind_host,
    expected_username,
    expected_password,
    realm="tg-video-downloader",
    trust_forwarded=False,
):
    auth = request.authorization
    failure = web_auth_failure_kind(
        remote_addr=request.remote_addr or "",
        forwarded_for=request.headers.get("X-Forwarded-For", ""),
        bind_host=bind_host,
        auth_username=(auth.username if auth else ""),
        auth_password=(auth.password if auth else ""),
        expected_username=expected_username,
        expected_password=expected_password,
        trust_forwarded=trust_forwarded,
    )
    if failure is None:
        return None
    if failure == "forbidden":
        return jsonify({"error": "Web auth is required for non-local access"}), 403
    # 仅当客户端主动带了 Authorization 头（真正的 API / Basic 客户端）时才回
    # WWW-Authenticate 促其重试；浏览器的普通请求（favicon、子资源等，不带该头）
    # 返回不带此头的 401，避免浏览器弹出原生 Basic 登录框——网页 /login 才是
    # 浏览器端入口。healthcheck 走豁免端点、curl -u 会预先带头，均不受影响。
    headers = {"WWW-Authenticate": f'Basic realm="{realm}"'} if auth else {}
    return Response("Authentication required", 401, headers)
