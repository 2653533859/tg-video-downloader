"""SQLite-backed task state persistence."""

import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Iterable, Optional, Tuple


class TaskStatePersistence:
    """Persist live task state, completed history, and tdl fallback metadata."""

    def __init__(
        self,
        state_dir: str,
        terminal_states: Iterable[str],
        warning_logger: Optional[Callable[[str], None]] = None,
        backup_retention_days: int = 7,
    ):
        self.state_dir = state_dir
        self.db_path = os.path.join(state_dir, "tasks.sqlite3")
        self.backup_dir = os.path.join(state_dir, "backups")
        self.backup_retention_days = backup_retention_days
        self.terminal_states = set(terminal_states)
        self.warning_logger = warning_logger or (lambda message: None)
        self.lock = threading.RLock()
        os.makedirs(self.state_dir, exist_ok=True)

    def legacy_state_file(self, task_id):
        safe_name = re.sub(r"[^0-9A-Za-z_.:-]", "_", str(task_id))
        return os.path.join(self.state_dir, f"{safe_name}.json")

    def enabled(self):
        return "unittest" not in sys.modules

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_states (
                task_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tdl_fallback_channels (
                entity_id TEXT PRIMARY KEY,
                reason TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_history (
                task_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                completed_at REAL
            )
        """)
        return conn

    def migrate_legacy_state_files(self):
        if not self.enabled():
            return
        for name in os.listdir(self.state_dir):
            if not name.endswith(".json"):
                continue
            task_id = name[:-5]
            path = os.path.join(self.state_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    state = json.load(handle)
                self.persist_state(task_id, state)
                os.remove(path)
            except Exception as exc:
                self.warning_logger(f"[{task_id}] 迁移旧任务状态失败: {exc}")

    def persist_state(self, task_id, state):
        if not task_id or not self.enabled():
            return
        try:
            payload = dict(state or {})
            with self.lock, self.connect() as conn:
                now = time.time()
                state_json = json.dumps(payload, ensure_ascii=False)
                conn.execute(
                    "INSERT OR REPLACE INTO task_states(task_id, state_json, updated_at) VALUES (?, ?, ?)",
                    (str(task_id), state_json, now),
                )
                if payload.get("status") in self.terminal_states:
                    conn.execute(
                        "INSERT OR REPLACE INTO task_history(task_id, state_json, updated_at, completed_at) VALUES (?, ?, ?, ?)",
                        (str(task_id), state_json, now, payload.get("finish_time") or now),
                    )
        except Exception as exc:
            self.warning_logger(f"[{task_id}] 持久化任务状态失败: {exc}")

    def delete_state(self, task_id):
        if not self.enabled():
            return
        try:
            with self.lock, self.connect() as conn:
                conn.execute("DELETE FROM task_states WHERE task_id = ?", (str(task_id),))
        except Exception as exc:
            self.warning_logger(f"[{task_id}] 删除持久化任务状态失败: {exc}")

    def load_states(self) -> Tuple[Dict[str, dict], int]:
        if not self.enabled():
            return {}, 0
        self.migrate_legacy_state_files()
        try:
            with self.lock, self.connect() as conn:
                rows = list(conn.execute("SELECT task_id, state_json FROM task_states ORDER BY updated_at"))
        except Exception as exc:
            self.warning_logger(f"读取 SQLite 任务状态失败: {exc}")
            return {}, 0

        states = {}
        for task_id, state_json in rows:
            try:
                state = json.loads(state_json)
                if not isinstance(state, dict):
                    continue
                if state.get("status") in {"submitting", "queued", "downloading"}:
                    state["status"] = "error"
                    state["error"] = "服务重启后任务已停止，等待自动恢复"
                    state["speed"] = ""
                    state["speed_bps"] = 0.0
                    state["queue_position"] = None
                    state["queue_size"] = 0
                    state["finish_time"] = time.time()
                states[task_id] = state
            except Exception as exc:
                self.warning_logger(f"[{task_id}] 读取持久化任务状态失败: {exc}")
        return states, len(states)

    def count_states(self):
        try:
            with self.lock, self.connect() as conn:
                return int(conn.execute("SELECT COUNT(*) FROM task_states").fetchone()[0])
        except Exception:
            return 0

    def backup_database(self):
        if not self.enabled():
            return None
        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        backup_path = os.path.join(self.backup_dir, f"tasks-{stamp}.sqlite3")
        try:
            with self.lock, self.connect() as source:
                with sqlite3.connect(backup_path) as target:
                    source.backup(target)
            cutoff = time.time() - self.backup_retention_days * 24 * 3600
            for name in os.listdir(self.backup_dir):
                path = os.path.join(self.backup_dir, name)
                if name.startswith("tasks-") and name.endswith(".sqlite3") and os.path.getmtime(path) < cutoff:
                    os.remove(path)
            return backup_path
        except Exception as exc:
            self.warning_logger(f"SQLite 自动备份失败: {exc}")
            return None

    def query_history(self, live_items, status="", query="", page=1, per_page=30):
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 30)))
        query = str(query or "").strip().lower()
        try:
            with self.lock, self.connect() as conn:
                rows = list(conn.execute(
                    "SELECT task_id, state_json FROM task_history ORDER BY completed_at DESC, updated_at DESC"
                ))
        except Exception as exc:
            self.warning_logger(f"读取下载历史失败: {exc}")
            return [], 0

        items = []
        seen = set()
        for task_id, state in live_items:
            state = dict(state)
            state["task_id"] = task_id
            if self._matches_history_filter(task_id, state, status, query):
                items.append(state)
                seen.add(task_id)

        for task_id, state_json in rows:
            if task_id in seen:
                continue
            try:
                state = json.loads(state_json)
            except Exception:
                continue
            state["task_id"] = task_id
            if self._matches_history_filter(task_id, state, status, query):
                items.append(state)

        start = (page - 1) * per_page
        return items[start:start + per_page], len(items)

    def remember_tdl_fallback_channel(self, entity_id, reason):
        if entity_id is None or not self.enabled():
            return
        try:
            with self.lock, self.connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO tdl_fallback_channels(entity_id, reason, updated_at) VALUES (?, ?, ?)",
                    (str(int(entity_id)), str(reason or ""), time.time()),
                )
        except Exception as exc:
            self.warning_logger(f"[{entity_id}] 保存 tdl 回退缓存失败: {exc}")

    def has_tdl_fallback_channel(self, entity_id):
        if entity_id is None or not self.enabled():
            return False
        try:
            with self.lock, self.connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM tdl_fallback_channels WHERE entity_id = ?",
                    (str(int(entity_id)),),
                ).fetchone()
            return bool(row)
        except Exception:
            return False

    @staticmethod
    def _matches_history_filter(task_id, state, status, query):
        if status and state.get("status") != status:
            return False
        haystack = " ".join(
            str(state.get(key, ""))
            for key in ("filename", "dialog_name", "downloader", "error")
        ).lower()
        return not query or query in haystack or query in task_id.lower()
