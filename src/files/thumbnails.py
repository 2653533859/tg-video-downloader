"""Thumbnail cache helpers."""

import os
import tempfile
import time


def thumbnail_cache_path(cache_dir, entity_id, msg_id):
    key = f"{entity_id}_{msg_id}" if entity_id is not None else f"unknown_{msg_id}"
    return os.path.join(cache_dir, f"{key}.jpg")


def write_thumbnail(cache_dir, entity_id, msg_id, data):
    os.makedirs(cache_dir, exist_ok=True)
    target = thumbnail_cache_path(cache_dir, entity_id, msg_id)
    fd, temp_path = tempfile.mkstemp(prefix=".thumb-", suffix=".tmp", dir=cache_dir)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp_path, target)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise
    return target


def cleanup_thumbnail_cache(cache_dir, max_age_seconds, max_bytes, now=None):
    now = time.time() if now is None else now
    entries = []
    total_bytes = 0
    removed = 0

    if not os.path.isdir(cache_dir):
        return {"bytes": 0, "removed": 0}

    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        stat = os.stat(path)
        if now - stat.st_mtime > max_age_seconds:
            os.remove(path)
            removed += 1
            continue
        entries.append((stat.st_mtime, stat.st_size, path))
        total_bytes += stat.st_size

    for _, size, path in sorted(entries):
        if total_bytes <= max_bytes:
            break
        os.remove(path)
        total_bytes -= size
        removed += 1

    return {"bytes": total_bytes, "removed": removed}
