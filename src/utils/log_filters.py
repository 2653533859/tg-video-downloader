"""Logging redaction filter — masks sensitive values in log records.

日志可能无意间带出凭据（密码、api_hash、relay/HMAC token、Basic Auth 头、
Telethon session 串）。RedactionFilter 在写入前对消息做正则脱敏，保留键名、
只替换值，兼顾可读性与安全。挂在 logger 上即可全局生效。
"""

import logging
import re


# (?i) 忽略大小写。顺序敏感：Basic/Bearer 头先脱敏，再处理 key=value。
# 键用 \w* 前缀覆盖 env 变量式命名（WEB_AUTH_PASSWORD / TG_API_HASH /
# RELAY_TOKEN_SECRET 等），但敏感词必须紧邻分隔符（无 \w* 后缀），以免误伤
# token_ttl / authorization_status 这类以敏感词为前缀的良性字段。
_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)(\b(?:basic|bearer)\s+)([A-Za-z0-9+/=._-]{8,})"),
    re.compile(
        r"(?i)(\w*(?:password|passwd|pwd|api_?hash|secret|token|authorization)\s*[=:]\s*[\"']?)"
        r"([^\s,&\"';]+)"
    ),
]

_MASK = "***"


class RedactionFilter(logging.Filter):
    """logging.Filter：把记录中的敏感值替换为 ***。"""

    def filter(self, record):
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def redact(text):
    if not text:
        return text
    result = str(text)
    for pattern in _SENSITIVE_PATTERNS:
        result = pattern.sub(lambda m: m.group(1) + _MASK, result)
    return result
