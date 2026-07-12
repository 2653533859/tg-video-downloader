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
    return Response(
        "Authentication required",
        401,
        {"WWW-Authenticate": f'Basic realm="{realm}"'},
    )
