"""Download status payload helpers."""

import time


def build_download_status_payload(
    *,
    recover_stalled_tasks,
    restore_resume_tasks,
    status_lock,
    download_status,
    terminal_states,
    drop_task_state,
    get_queue_status,
    now_func=time.time,
    terminal_ttl=3600,
):
    recover_stalled_tasks()
    restore_resume_tasks()
    now = now_func()

    with status_lock:
        stale = [
            task_id
            for task_id, state in list(download_status.items())
            if state.get("status") in terminal_states
            and state.get("finish_time")
            and now - state["finish_time"] > terminal_ttl
        ]
        for task_id in stale:
            drop_task_state(task_id)
        tasks = {
            task_id: dict(state)
            for task_id, state in list(download_status.items())
        }

    return {"tasks": tasks, "queue": get_queue_status()}
