"""Task list, cleanup, and recovery action helpers."""


def clear_tasks_by_scope(
    *,
    scope="terminal",
    terminal_states,
    status_lock,
    download_status,
    drop_task_state,
    clear_download_cancelled,
    clear_tdl_error,
    clear_resume_info,
):
    clearable = {"error", "cancelled"}
    if scope == "all":
        clearable = set(terminal_states) | {"error", "cancelled"}

    with status_lock:
        task_ids = [
            task_id
            for task_id, state in list(download_status.items())
            if state.get("status") in clearable
        ]

    cleared = 0
    for task_id in task_ids:
        drop_task_state(task_id)
        clear_download_cancelled(task_id)
        clear_tdl_error(task_id)
        clear_resume_info(task_id)
        cleared += 1

    return cleared


def query_task_history_payload(query_task_history, status="", query="", page=1, per_page=30):
    page = max(1, int(page or 1))
    per_page = min(100, max(1, int(per_page or 30)))
    items, total = query_task_history(status, query, page, per_page)
    return {
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page,
    }


def recover_candidate_tasks(
    *,
    task_ids,
    get_recovery_candidates,
    resume_task,
    dialog_name="日志恢复",
    limit=500,
):
    submitted = []
    errors = {}
    allowed = {item["task_id"] for item in get_recovery_candidates(limit)}

    for task_id in task_ids:
        if task_id not in allowed:
            errors[task_id] = "任务不在可恢复日志列表中"
            continue
        try:
            result = resume_task(task_id, dialog_name=dialog_name, auto=False)
            if result.get("ok"):
                submitted.append(task_id)
            else:
                errors[task_id] = result.get("error", "恢复失败")
        except Exception as exc:
            errors[task_id] = str(exc)

    return {"ok": True, "submitted": submitted, "errors": errors}
