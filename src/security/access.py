"""Pure access-control decisions used by Flask routes."""

import hmac
from ipaddress import ip_address


LOCAL_ONLY_HOSTS = {"127.0.0.1", "::1", "localhost"}


def is_local_bind_only(bind_host):
    return bind_host in LOCAL_ONLY_HOSTS


def request_ip_is_local(remote_addr="", forwarded_for="", trust_forwarded=False):
    # X-Forwarded-For 由客户端可控，只有部署在可信反向代理之后
    # （trust_forwarded=True）才允许它参与本地判定，否则可伪造绕过认证。
    if trust_forwarded and forwarded_for:
        remote = forwarded_for
    else:
        remote = remote_addr or ""
    remote = remote.split(",", 1)[0].strip()
    if not remote:
        return False
    try:
        return ip_address(remote).is_loopback
    except ValueError:
        return remote in LOCAL_ONLY_HOSTS


def verify_basic_auth(username, password, expected_username, expected_password):
    if not expected_username or not expected_password:
        return False
    user_ok = hmac.compare_digest(username or "", expected_username)
    pass_ok = hmac.compare_digest(password or "", expected_password)
    return user_ok and pass_ok


def web_auth_failure_kind(
    remote_addr="",
    forwarded_for="",
    bind_host="",
    auth_username="",
    auth_password="",
    expected_username="",
    expected_password="",
    trust_forwarded=False,
):
    if request_ip_is_local(remote_addr, forwarded_for, trust_forwarded) and is_local_bind_only(bind_host):
        return None
    if not expected_username or not expected_password:
        return "forbidden"
    if verify_basic_auth(auth_username, auth_password, expected_username, expected_password):
        return None
    return "auth_required"
