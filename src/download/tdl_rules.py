"""Pure decision rules for tdl download handling."""


def should_fallback_from_tdl(error_message):
    text = (error_message or "").lower()
    return any(
        token in text
        for token in (
            "chat_id_invalid",
            "channel_invalid",
            "peer_id_invalid",
        )
    )


def classify_tdl_error(error_message):
    text = (error_message or "").lower()
    if should_fallback_from_tdl(text):
        return "fatal"
    if "未从断点续传" in text or "断点失效" in text:
        return "network"
    if "eof" not in text:
        if "timeout" in text or "i/o timeout" in text:
            return "timeout"
        if any(
            token in text
            for token in (
                "proxy",
                "connection reset",
                "connection refused",
                "connection aborted",
                "broken pipe",
                "context canceled",
                "context deadline exceeded",
                "transport is closing",
                "rpc error",
                "stream error",
                "read tcp",
                "dial tcp",
                "tls handshake timeout",
            )
        ):
            return "network"
        return "fatal"
    return "eof"


def should_retry_tdl_error(
    error_message,
    retry_count,
    *,
    max_eof_retries,
    max_retry_attempts,
    max_stalled_eof_retries,
    current_size=0,
    last_retry_size=0,
):
    category = classify_tdl_error(error_message)
    if category not in {"eof", "timeout", "network"}:
        return False
    if retry_count >= min(max_eof_retries, max_retry_attempts):
        return False
    if category == "eof" and last_retry_size > 0 and current_size <= last_retry_size:
        return retry_count < max_stalled_eof_retries
    return True


def should_capture_tdl_error_line(line):
    text = (line or "").strip()
    if not text:
        return False
    if "\x1b[" in text:
        return False
    lowered = text.lower()
    if lowered.startswith("cpu: "):
        return False
    if "%]" in lowered or "; ~eta:" in lowered:
        return False
    return True


def reconcile_tdl_progress_size(current_size, written, allow_offset_correction):
    if current_size < written and allow_offset_correction:
        return current_size, False
    if current_size < written:
        return written, allow_offset_correction
    return current_size, allow_offset_correction


def did_tdl_restart_from_scratch(
    retry_count,
    previous_size,
    current_size,
    *,
    start_offset=0,
    restart_reset_min_bytes,
):
    effective_previous = previous_size if retry_count > 0 else start_offset
    if effective_previous <= 0 or current_size <= 0:
        return False
    if effective_previous < restart_reset_min_bytes:
        return False
    if current_size >= effective_previous:
        return False
    if effective_previous - current_size < restart_reset_min_bytes:
        return False
    return current_size < int(effective_previous * 0.9)


def validate_tdl_completion(total_bytes, final_size, format_size):
    if total_bytes and final_size != total_bytes:
        return f"下载不完整：期望 {format_size(total_bytes)}，实际 {format_size(final_size)}"
    return None


def error_priority(message):
    category = classify_tdl_error(message)
    return {
        "eof": 3,
        "timeout": 3,
        "network": 2,
        "fatal": 1,
    }.get(category, 0)


def choose_more_specific_tdl_error(current_message, candidate_message):
    candidate = (candidate_message or "").strip()
    if not candidate:
        return current_message
    current = (current_message or "").strip()
    if not current:
        return candidate
    if error_priority(candidate) >= error_priority(current):
        return candidate
    return current
