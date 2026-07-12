"""
数据库连接池和优化的数据库访问层
"""
import sqlite3
import json
import threading
import time
from contextlib import contextmanager
from typing import Optional, Dict, Any, List
from queue import Queue, Full


class DatabaseConnectionPool:
    """SQLite 连接池"""

    def __init__(self, db_path: str, pool_size: int = 5, timeout: float = 10.0):
        """
        初始化连接池

        Args:
            db_path: 数据库文件路径
            pool_size: 连接池大小
            timeout: 连接超时时间（秒）
        """
        self.db_path = db_path
        self.pool_size = pool_size
        self.timeout = timeout
        self._pool = Queue(maxsize=pool_size)
        self._lock = threading.Lock()
        self._initialized = False

    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接"""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        """初始化连接池"""
        with self._lock:
            if self._initialized:
                return

            # 创建表结构和索引
            conn = self._create_connection()
            try:
                self._create_tables(conn)
                self._create_indexes(conn)
                conn.commit()
            finally:
                conn.close()

            # 预创建连接
            for _ in range(self.pool_size):
                try:
                    self._pool.put_nowait(self._create_connection())
                except Full:
                    break

            self._initialized = True

    def _create_tables(self, conn: sqlite3.Connection):
        """创建表结构"""
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
                completed_at REAL,
                status TEXT,
                file_name TEXT,
                total_bytes INTEGER
            )
        """)

    def _create_indexes(self, conn: sqlite3.Connection):
        """创建索引以优化查询性能"""
        # task_states 索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_states_updated_at
            ON task_states(updated_at DESC)
        """)

        # task_history 索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_history_status
            ON task_history(status)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_history_completed_at
            ON task_history(completed_at DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_history_file_name
            ON task_history(file_name)
        """)

        # 复合索引用于常见查询
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_task_history_status_completed
            ON task_history(status, completed_at DESC)
        """)

        # tdl_fallback_channels 索引
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_tdl_fallback_updated_at
            ON tdl_fallback_channels(updated_at DESC)
        """)

    @contextmanager
    def get_connection(self):
        """
        获取数据库连接（上下文管理器）

        Usage:
            with pool.get_connection() as conn:
                conn.execute(...)
        """
        conn = None
        try:
            # 尝试从池中获取连接
            try:
                conn = self._pool.get(timeout=5)
            except:
                # 池已空，创建新连接
                conn = self._create_connection()

            yield conn

        finally:
            if conn:
                try:
                    # 归还连接到池
                    self._pool.put_nowait(conn)
                except Full:
                    # 池已满，关闭连接
                    conn.close()

    def close_all(self):
        """关闭所有连接"""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except:
                break


