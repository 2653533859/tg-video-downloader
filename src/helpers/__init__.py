"""
辅助函数模块
"""
from .core import (
    make_task_id,
    make_excerpt,
    sanitize_filename,
    resolve_download_path,
    format_user_display,
    message_text,
    calc_download_timeout,
    request_ip_is_local,
    is_local_bind_only,
    abort_if_debug_disabled,
    require_web_auth,
)

from .telegram import (
    TelegramHelper,
    get_video_info,
    video_info_for_message,
    supports_tdl_download,
    build_tdl_message_url,
)

__all__ = [
    # core
    'make_task_id',
    'make_excerpt',
    'sanitize_filename',
    'resolve_download_path',
    'format_user_display',
    'message_text',
    'calc_download_timeout',
    'request_ip_is_local',
    'is_local_bind_only',
    'abort_if_debug_disabled',
    'require_web_auth',
    # telegram
    'TelegramHelper',
    'get_video_info',
    'video_info_for_message',
    'supports_tdl_download',
    'build_tdl_message_url',
]
