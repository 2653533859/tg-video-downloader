"""Download path helpers."""

import os


def sanitize_dialog_name(dialog_name):
    return "".join(
        char if char.isalnum() or char in " _-" else "_"
        for char in (dialog_name or "unknown")
    ).strip() or "unknown"


def download_dir_for_dialog(base_dir, dialog_name):
    return os.path.join(base_dir, sanitize_dialog_name(dialog_name))


def resolve_tdl_progress_path(filepath):
    tmp_path = filepath + ".tmp"
    tmp_exists = os.path.exists(tmp_path)
    final_exists = os.path.exists(filepath)
    if tmp_exists and final_exists:
        if os.path.getsize(filepath) >= os.path.getsize(tmp_path):
            return filepath
        return tmp_path
    if tmp_exists:
        return tmp_path
    return filepath


def prepare_telegram_fallback_target(filepath):
    tmp_path = filepath + ".tmp"
    if not os.path.exists(tmp_path):
        return filepath
    if os.path.exists(filepath):
        if os.path.getsize(filepath) >= os.path.getsize(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return filepath
        try:
            os.remove(filepath)
        except OSError:
            pass
    os.replace(tmp_path, filepath)
    return filepath