class TaskDatabase:
    """任务数据库访问层（使用连接池）"""

    def __init__(self, pool: DatabaseConnectionPool):
        self.pool = pool

    def persist_task_state(self, task_id: str, state: Dict[str, Any]) -> bool:
        """持久化任务状态"""
        if not task_id:
            return False

        try:
            payload = dict(state or {})
            payload.pop("_lock", None)

            with self.pool.get_connection() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO task_states (task_id, state_json, updated_at) VALUES (?, ?, ?)",
                    (task_id, json.dumps(payload, ensure_ascii=False), time.time())
                )
                conn.commit()
            return True

        except Exception as e:
            print(f"[{task_id}] 持久化任务状态失败: {e}")
            return False

    def load_task_state(self, task_id: str) -> Optional[Dict[str, Any]]:
        """加载单个任务状态"""
        try:
            with self.pool.get_connection() as conn:
                row = conn.execute(
                    "SELECT state_json FROM task_states WHERE task_id = ?",
                    (task_id,)
                ).fetchone()

                if row:
                    return json.loads(row['state_json'])
                return None

        except Exception as e:
            print(f"[{task_id}] 加载任务状态失败: {e}")
            return None

    def load_all_task_states(self) -> Dict[str, Dict[str, Any]]:
        """加载所有任务状态"""
        tasks = {}
        try:
            with self.pool.get_connection() as conn:
                rows = conn.execute(
                    "SELECT task_id, state_json FROM task_states ORDER BY updated_at DESC"
                ).fetchall()

                for row in rows:
                    try:
                        tasks[row['task_id']] = json.loads(row['state_json'])
                    except:
                        continue

        except Exception as e:
            print(f"加载任务状态失败: {e}")

        return tasks

    def delete_task_state(self, task_id: str) -> bool:
        """删除任务状态"""
        try:
            with self.pool.get_connection() as conn:
                conn.execute("DELETE FROM task_states WHERE task_id = ?", (task_id,))
                conn.commit()
            return True

        except Exception as e:
            print(f"[{task_id}] 删除任务状态失败: {e}")
            return False

    def count_task_states(self) -> int:
        """统计任务数量"""
        try:
            with self.pool.get_connection() as conn:
                row = conn.execute("SELECT COUNT(*) as cnt FROM task_states").fetchone()
                return row['cnt'] if row else 0

        except Exception as e:
            print(f"统计任务数量失败: {e}")
            return 0

    def query_task_history(
        self,
        status: str = "",
        query: str = "",
        page: int = 1,
        per_page: int = 30
    ) -> Dict[str, Any]:
        """
        查询任务历史（使用索引优化）

        Args:
            status: 状态过滤（空字符串表示全部）
            query: 文件名搜索关键词
            page: 页码（从1开始）
            per_page: 每页数量

        Returns:
            包含 tasks 和 total 的字典
        """
        try:
            with self.pool.get_connection() as conn:
                # 构建查询条件
                where_clauses = []
                params = []

                if status:
                    where_clauses.append("status = ?")
                    params.append(status)

                if query:
                    where_clauses.append("file_name LIKE ?")
                    params.append(f"%{query}%")

                where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

                # 查询总数
                total_row = conn.execute(
                    f"SELECT COUNT(*) as cnt FROM task_history WHERE {where_sql}",
                    params
                ).fetchone()
                total = total_row['cnt'] if total_row else 0

                # 分页查询（利用索引）
                offset = (page - 1) * per_page
                rows = conn.execute(
                    f"""
                    SELECT task_id, state_json, completed_at
                    FROM task_history
                    WHERE {where_sql}
                    ORDER BY completed_at DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [per_page, offset]
                ).fetchall()

                tasks = []
                for row in rows:
                    try:
                        task = json.loads(row['state_json'])
                        task['completed_at'] = row['completed_at']
                        tasks.append(task)
                    except:
                        continue

                return {"tasks": tasks, "total": total}

        except Exception as e:
            print(f"查询任务历史失败: {e}")
            return {"tasks": [], "total": 0}

    def save_to_history(self, task_id: str, state: Dict[str, Any]):
        """保存任务到历史记录（带状态提取以利用索引）"""
        try:
            with self.pool.get_connection() as conn:
                payload = dict(state or {})
                payload.pop("_lock", None)

                # 提取常用查询字段
                status = payload.get("status", "")
                file_name = payload.get("file_name", "")
                total_bytes = payload.get("total_bytes", 0)

                conn.execute(
                    """
                    INSERT OR REPLACE INTO task_history
                    (task_id, state_json, updated_at, completed_at, status, file_name, total_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        json.dumps(payload, ensure_ascii=False),
                        time.time(),
                        time.time() if status in ("completed", "failed", "cancelled") else None,
                        status,
                        file_name,
                        total_bytes
                    )
                )
                conn.commit()

        except Exception as e:
            print(f"[{task_id}] 保存历史记录失败: {e}")

    def cleanup_old_history(self, days: int = 30) -> int:
        """清理旧历史记录"""
        try:
            cutoff = time.time() - (days * 86400)
            with self.pool.get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM task_history WHERE completed_at < ?",
                    (cutoff,)
                )
                conn.commit()
                return cursor.rowcount

        except Exception as e:
            print(f"清理历史记录失败: {e}")
            return 0
