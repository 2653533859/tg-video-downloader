"""Application startup orchestration."""

import os
import threading

from src.security import is_local_bind_only


RELAY_SECRET_MIN_LENGTH = 32


def validate_runtime_config(
    api_id,
    api_hash,
    web_bind_host,
    web_auth_username,
    web_auth_password,
    relay_token_secret="",
):
    if not api_id or not api_hash:
        raise RuntimeError("Missing TG_API_ID/TG_API_HASH environment variables")
    if not is_local_bind_only(web_bind_host) and (not web_auth_username or not web_auth_password):
        raise RuntimeError("Non-local binding requires WEB_AUTH_USERNAME and WEB_AUTH_PASSWORD")
    if relay_token_secret and len(relay_token_secret) < RELAY_SECRET_MIN_LENGTH:
        raise RuntimeError(
            f"RELAY_TOKEN_SECRET too short: must be at least "
            f"{RELAY_SECRET_MIN_LENGTH} characters (leave empty to disable relay)"
        )


def start_runtime_services(
    *,
    download_dir,
    load_persisted_states,
    log_info,
    restore_resume_tasks,
    start_background_clients,
    auto_resume_incomplete_tasks,
    download_watchdog,
    thumbnail_cleanup_loop,
    task_database_backup_loop,
    thread_factory=threading.Thread,
):
    os.makedirs(download_dir, exist_ok=True)

    restored_states = load_persisted_states()
    if restored_states:
        log_info(f"已加载持久化下载任务: {restored_states}")

    restore_resume_tasks()
    background = start_background_clients()

    auto_resume_thread = thread_factory(target=auto_resume_incomplete_tasks, daemon=True)
    auto_resume_thread.start()

    download_watchdog.start()

    thumbnail_thread = thread_factory(target=thumbnail_cleanup_loop, daemon=True)
    thumbnail_thread.start()

    backup_thread = thread_factory(target=task_database_backup_loop, daemon=True)
    backup_thread.start()

    return {
        "restored_states": restored_states,
        "background": background,
        "auto_resume_thread": auto_resume_thread,
        "thumbnail_thread": thumbnail_thread,
        "backup_thread": backup_thread,
    }
