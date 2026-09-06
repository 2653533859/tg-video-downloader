"""SQLite persistence for upload tasks."""

import json
import os
import sqlite3
import threading
from typing import Dict, Any, Optional, List


class UploadPersistence:
    """Thread-safe SQLite storage for upload tasks using WAL mode."""

    def __init__(self, db_path: str = ".task_state/uploads.sqlite3"):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS upload_tasks (
                            upload_id TEXT PRIMARY KEY,
                            task_id TEXT,
                            folder TEXT,
                            filename TEXT,
                            filepath TEXT,
                            remote_dir TEXT,
                            status TEXT,
                            progress REAL,
                            speed TEXT,
                            speed_bps REAL,
                            bytes_transferred INTEGER,
                            total_bytes INTEGER,
                            error TEXT,
                            created_at REAL,
                            updated_at REAL,
                            finished_at REAL,
                            delete_local INTEGER,
                            task_json TEXT
                        );
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_status ON upload_tasks(status);")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_upload_filepath ON upload_tasks(filepath);")
            finally:
                conn.close()

    def save_task(self, task: Dict[str, Any]):
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute("""
                        INSERT INTO upload_tasks (
                            upload_id, task_id, folder, filename, filepath,
                            remote_dir, status, progress, speed, speed_bps,
                            bytes_transferred, total_bytes, error, created_at,
                            updated_at, finished_at, delete_local, task_json
                        ) VALUES (
                            :upload_id, :task_id, :folder, :filename, :filepath,
                            :remote_dir, :status, :progress, :speed, :speed_bps,
                            :bytes_transferred, :total_bytes, :error, :created_at,
                            :updated_at, :finished_at, :delete_local, :task_json
                        )
                        ON CONFLICT(upload_id) DO UPDATE SET
                            status=excluded.status,
                            progress=excluded.progress,
                            speed=excluded.speed,
                            speed_bps=excluded.speed_bps,
                            bytes_transferred=excluded.bytes_transferred,
                            total_bytes=excluded.total_bytes,
                            error=excluded.error,
                            updated_at=excluded.updated_at,
                            finished_at=excluded.finished_at,
                            delete_local=excluded.delete_local,
                            task_json=excluded.task_json;
                    """, {
                        "upload_id": task["upload_id"],
                        "task_id": task.get("task_id", ""),
                        "folder": task.get("folder", ""),
                        "filename": task.get("filename", ""),
                        "filepath": task.get("filepath", ""),
                        "remote_dir": task.get("remote_dir", ""),
                        "status": task.get("status", "pending"),
                        "progress": float(task.get("progress", 0.0)),
                        "speed": task.get("speed", ""),
                        "speed_bps": float(task.get("speed_bps", 0.0)),
                        "bytes_transferred": int(task.get("bytes_transferred", 0)),
                        "total_bytes": int(task.get("total_bytes", 0)),
                        "error": task.get("error", ""),
                        "created_at": float(task.get("created_at", 0.0)),
                        "updated_at": float(task.get("updated_at", 0.0)),
                        "finished_at": task.get("finished_at"),
                        "delete_local": 1 if task.get("delete_local") else 0,
                        "task_json": json.dumps(task),
                    })
            finally:
                conn.close()

    def get_task(self, upload_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute("SELECT task_json FROM upload_tasks WHERE upload_id = ?", (upload_id,)).fetchone()
                if row and row["task_json"]:
                    return json.loads(row["task_json"])
                return None
            finally:
                conn.close()

    def get_by_filepath(self, filepath: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT task_json FROM upload_tasks WHERE filepath = ? ORDER BY updated_at DESC LIMIT 1",
                    (filepath,)
                ).fetchone()
                if row and row["task_json"]:
                    return json.loads(row["task_json"])
                return None
            finally:
                conn.close()

    def list_tasks(self, limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._get_connection()
            try:
                if status:
                    rows = conn.execute(
                        "SELECT task_json FROM upload_tasks WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                        (status, limit)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT task_json FROM upload_tasks ORDER BY updated_at DESC LIMIT ?",
                        (limit,)
                    ).fetchall()
                return [json.loads(r["task_json"]) for r in rows if r["task_json"]]
            finally:
                conn.close()

    def delete_task(self, upload_id: str):
        with self._lock:
            conn = self._get_connection()
            try:
                with conn:
                    conn.execute("DELETE FROM upload_tasks WHERE upload_id = ?", (upload_id,))
            finally:
                conn.close()
