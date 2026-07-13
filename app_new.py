#!/usr/bin/env python3
"""Telegram 视频下载器 - Blueprint 装配入口。

This entry point keeps app.py as the legacy runtime owner for now, while
registering the split route modules against the real runtime functions.
"""

import time

from flask import Flask, jsonify, request
from werkzeug.routing import BaseConverter

import app as runtime
from config import (
    API_HASH,
    API_ID,
    DOWNLOAD_DIR,
    OPEN_FOLDER_ENABLED,
    PROXY_CONFIG,
    RELAY_TOKEN_SECRET,
    TDL_BINARY,
    TRUST_FORWARDED_FOR,
    WEB_AUTH_PASSWORD,
    WEB_AUTH_USERNAME,
    WEB_BIND_HOST,
    WEB_BIND_PORT,
)
from relay_tokens import verify_relay_token
from src.security import require_web_auth
from src.system import start_runtime_services, validate_runtime_config
from src.routes import (
    download,
    download_bp,
    files,
    files_bp,
    misc,
    misc_bp,
    relay,
    relay_bp,
    system,
    system_bp,
    telegram,
    telegram_bp,
)


class SignedIntConverter(BaseConverter):
    regex = r"-?\d+"

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(int(value))


app = Flask(__name__)
app.url_map.converters["signed_int"] = SignedIntConverter


def _runtime_attr(name):
    return getattr(runtime, name)


def init_all_blueprints():
    files.init_blueprint({
        "resolve_path_func": runtime.resolve_current_download_path,
        "is_local_func": runtime.current_request_is_local,
        "open_folder_enabled": OPEN_FOLDER_ENABLED,
    })

    system.init_blueprint({
        "ensure_tg_conn_func": runtime.ensure_tg_connection,
        "get_tg_connected_func": lambda: runtime.tg_connected,
        "get_tg_error_func": lambda: runtime.tg_connect_error,
        "get_tg_user_func": lambda: runtime.tg_user_info,
        "get_queue_func": runtime.get_queue_status,
        "get_tdl_func": runtime.get_tdl_status,
        "proxy_config": PROXY_CONFIG,
        "tdl_binary": TDL_BINARY,
    })

    telegram.init_blueprint({
        "tg_client": runtime.tg_client,
        "run_async_func": runtime.run_async,
        "kickoff_dialogs_func": runtime.kickoff_dialogs_refresh,
        "dialogs_snapshot_func": runtime.dialogs_cache_snapshot,
        "resolve_entity_func": runtime.resolve_requested_entity,
        "video_info_func": runtime.video_info_for_message,
        "make_excerpt_func": runtime.make_excerpt,
        "message_text_func": runtime.message_text,
        "get_cached_message_func": runtime.get_cached_message,
        "resolve_message_func": runtime.resolve_message,
        "abort_debug_func": runtime.abort_if_debug_disabled,
        "thumb_dir": runtime.THUMB_DIR,
        "relay_token_secret": RELAY_TOKEN_SECRET,
        "dialogs_cache_ref": runtime.dialogs_cache,
        "current_entity_cache_ref": runtime.current_entity_cache,
        "videos_cache_ref": runtime.videos_cache,
        "replies_cache_ref": runtime.replies_cache,
        "video_service": runtime.telegram_video_service,
        "get_video_info_func": runtime.get_video_info,
        "build_relay_url_func": runtime.build_relay_url,
    })

    download.init_blueprint({
        "current_entity_cache": runtime.current_entity_cache,
        "make_task_id_func": runtime.make_task_id,
        "copy_task_state_func": runtime.copy_task_state,
        "set_task_state_func": runtime.set_task_state,
        "update_task_state_func": runtime.update_task_state,
        "get_cached_message_func": runtime.get_cached_message,
        "resolve_message_func": runtime.resolve_message,
        "mark_cancelled_func": runtime.mark_download_cancelled,
        "clear_cancelled_func": runtime.clear_download_cancelled,
        "supports_tdl_func": runtime.supports_tdl_download,
        "enqueue_download_func": runtime.enqueue_download,
        "remove_from_queue_func": runtime.remove_from_queue,
        "get_tdl_process_func": runtime.get_tdl_process,
        "get_download_status_func": runtime.get_download_status_payload,
        "format_size_func": runtime.format_size,
        "get_video_info_func": runtime.get_video_info,
        "terminal_states": runtime.TERMINAL_STATES,
        "last_download_dialog_ref": runtime.last_download_dialog,
        "resume_all_func": runtime.resume_all_incomplete_tasks,
        "resume_task_func": runtime.resume_task,
        "move_queued_task_func": runtime.move_queued_task,
        "drop_task_state_func": runtime.drop_task_state,
        "clear_tdl_error_func": runtime.clear_tdl_error,
        "clear_resume_info_func": runtime.clear_resume_info,
        "get_queue_status_func": runtime.get_queue_status,
        "status_lock": runtime.status_lock,
        "download_status_ref": runtime.download_status,
    })

    misc.init_blueprint({
        "download_dir": DOWNLOAD_DIR,
        "format_size_func": runtime.format_size,
        "query_task_history_func": runtime.get_task_history_payload,
        "get_download_status_func": lambda: dict(runtime.download_status),
        "clear_all_tasks_func": runtime.clear_tasks_for_scope,
        "clear_task_ids_func": runtime.clear_task_ids,
        "get_recovery_candidates_func": runtime.log_recovery_candidates,
        "recover_candidates_func": runtime.recover_tasks_from_candidates,
        "abort_debug_func": runtime.abort_if_debug_disabled,
        "resolve_download_path_func": runtime.resolve_current_download_path,
        "debug_service": runtime.telegram_debug_service,
    })

    relay.init_blueprint({
        "relay_token_secret": RELAY_TOKEN_SECRET,
        "max_concurrent_relays": runtime.MAX_CONCURRENT_RELAYS,
        "verify_relay_token_func": verify_relay_token,
        "get_relay_media_func": runtime.get_relay_media,
        "parse_range_func": runtime.parse_range,
        "iter_relay_bytes_func": runtime.iter_relay_bytes,
        "log_warning_func": runtime.log_warning,
        "log_info_func": runtime.log_info,
        "log_error_func": runtime.log_error,
    })


