"""Pure helpers for serving downloaded files."""

import os
import subprocess
import sys
from datetime import datetime


def list_download_files(download_dir, format_size, page=1, per_page=100):
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 100), 10), 500)
    files = []

    if not os.path.exists(download_dir):
        return {
            "files": [],
            "page": page,
            "per_page": per_page,
            "total": 0,
            "pages": 0,
        }

    for folder in sorted(os.listdir(download_dir)):
        folder_path = os.path.join(download_dir, folder)
        if not os.path.isdir(folder_path):
            continue
        for filename in sorted(os.listdir(folder_path)):
            file_path = os.path.join(folder_path, filename)
            if os.path.isfile(file_path):
                modified_ts = os.path.getmtime(file_path)
                size_bytes = os.path.getsize(file_path)
                files.append({
                    "folder": folder,
                    "filename": filename,
                    "size": format_size(size_bytes),
                    "size_bytes": size_bytes,
                    "modified": datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d %H:%M"),
                    "modified_ts": modified_ts,
                })

    files.sort(key=lambda item: item["modified_ts"], reverse=True)
    total = len(files)
    start = (page - 1) * per_page
    items = files[start:start + per_page]
    for item in items:
        item.pop("modified_ts", None)

    return {
        "files": items,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": (total + per_page - 1) // per_page,
    }


def resolve_file_path(download_dir, filepath, *, must_be_file=True):
    full_path = os.path.realpath(os.path.join(download_dir, filepath))
    base_path = os.path.realpath(download_dir)
    if os.path.commonpath([base_path, full_path]) != base_path:
        raise ValueError("非法路径")
    if must_be_file and not os.path.isfile(full_path):
        raise FileNotFoundError("文件不存在")
    return full_path


def resolve_download_path(download_dir, *parts, must_exist=False):
    base_dir = os.path.realpath(download_dir)
    candidate = os.path.realpath(os.path.join(base_dir, *parts))
    if os.path.commonpath([base_dir, candidate]) != base_dir:
        raise ValueError("非法路径")
    if must_exist and not os.path.exists(candidate):
        raise FileNotFoundError("文件不存在")
    return candidate


def local_stream_range(file_size, range_header, chunk_size=4 * 1024 * 1024):
    if not range_header:
        return None
    if file_size <= 0 or not range_header.startswith("bytes="):
        raise ValueError("invalid range")

    range_spec = range_header.replace("bytes=", "", 1).split(",", 1)[0].strip()
    if "-" not in range_spec:
        raise ValueError("invalid range")

    start_raw, end_raw = range_spec.split("-", 1)
    if start_raw:
        byte_start = int(start_raw)
        if byte_start < 0 or byte_start >= file_size:
            raise ValueError("invalid range")
        if end_raw:
            byte_end_exclusive = min(int(end_raw) + 1, file_size)
        else:
            byte_end_exclusive = min(byte_start + chunk_size, file_size)
    else:
        suffix_length = int(end_raw) if end_raw else 0
        if suffix_length <= 0:
            raise ValueError("invalid range")
        byte_start = max(file_size - suffix_length, 0)
        byte_end_exclusive = file_size

    if byte_end_exclusive <= byte_start:
        raise ValueError("invalid range")

    content_length = byte_end_exclusive - byte_start
    return {
        "start": byte_start,
        "end": byte_end_exclusive,
        "content_length": content_length,
        "content_range": f"bytes {byte_start}-{byte_end_exclusive - 1}/{file_size}",
    }


def iter_file_chunks(file_path, start, length, chunk_size=65536):
    with open(file_path, "rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            chunk = handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def open_path_with_platform(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def prepare_open_folder(resolve_path, folder, open_folder_enabled, request_is_local, opener=None):
    path = resolve_path(folder, must_exist=True)
    if not os.path.isdir(path):
        raise FileNotFoundError("Folder not found")
    if not open_folder_enabled:
        return {
            "ok": False,
            "path": path,
            "error": "服务器目录打开功能已禁用，下面是服务器目录路径",
        }, 409
    if not request_is_local:
        return {
            "ok": False,
            "path": path,
            "error": "浏览器不在服务器本机，无法直接打开服务器目录",
        }, 409
    (opener or open_path_with_platform)(path)
    return {"ok": True, "path": path}, 200


def _validate_basename(filename):
    if os.path.basename(filename) != filename:
        raise ValueError("非法文件名")


def rename_download_file(resolve_path, folder, old_name, new_name):
    _validate_basename(new_name)
    old_path = resolve_path(folder, old_name, must_exist=True)
    new_path = resolve_path(folder, new_name)
    if os.path.exists(new_path):
        raise FileExistsError("Target file name already exists")
    os.rename(old_path, new_path)


def delete_download_file(resolve_path, folder, filename):
    _validate_basename(filename)
    path = resolve_path(folder, filename, must_exist=True)
    if not os.path.isfile(path):
        raise FileNotFoundError("File not found")
    os.remove(path)
