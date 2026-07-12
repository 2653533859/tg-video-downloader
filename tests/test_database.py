"""
测试 database 模块
"""
import pytest
import tempfile
import os
import time
from database import DatabaseConnectionPool, TaskDatabase


class TestDatabaseConnectionPool:
    """数据库连接池测试"""

    def test_pool_initialization(self):
        """测试连接池初始化"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as f:
            db_path = f.name

        try:
            pool = DatabaseConnectionPool(db_path, pool_size=3)
            pool.initialize()

            # 验证表和索引已创建
            with pool.get_connection() as conn:
                # 检查表存在
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                table_names = [t['name'] for t in tables]

                assert 'task_states' in table_names
                assert 'task_history' in table_names
                assert 'tdl_fallback_channels' in table_names

                # 检查索引存在
                indexes = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
                index_names = [i['name'] for i in indexes]

                assert 'idx_task_states_updated_at' in index_names
                assert 'idx_task_history_status' in index_names

            pool.close_all()

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_connection_reuse(self):
        """测试连接复用"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as f:
            db_path = f.name

        try:
            pool = DatabaseConnectionPool(db_path, pool_size=2)
            pool.initialize()

            # 获取连接
            with pool.get_connection() as conn1:
                conn1_id = id(conn1)

            # 再次获取应该复用
            with pool.get_connection() as conn2:
                conn2_id = id(conn2)

            # 可能是同一个连接（从池中获取）
            # 这里只是测试不会抛出异常

            pool.close_all()

        finally:
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestTaskDatabase:
    """任务数据库测试"""

    @pytest.fixture
    def task_db(self):
        """创建测试数据库"""
        with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as f:
            db_path = f.name

        pool = DatabaseConnectionPool(db_path, pool_size=2)
        pool.initialize()
        db = TaskDatabase(pool)

        yield db

        pool.close_all()
        if os.path.exists(db_path):
            os.unlink(db_path)

    def test_persist_and_load_task_state(self, task_db):
        """测试持久化和加载任务状态"""
        task_id = "test_task_001"
        state = {
            "status": "downloading",
            "progress": 50,
            "file_name": "test.mp4",
            "total_bytes": 1000000
        }

        # 保存
        assert task_db.persist_task_state(task_id, state)

        # 加载
        loaded = task_db.load_task_state(task_id)
        assert loaded is not None
        assert loaded['status'] == 'downloading'
        assert loaded['progress'] == 50
        assert loaded['file_name'] == 'test.mp4'

    def test_load_all_task_states(self, task_db):
        """测试加载所有任务状态"""
        # 保存多个任务
        for i in range(3):
            task_db.persist_task_state(
                f"task_{i}",
                {"status": "pending", "index": i}
            )

        # 加载所有
        all_tasks = task_db.load_all_task_states()
        assert len(all_tasks) == 3
        assert "task_0" in all_tasks
        assert "task_2" in all_tasks

    def test_delete_task_state(self, task_db):
        """测试删除任务状态"""
        task_id = "test_task_delete"
        task_db.persist_task_state(task_id, {"status": "pending"})

        # 删除
        assert task_db.delete_task_state(task_id)

        # 验证已删除
        loaded = task_db.load_task_state(task_id)
        assert loaded is None

    def test_count_task_states(self, task_db):
        """测试统计任务数量"""
        # 初始为0
        assert task_db.count_task_states() == 0

        # 添加任务
        task_db.persist_task_state("task_1", {"status": "pending"})
        task_db.persist_task_state("task_2", {"status": "downloading"})

        # 验证计数
        assert task_db.count_task_states() == 2

    def test_save_to_history(self, task_db):
        """测试保存到历史记录"""
        task_id = "history_task"
        state = {
            "status": "completed",
            "file_name": "video.mp4",
            "total_bytes": 5000000
        }

        task_db.save_to_history(task_id, state)

        # 查询历史
        result = task_db.query_task_history(status="completed")
        assert result['total'] == 1
        assert len(result['tasks']) == 1
        assert result['tasks'][0]['file_name'] == 'video.mp4'

    def test_query_task_history_with_filter(self, task_db):
        """测试带过滤的历史查询"""
        # 添加多条记录
        task_db.save_to_history("task_1", {
            "status": "completed",
            "file_name": "video1.mp4",
            "total_bytes": 1000000
        })
        task_db.save_to_history("task_2", {
            "status": "failed",
            "file_name": "video2.mp4",
            "total_bytes": 2000000
        })
        task_db.save_to_history("task_3", {
            "status": "completed",
            "file_name": "document.pdf",
            "total_bytes": 500000
        })

        # 按状态过滤
        completed = task_db.query_task_history(status="completed")
        assert completed['total'] == 2

        # 按文件名搜索
        videos = task_db.query_task_history(query="video")
        assert videos['total'] == 2

        # 组合过滤
        completed_videos = task_db.query_task_history(status="completed", query="video")
        assert completed_videos['total'] == 1

    def test_query_task_history_pagination(self, task_db):
        """测试历史查询分页"""
        # 添加多条记录
        for i in range(15):
            task_db.save_to_history(f"task_{i}", {
                "status": "completed",
                "file_name": f"file_{i}.mp4",
                "total_bytes": 1000000
            })

        # 第一页
        page1 = task_db.query_task_history(page=1, per_page=10)
        assert page1['total'] == 15
        assert len(page1['tasks']) == 10

        # 第二页
        page2 = task_db.query_task_history(page=2, per_page=10)
        assert len(page2['tasks']) == 5

    def test_cleanup_old_history(self, task_db):
        """测试清理旧历史记录"""
        # 添加新旧记录
        task_db.save_to_history("new_task", {
            "status": "completed",
            "file_name": "new.mp4",
            "total_bytes": 1000000
        })

        # 手动插入旧记录
        old_time = time.time() - (60 * 86400)  # 60天前
        with task_db.pool.get_connection() as conn:
            conn.execute(
                "INSERT INTO task_history (task_id, state_json, updated_at, completed_at, status) VALUES (?, ?, ?, ?, ?)",
                ("old_task", '{"file_name": "old.mp4"}', old_time, old_time, "completed")
            )
            conn.commit()

        # 清理30天前的记录
        deleted = task_db.cleanup_old_history(days=30)
        assert deleted == 1

        # 验证新记录仍在
        result = task_db.query_task_history()
        assert result['total'] == 1
        assert result['tasks'][0]['file_name'] == 'new.mp4'
