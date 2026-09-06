"""Upload task status constants and state helpers."""

import hashlib
import time
from typing import Dict, Any, Optional

UPLOAD_TERMINAL_STATES = frozenset({"done", "error", "cancelled"})
UPLOAD_ACTIVE_STATES = frozenset({"pending", "uploading"})
UPLOAD_ALL_STATES = UPLOAD_TERMINAL_STATES | UPLOAD_ACTIVE_STATES


def make_upload_id(filepath: str, task_id: Optional[str] = None) -> str:
    """Generate a stable upload task identifier."""
    if task_id:
        return f"up_{task_id}"
    raw = filepath.strip()
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"up_{digest}"


def create_upload_task(
    filepath: str,
    folder: str,
    filename: str,
    remote_dir: str,
    total_bytes: int = 0,
    delete_local: bool = False,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a new upload task dictionary."""
    upload_id = make_upload_id(filepath, task_id)
    now = time.time()
    return {
        "upload_id": upload_id,
        "task_id": task_id or "",
        "folder": folder or "",
        "filename": filename or "",
        "filepath": filepath,
        "remote_dir": remote_dir or "",
        "status": "pending",
        "progress": 0.0,
        "speed": "",
        "speed_bps": 0.0,
        "bytes_transferred": 0,
        "total_bytes": int(total_bytes or 0),
        "error": "",
        "created_at": now,
        "updated_at": now,
        "finished_at": None,
        "delete_local": bool(delete_local),
    }