def register_all_blueprints():
    app.register_blueprint(system_bp)
    app.register_blueprint(files_bp)
    app.register_blueprint(telegram_bp)
    app.register_blueprint(download_bp)
    app.register_blueprint(misc_bp)
    app.register_blueprint(relay_bp)


@app.before_request
def enforce_access_control():
    if request.path.startswith("/relay/"):
        return None
    return require_web_auth(
        request,
        WEB_BIND_HOST,
        WEB_AUTH_USERNAME,
        WEB_AUTH_PASSWORD,
        trust_forwarded=TRUST_FORWARDED_FOR,
    )


@app.route("/api/settings/proxy", methods=["GET"])
def api_get_proxy_settings():
    return runtime.api_get_proxy_settings()


@app.route("/api/settings/proxy", methods=["POST"])
def api_set_proxy_settings():
    return runtime.api_set_proxy_settings()


def start_runtime():
    return start_runtime_services(
        download_dir=DOWNLOAD_DIR,
        load_persisted_states=runtime.load_persisted_task_states,
        log_info=runtime.log_info,
        restore_resume_tasks=runtime.restore_resume_tasks_into_memory,
        start_background_clients=runtime.start_background_clients,
        auto_resume_incomplete_tasks=runtime.auto_resume_incomplete_tasks,
        download_watchdog=runtime.download_watchdog,
        thumbnail_cleanup_loop=runtime.run_thumbnail_cleanup_loop,
        task_database_backup_loop=runtime.run_task_database_backup_loop,
    )


def validate_config():
    validate_runtime_config(
        API_ID,
        API_HASH,
        WEB_BIND_HOST,
        WEB_AUTH_USERNAME,
        WEB_AUTH_PASSWORD,
        relay_token_secret=RELAY_TOKEN_SECRET,
    )


init_all_blueprints()
register_all_blueprints()


if __name__ == "__main__":
    validate_config()
    start_runtime()
    runtime._install_shutdown_signal_handlers()
    time.sleep(3)
    print(f"Web UI 启动: http://{WEB_BIND_HOST}:{WEB_BIND_PORT}")
    app.run(host=WEB_BIND_HOST, port=WEB_BIND_PORT, threaded=True)
