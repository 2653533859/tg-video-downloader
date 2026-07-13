"""
测试新增的核心模块（src/ 包）
覆盖：utils、validators、state、queue、async_manager、health_checker、helpers、routes
"""
import os
import sys
import asyncio
import tempfile
from unittest.mock import AsyncMock, Mock

import pytest


# ==================== 工具函数 ====================
class TestFormatting:
    def test_format_size(self):
        from src.utils import format_size
        assert format_size(0) == "0B"
        assert format_size(1024) == "1.00KB"
        assert format_size(1024 * 1024) == "1.00MB"
        assert format_size(1024 * 1024 * 1024) == "1.00GB"

    def test_format_speed(self):
        from src.utils import format_speed
        assert format_speed(0) == "0B/s"
        assert format_speed(1024) == "1.00KB/s"


class TestValidators:
    def test_validate_task_id(self):
        from src.utils.validators import validate_task_id
        assert validate_task_id("123_456") is True
        assert validate_task_id("abc-DEF_9") is True
        assert validate_task_id("") is False
        assert validate_task_id(None) is False
        assert validate_task_id("bad/id") is False

    def test_validate_entity_id(self):
        from src.utils.validators import validate_entity_id
        assert validate_entity_id(-1001234567890) is True
        assert validate_entity_id(0) is False

    def test_validate_message_id(self):
        from src.utils.validators import validate_message_id
        assert validate_message_id(123) is True
        assert validate_message_id(0) is False
        assert validate_message_id(-5) is False

    def test_is_valid_path(self):
        from src.utils.validators import is_valid_path
        with tempfile.TemporaryDirectory() as base:
            assert is_valid_path(os.path.join(base, "file.mp4"), base) is True
            assert is_valid_path(os.path.join(base, "../etc/passwd"), base) is False

    def test_sanitize_path_component(self):
        from src.utils.validators import sanitize_path_component
        assert sanitize_path_component("file<>name.mp4") == "file__name.mp4"
        assert sanitize_path_component("") == "unnamed"


# ==================== 状态管理 ====================
class TestStateManager:
    def test_lifecycle(self):
        from src.state.manager import TaskStateManager
        m = TaskStateManager()
        m.set_state("t1", {"status": "downloading", "progress": 50})
        assert m.get_state("t1")["progress"] == 50

        m.update_state("t1", progress=75)
        assert m.get_state("t1")["progress"] == 75

        assert "t1" in m.get_all_states()

        m.mark_cancelled("t1")
        assert m.is_cancelled("t1") is True

        m.remove_state("t1")
        assert m.get_state("t1") is None

    def test_stats(self):
        from src.state.manager import TaskStateManager
        m = TaskStateManager()
        m.set_state("a", {"status": "downloading"})
        m.set_state("b", {"status": "done"})
        stats = m.get_stats()
        assert stats["total"] == 2


class TestTaskStatePersistence:
    def test_persist_load_and_query_history(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(
                state_dir=state_dir,
                terminal_states={"done", "error", "cancelled"},
            )
            store.persist_state("t1", {"status": "done", "filename": "video.mp4", "finish_time": 123})
            store.persist_state("t2", {"status": "downloading", "filename": "active.mp4"})

            states, loaded = store.load_states()
            assert loaded == 2
            assert states["t1"]["status"] == "done"
            assert states["t2"]["status"] == "error"

            items, total = store.query_history([], status="done", query="video", page=1, per_page=10)
            assert total == 1
            assert items[0]["task_id"] == "t1"
            store.close()

    def test_tdl_fallback_cache(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(
                state_dir=state_dir,
                terminal_states={"done", "error", "cancelled"},
            )
            assert store.has_tdl_fallback_channel(-100123) is False
            store.remember_tdl_fallback_channel(-100123, "failed")
            assert store.has_tdl_fallback_channel(-100123) is True
            store.close()

    def test_enabled_flag_no_test_awareness(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            # 默认启用（不再感知 unittest 模块）；显式关闭时所有写入为 no-op
            enabled_store = TaskStatePersistence(state_dir=state_dir, terminal_states={"done"})
            assert enabled_store.enabled() is True
            enabled_store.close()

            disabled_store = TaskStatePersistence(
                state_dir=state_dir, terminal_states={"done"}, enabled=False
            )
            assert disabled_store.enabled() is False
            disabled_store.persist_state("x", {"status": "done"})
            assert disabled_store.load_states() == ({}, 0)

    def test_persist_throttle_skips_same_status(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(
                state_dir=state_dir,
                terminal_states={"done", "error", "cancelled"},
                persist_throttle_seconds=60,
            )
            # 首次写入落库
            store.persist_state("t1", {"status": "downloading", "progress": 1})
            # 同状态窗口内的进度更新被节流跳过（DB 仍是第一版）
            store.persist_state("t1", {"status": "downloading", "progress": 2})
            with store.connect() as conn:
                row = conn.execute("SELECT state_json FROM task_states WHERE task_id='t1'").fetchone()
            assert '"progress": 1' in row[0]
            # 状态切换到终态必定写入，且进入 history
            store.persist_state("t1", {"status": "done", "progress": 3, "finish_time": 9})
            with store.connect() as conn:
                row = conn.execute("SELECT state_json FROM task_states WHERE task_id='t1'").fetchone()
                hist = conn.execute("SELECT COUNT(*) FROM task_history WHERE task_id='t1'").fetchone()
            assert '"progress": 3' in row[0]
            assert hist[0] == 1
            store.close()

    def test_throttle_only_applies_to_downloading(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(
                state_dir=state_dir,
                terminal_states={"done", "error", "cancelled"},
                persist_throttle_seconds=60,
            )
            # queued 属非高频状态：同状态的字段变化不应被节流，必须落库最新值
            store.persist_state("t1", {"status": "queued", "queue_position": 1})
            store.persist_state("t1", {"status": "queued", "queue_position": 5})
            with store.connect() as conn:
                row = conn.execute("SELECT state_json FROM task_states WHERE task_id='t1'").fetchone()
            assert '"queue_position": 5' in row[0]
            store.close()

    def test_closed_persistence_is_noop_and_does_not_reopen(self):
        from src.state.persistence import TaskStatePersistence

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(state_dir=state_dir, terminal_states={"done"})
            store.persist_state("t1", {"status": "done", "finish_time": 1})
            store.close()
            # 关闭后：迟到写入静默 no-op，且不得重开连接（防泄漏）
            store.persist_state("t2", {"status": "done", "finish_time": 2})
            assert store._conn is None
            assert store.backup_database() is None

    def test_schema_version_and_backup_integrity(self):
        from src.state.persistence import TaskStatePersistence, SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as state_dir:
            store = TaskStatePersistence(state_dir=state_dir, terminal_states={"done"})
            store.persist_state("t1", {"status": "done", "finish_time": 1})
            with store.connect() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            assert version == SCHEMA_VERSION
            backup_path = store.backup_database()
            assert backup_path is not None
            assert os.path.exists(backup_path)
            store.close()



# ==================== 下载队列 ====================
class TestDownloadQueue:
    def test_initial_status(self):
        from src.download.queue import DownloadQueue
        q = DownloadQueue(max_concurrent=2)
        status = q.get_status()
        assert status["max_concurrent"] == 2
        assert status["queue_length"] == 0
        assert status["active_downloads"] == 0

    def test_add_and_remove(self):
        from src.download.queue import DownloadQueue
        q = DownloadQueue()
        assert q.add_task({"task_id": "t1"}) is True
        assert q.add_task({"task_id": "t1"}) is False  # 重复
        assert q.is_task_queued("t1") is True
        assert q.remove_task("t1") is True
        assert q.is_task_queued("t1") is False

    def test_get_next(self):
        from src.download.queue import DownloadQueue
        q = DownloadQueue(max_concurrent=1)
        q.add_task({"task_id": "t1"})
        q.add_task({"task_id": "t2"})
        first = q.get_next_task()
        assert first["task_id"] == "t1"
        # 已达并发上限，第二个取不出
        assert q.get_next_task() is None


class TestDownloadScheduler:
    def test_queue_lifecycle(self):
        from src.download.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_concurrent=1)
        updates = []

        def update_positions():
            scheduler.update_positions(lambda task_id, pos, size: updates.append((task_id, pos, size)))

        assert scheduler.add_task({"task_id": "t1"}, update_positions=update_positions) is True
        assert scheduler.add_task({"task_id": "t2"}, update_positions=update_positions) is True
        assert scheduler.add_task({"task_id": "t2"}, update_positions=update_positions) is False

        assert scheduler.get_status() == {"active": 0, "queued": 2, "max": 1}
        assert scheduler.move_task("t2", "top", update_positions=update_positions) is True

        task = scheduler.get_next_task(update_positions=update_positions)
        assert task["task_id"] == "t2"
        assert scheduler.get_next_task() is None

        scheduler.release_tasks([task])
        assert scheduler.get_status() == {"active": 0, "queued": 1, "max": 1}
        assert ("t1", 1, 1) in updates

    def test_remove_task(self):
        from src.download.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_concurrent=1)
        scheduler.add_task({"task_id": "t1"})
        assert scheduler.remove_task("t1") is True
        assert scheduler.remove_task("t1") is False
        assert scheduler.get_status()["queued"] == 0

    def test_generation_token_assigned(self):
        from src.download.scheduler import DownloadScheduler, GENERATION_KEY

        scheduler = DownloadScheduler(max_concurrent=1)
        scheduler.add_task({"task_id": "t1"})
        task = scheduler.get_next_task()
        assert task[GENERATION_KEY] >= 1
        assert scheduler.get_status()["active"] == 1

    def test_double_release_is_idempotent(self):
        # 同一占槽被重复 release 只减一次，杜绝槽位超发
        from src.download.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_concurrent=2)
        scheduler.add_task({"task_id": "t1"})
        task = scheduler.get_next_task()
        assert scheduler.get_status()["active"] == 1

        scheduler.release_tasks([task])
        scheduler.release_tasks([task])  # 陈旧的第二次释放
        assert scheduler.get_status()["active"] == 0

    def test_watchdog_revoke_then_stale_release(self):
        # watchdog 撤销当代令牌还槽后，原 worker 携旧令牌 release 应被忽略
        from src.download.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_concurrent=1)
        scheduler.add_task({"task_id": "t1"})
        task_gen1 = scheduler.get_next_task()
        assert scheduler.get_status()["active"] == 1

        # watchdog 撤销：立即还槽
        assert scheduler.release_scheduled_task("t1") is True
        assert scheduler.get_status()["active"] == 0

        # 任务重新入队并再次占槽（新一代令牌）
        scheduler.add_task({"task_id": "t1"})
        task_gen2 = scheduler.get_next_task()
        assert task_gen2 is not None
        assert scheduler.get_status()["active"] == 1

        # 原 worker 迟到的释放：携带旧令牌，必须被忽略，不能误还新一代的槽
        scheduler.release_tasks([task_gen1])
        assert scheduler.get_status()["active"] == 1

        # 新 worker 正常释放
        scheduler.release_tasks([task_gen2])
        assert scheduler.get_status()["active"] == 0

    def test_release_scheduled_removes_queued_task(self):
        # 撤销仍在排队（未占槽）的任务：从队列移除，不影响 active 计数
        from src.download.scheduler import DownloadScheduler

        scheduler = DownloadScheduler(max_concurrent=1)
        scheduler.add_task({"task_id": "t1"})
        scheduler.add_task({"task_id": "t2"})
        scheduler.get_next_task()  # t1 占槽
        assert scheduler.release_scheduled_task("t2") is True  # t2 仍排队
        assert scheduler.get_status() == {"active": 1, "queued": 0, "max": 1}


class TestDownloadManager:
    def test_enqueue_selects_telegram(self):
        from src.download.manager import DownloadManager

        queued = []
        processed = []
        manager = DownloadManager(
            tdl_binary="/missing/tdl",
            supports_tdl_download=lambda _entity_id: False,
            add_to_queue=queued.append,
            process_queue=lambda: processed.append(True),
        )

        assert manager.enqueue("t1", 123, 4, "chat", {"filename": "v.mp4"}) == "t1"
        assert queued[0]["downloader"] == "telegram"
        assert processed == [True]

    def test_enqueue_selects_tdl_when_available(self):
        from src.download.manager import DownloadManager

        queued = []
        with tempfile.NamedTemporaryFile() as binary:
            manager = DownloadManager(
                tdl_binary=binary.name,
                supports_tdl_download=lambda _entity_id: True,
                add_to_queue=queued.append,
                process_queue=lambda: None,
            )

            assert manager.enqueue("t1", -100123, 4, "chat", None) == "t1"
            assert queued[0]["downloader"] == "tdl"

    def test_tdl_missing_raises(self):
        from src.download.manager import DownloadManager

        manager = DownloadManager(
            tdl_binary="/missing/tdl",
            supports_tdl_download=lambda _entity_id: True,
            add_to_queue=lambda _task: None,
            process_queue=lambda: None,
        )

        with pytest.raises(RuntimeError):
            manager.enqueue("t1", -100123, 4, "chat", None)


class TestDownloadWorker:
    def make_worker(self, **overrides):
        from src.download.worker import DownloadWorker

        states = {}
        resumes = {}
        calls = {"released": [], "processed": 0, "tdl": 0}

        defaults = {
            "download_dir_for_dialog": lambda _dialog: tempfile.mkdtemp(),
            "release_tasks": lambda tasks: calls["released"].append(list(tasks)),
            "process_queue": lambda: calls.__setitem__("processed", calls["processed"] + 1),
            "copy_task_state": lambda task_id: states.get(task_id, {}).copy(),
            "set_task_state": lambda task_id, state: states.__setitem__(task_id, dict(state)),
            "update_task_state": lambda task_id, **updates: states.setdefault(task_id, {}).update(updates),
            "is_cancelled": lambda _task_id: False,
            "get_cached_message": lambda *_args: None,
            "resolve_message": lambda *_args: None,
            "get_video_info": lambda _message: None,
            "supports_tdl_download": lambda _entity_id: False,
            "download_with_telegram": lambda *_args: None,
            "tdl_executor": lambda: Mock(download=lambda *_args: calls.__setitem__("tdl", calls["tdl"] + 1)),
            "save_resume_info": lambda task_id, info: resumes.__setitem__(task_id, info),
            "format_size": lambda size: f"{int(size)}B",
            "log_info": lambda _msg: None,
            "log_error": lambda _msg: None,
        }
        defaults.update(overrides)
        return DownloadWorker(**defaults), states, resumes, calls

    def test_no_video_marks_error_and_releases(self):
        worker, states, _resumes, calls = self.make_worker()

        worker.run([{"task_id": "t1", "entity_id": 1, "msg_id": 2}], "chat")

        assert states["t1"]["status"] == "error"
        assert states["t1"]["error"] == "消息不包含可下载视频"
        assert calls["released"]
        assert calls["processed"] == 1

    def test_existing_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as base:
            file_path = os.path.join(base, "video.mp4")
            with open(file_path, "wb") as handle:
                handle.write(b"abc")

            worker, states, _resumes, _calls = self.make_worker(
                download_dir_for_dialog=lambda _dialog: base,
            )
            worker.run([
                {
                    "task_id": "t1",
                    "entity_id": 1,
                    "msg_id": 2,
                    "info": {"filename": "video.mp4", "size": 3, "document_id": "doc"},
                }
            ], "chat")

            assert states["t1"]["status"] == "skipped"
            assert states["t1"]["progress"] == 100

    def test_telegram_failure_saves_resume(self):
        with tempfile.TemporaryDirectory() as base:
            file_path = os.path.join(base, "video.mp4")
            with open(file_path, "wb") as handle:
                handle.write(b"partial")

            def fail_download(*_args):
                raise RuntimeError("network")

            worker, states, resumes, _calls = self.make_worker(
                download_dir_for_dialog=lambda _dialog: base,
                download_with_telegram=fail_download,
            )
            worker.run([
                {
                    "task_id": "t1",
                    "entity_id": 1,
                    "msg_id": 2,
                    "info": {"filename": "video.mp4", "size": 10, "document_id": "doc"},
                    "downloader": "telegram",
                }
            ], "chat")

            assert states["t1"]["status"] == "error"
            assert resumes["t1"]["offset"] == len(b"partial")


class TestTelegramDirectDownloader:
    def test_download_writes_file_and_marks_done(self):
        import asyncio
        from src.download.telegram_downloader import TelegramDirectDownloader

        class FakeClient:
            async def iter_download(self, *_args, **_kwargs):
                yield b"abc"
                yield b"def"

        async def next_chunk(iterator, timeout=60):
            return await iterator.__anext__()

        message = Mock()
        message.media.document = object()
        states = {}
        resumes = {}

        def set_state(task_id, state):
            states[task_id] = dict(state)

        def update_state(task_id, **updates):
            states.setdefault(task_id, {}).update(updates)

        downloader = TelegramDirectDownloader(
            tg_client=FakeClient(),
            ensure_connection=lambda allow_reconnect=True: True,
            run_async=lambda factory, **_kwargs: asyncio.run(factory()),
            resolve_message=lambda *_args, **_kwargs: message,
            next_chunk=next_chunk,
            detect_resume_offset=lambda *_args, **_kwargs: 0,
            save_resume_info=lambda task_id, info: resumes.__setitem__(task_id, info),
            clear_resume_info=lambda task_id: resumes.pop(task_id, None),
            set_task_state=set_state,
            update_task_state=update_state,
            is_cancelled=lambda _task_id: False,
            should_retry_error=lambda _exc: False,
            validate_completion=lambda **_kwargs: None,
            calc_timeout=lambda _size: 30,
            format_size=lambda size: f"{int(size)}B",
            log_info=lambda _msg: None,
            log_warning=lambda _msg: None,
            max_retry_attempts=1,
            chunk_timeout=60,
        )

        with tempfile.TemporaryDirectory() as base:
            filepath = os.path.join(base, "video.mp4")
            downloader.download(
                "t1",
                -100123,
                42,
                "chat",
                {"filename": "video.mp4", "size": 6, "document_id": "doc"},
                filepath,
            )

            with open(filepath, "rb") as handle:
                assert handle.read() == b"abcdef"
            assert states["t1"]["status"] == "done"
            assert states["t1"]["progress"] == 100
            assert states["t1"]["final_bytes"] == 6
            assert "t1" not in resumes


class TestTdlRuntime:
    def test_url_support_and_command(self):
        from src.download.tdl import TdlRuntime

        runtime = TdlRuntime(
            binary="/bin/tdl",
            namespace="ns",
            storage_path="/tmp/tdl",
            threads=8,
            limit=4,
            proxy_config=("socks5", "127.0.0.1", 7890),
        )

        assert runtime.build_message_url(-1001234567890, 42) == "https://t.me/c/1234567890/42"
        assert runtime.supports_download(-1001234567890) is True
        assert runtime.supports_download(123) is False

        command = runtime.build_download_command("https://t.me/c/1/2", "/downloads", "video.mp4")
        assert command[:2] == ["/bin/tdl", "download"]
        assert "--proxy" in command
        assert "socks5://127.0.0.1:7890" in command

    def test_process_and_error_state(self):
        from src.download.tdl import TdlRuntime

        process = Mock()
        process.poll.return_value = None
        runtime = TdlRuntime(
            binary="/missing/tdl",
            namespace="ns",
            storage_path="/tmp/tdl",
            threads=8,
            limit=4,
        )

        runtime.register_process("t1", process)
        runtime.set_error("t1", "boom")

        status = runtime.status()
        assert status["active"] == 1
        assert status["error"] == "boom"
        assert runtime.get_process("t1") is process
        assert runtime.last_error("t1") == "boom"

        assert runtime.drop_process("t1") is process
        runtime.clear_error("t1")
        assert runtime.last_error("t1") == ""


class TestTdlDownloadExecutor:
    def test_message_url_error_marks_task_error(self):
        from src.download.tdl_executor import TdlDownloadExecutor

        updates = {}

        def update_state(task_id, **kwargs):
            updates.setdefault(task_id, {}).update(kwargs)

        executor = TdlDownloadExecutor(
            build_message_url=lambda *_args: (_ for _ in ()).throw(ValueError("bad url")),
            build_command=lambda *_args: [],
            clear_tdl_error=lambda _task_id: None,
            register_process=lambda *_args: None,
            drop_process=lambda _task_id: None,
            get_process=lambda _task_id: None,
            set_tdl_error=lambda *_args: None,
            last_tdl_error=lambda _task_id: "",
            stop_process=lambda _process: None,
            detect_resume_offset=lambda *_args: 0,
            resolve_progress_path=lambda path: path,
            prepare_telegram_fallback_target=lambda path: path,
            save_resume_info=lambda *_args: None,
            clear_resume_info=lambda _task_id: None,
            update_task_state=update_state,
            set_task_state=lambda *_args: None,
            copy_task_state=lambda _task_id: {},
            is_cancelled=lambda _task_id: False,
            should_capture_error_line=lambda _line: False,
            choose_more_specific_error=lambda current, _candidate: current,
            reconcile_progress_size=lambda current_size, _written, allow_offset_correction: (current_size, allow_offset_correction),
            did_restart_from_scratch=lambda **_kwargs: False,
            should_retry_error=lambda *_args, **_kwargs: False,
            should_fallback=lambda _err: False,
            remember_fallback_channel=lambda *_args: None,
            validate_completion=lambda **_kwargs: None,
            download_with_telegram=lambda *_args: None,
            format_size=lambda size: f"{int(size)}B",
            log_info=lambda _msg: None,
            log_warning=lambda _msg: None,
            log_error=lambda _msg: None,
            restart_reset_min_bytes=64,
        )

        executor.download("t1", 123, 4, "chat", {"filename": "v.mp4", "size": 1}, "/tmp/v.mp4", "/tmp")
        assert updates["t1"]["status"] == "error"
        assert updates["t1"]["error"] == "bad url"

    def test_download_acquires_resource_lock(self):
        from src.download.tdl_executor import TdlDownloadExecutor

        events = []

        class TrackLock:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, *_args):
                events.append("exit")
                return False

        executor = TdlDownloadExecutor(
            build_message_url=lambda *_args: "https://t.me/c/1/1",
            build_command=lambda *_args: ["true"],
            clear_tdl_error=lambda _task_id: None,
            register_process=lambda *_args: None,
            drop_process=lambda _task_id: None,
            get_process=lambda _task_id: None,
            set_tdl_error=lambda *_args: None,
            last_tdl_error=lambda _task_id: "",
            stop_process=lambda _process: None,
            detect_resume_offset=lambda *_args: 0,
            resolve_progress_path=lambda path: path,
            prepare_telegram_fallback_target=lambda path: path,
            save_resume_info=lambda *_args: None,
            clear_resume_info=lambda _task_id: None,
            update_task_state=lambda *_args, **_kwargs: None,
            set_task_state=lambda *_args: None,
            copy_task_state=lambda _task_id: {},
            is_cancelled=lambda _task_id: False,
            should_capture_error_line=lambda _line: False,
            choose_more_specific_error=lambda current, _candidate: current,
            reconcile_progress_size=lambda current_size, _written, allow: (current_size, allow),
            did_restart_from_scratch=lambda **_kwargs: False,
            should_retry_error=lambda *_args, **_kwargs: False,
            should_fallback=lambda _err: False,
            remember_fallback_channel=lambda *_args: None,
            validate_completion=lambda **_kwargs: None,
            download_with_telegram=lambda *_args: None,
            format_size=lambda size: f"{int(size)}B",
            log_info=lambda _msg: None,
            log_warning=lambda _msg: None,
            log_error=lambda _msg: None,
            restart_reset_min_bytes=64,
            resource_lock=TrackLock(),
        )
        # 跳过真实子进程，仅验证 _run_once 被资源锁包裹
        executor._run_once = lambda *_args, **_kwargs: (0, 0)
        executor.download("t1", 123, 4, "chat", {"filename": "v.mp4", "size": 1}, "/tmp/v.mp4", "/tmp")
        assert events == ["enter", "exit"]


class TestDownloadWorkerPool:
    def test_submit_runs_worker_lazy_start(self):
        import threading as _th
        from src.download.worker_pool import DownloadWorkerPool

        done = _th.Event()
        received = {}

        def worker(items, name):
            received["items"] = items
            received["name"] = name
            done.set()

        pool = DownloadWorkerPool(1, worker)
        assert pool._started is False  # 懒启动：未提交前不建线程
        pool.submit(["t1"], "chat")
        assert done.wait(2) is True
        assert received == {"items": ["t1"], "name": "chat"}
        assert pool._started is True
        pool.stop()

    def test_worker_exception_does_not_kill_thread(self):
        import threading as _th
        from src.download.worker_pool import DownloadWorkerPool

        first = _th.Event()
        second = _th.Event()

        def worker(items, _name):
            if items == "boom":
                first.set()
                raise RuntimeError("x")
            second.set()

        pool = DownloadWorkerPool(1, worker)
        pool.submit("boom", "c")
        assert first.wait(2)
        pool.submit("ok", "c")
        # worker 抛异常后线程不退出，仍能处理后续任务
        assert second.wait(2)
        pool.stop()


class TestTdlRules:
    def test_error_classification_and_retry(self):
        from src.download import tdl_rules

        assert tdl_rules.classify_tdl_error("unexpected EOF") == "eof"
        assert tdl_rules.classify_tdl_error("proxy connection reset") == "network"
        assert tdl_rules.classify_tdl_error("i/o timeout") == "timeout"
        assert tdl_rules.classify_tdl_error("CHAT_ID_INVALID") == "fatal"

        assert tdl_rules.should_retry_tdl_error(
            "unexpected EOF",
            0,
            max_eof_retries=3,
            max_retry_attempts=5,
            max_stalled_eof_retries=2,
        ) is True
        assert tdl_rules.should_retry_tdl_error(
            "unexpected EOF",
            3,
            max_eof_retries=3,
            max_retry_attempts=5,
            max_stalled_eof_retries=2,
        ) is False

    def test_progress_and_completion_rules(self):
        from src.download import tdl_rules

        assert tdl_rules.should_capture_tdl_error_line("error: eof") is True
        assert tdl_rules.should_capture_tdl_error_line("CPU: 10%") is False
        assert tdl_rules.reconcile_tdl_progress_size(5, 10, True) == (5, False)
        assert tdl_rules.reconcile_tdl_progress_size(5, 10, False) == (10, False)

        assert tdl_rules.did_tdl_restart_from_scratch(
            1,
            100,
            10,
            restart_reset_min_bytes=64,
        ) is True
        assert tdl_rules.validate_tdl_completion(10, 9, lambda size: f"{size}B") == "下载不完整：期望 10B，实际 9B"
        assert tdl_rules.choose_more_specific_tdl_error("fatal", "unexpected EOF") == "unexpected EOF"


class TestDownloadPathHelpers:
    def test_dialog_paths_and_progress_selection(self):
        from src.download.paths import (
            download_dir_for_dialog,
            resolve_tdl_progress_path,
            sanitize_dialog_name,
        )

        assert sanitize_dialog_name("bad/name?") == "bad_name_"
        assert download_dir_for_dialog("/downloads", "chat/name") == os.path.join("/downloads", "chat_name")

        with tempfile.TemporaryDirectory() as base:
            final_path = os.path.join(base, "video.mp4")
            tmp_path = final_path + ".tmp"
            with open(final_path, "wb") as handle:
                handle.write(b"12345")
            with open(tmp_path, "wb") as handle:
                handle.write(b"12")
            assert resolve_tdl_progress_path(final_path) == final_path

    def test_prepare_telegram_fallback_target(self):
        from src.download.paths import prepare_telegram_fallback_target

        with tempfile.TemporaryDirectory() as base:
            final_path = os.path.join(base, "video.mp4")
            tmp_path = final_path + ".tmp"
            with open(tmp_path, "wb") as handle:
                handle.write(b"partial")
            assert prepare_telegram_fallback_target(final_path) == final_path
            assert os.path.exists(final_path)
            assert not os.path.exists(tmp_path)


class TestResumeStore:
    def test_save_load_clear_and_detect_offset(self):
        from src.download.resume import ResumeStore

        with tempfile.TemporaryDirectory() as base:
            resume_dir = os.path.join(base, "resume")
            store = ResumeStore(resume_dir, progress_path_func=lambda path: path + ".tmp")
            file_path = os.path.join(base, "video.mp4")

            store.save("t1", {"offset": 5, "filename": "video.mp4"})
            assert store.load("t1")["offset"] == 5
            assert store.list_task_ids() == ["t1"]
            assert store.count() == 1
            assert store.detect_offset("t1", file_path, total_bytes=10) == 5

            with open(file_path + ".tmp", "wb") as handle:
                handle.write(b"1234567")
            assert store.detect_offset("t1", file_path, total_bytes=10) == 7

            store.clear("t1")
            assert store.load("t1") is None


class TestRelayRange:
    def test_parse_range(self):
        from src.relay import parse_range

        assert parse_range(None, 10, chunk_size=4) == (0, 3, 206)
        assert parse_range("bytes=2-", 10, chunk_size=4) == (2, 5, 206)
        assert parse_range("bytes=-3", 10, chunk_size=4) == (7, 9, 206)
        assert parse_range("bytes=2-8", 10, chunk_size=4) == (2, 8, 206)

        with pytest.raises(ValueError):
            parse_range("bytes=20-", 10)


class TestFileService:
    def test_list_download_files_and_resolve_path(self):
        from src.files import list_download_files, resolve_download_path, resolve_file_path

        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "chat"))
            file_path = os.path.join(base, "chat", "video.mp4")
            with open(file_path, "wb") as handle:
                handle.write(b"abc")

            payload = list_download_files(base, lambda size: f"{size}B", page=1, per_page=10)
            assert payload["total"] == 1
            assert payload["files"][0]["folder"] == "chat"
            assert payload["files"][0]["filename"] == "video.mp4"
            assert payload["files"][0]["size"] == "3B"

            assert resolve_file_path(base, "chat/video.mp4") == os.path.realpath(file_path)
            assert resolve_download_path(base, "chat", "video.mp4") == os.path.realpath(file_path)
            with pytest.raises(ValueError):
                resolve_file_path(base, "../outside.mp4")
            with pytest.raises(ValueError):
                resolve_download_path(base, "../outside.mp4")
            with pytest.raises(FileNotFoundError):
                resolve_file_path(base, "missing.mp4")
            with pytest.raises(FileNotFoundError):
                resolve_download_path(base, "missing.mp4", must_exist=True)

    def test_local_stream_range_and_chunks(self):
        from src.files import iter_file_chunks, local_stream_range

        assert local_stream_range(10, None) is None
        assert local_stream_range(10, "bytes=2-", chunk_size=4) == {
            "start": 2,
            "end": 6,
            "content_length": 4,
            "content_range": "bytes 2-5/10",
        }

        with pytest.raises(ValueError):
            local_stream_range(10, "items=2-")

        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "file.bin")
            with open(path, "wb") as handle:
                handle.write(b"abcdef")
            assert b"".join(iter_file_chunks(path, 2, 3, chunk_size=2)) == b"cde"

    def test_thumbnail_cache_helpers(self):
        from src.files import cleanup_thumbnail_cache, thumbnail_cache_path, write_thumbnail

        with tempfile.TemporaryDirectory() as base:
            old_path = os.path.join(base, "old.jpg")
            large_path = os.path.join(base, "large.jpg")
            keep_path = os.path.join(base, "keep.jpg")

            with open(old_path, "wb") as handle:
                handle.write(b"old")
            with open(large_path, "wb") as handle:
                handle.write(b"12345")
            with open(keep_path, "wb") as handle:
                handle.write(b"12")

            now = 1_000_000
            os.utime(old_path, (now - 200, now - 200))
            os.utime(large_path, (now - 20, now - 20))
            os.utime(keep_path, (now - 10, now - 10))

            result = cleanup_thumbnail_cache(base, max_age_seconds=100, max_bytes=4, now=now)

            assert result == {"bytes": 2, "removed": 2}
            assert not os.path.exists(old_path)
            assert not os.path.exists(large_path)
            assert os.path.exists(keep_path)

            target = write_thumbnail(base, -100123, 42, b"jpeg")
            assert target == thumbnail_cache_path(base, -100123, 42)
            with open(target, "rb") as handle:
                assert handle.read() == b"jpeg"
            assert thumbnail_cache_path(base, None, 7).endswith("unknown_7.jpg")

    def test_open_folder_decisions(self):
        from src.files import prepare_open_folder

        with tempfile.TemporaryDirectory() as base:
            folder_path = os.path.realpath(os.path.join(base, "chat"))
            os.makedirs(folder_path)

            def resolve_path(folder, must_exist=False):
                path = os.path.realpath(os.path.join(base, folder))
                if must_exist and not os.path.exists(path):
                    raise FileNotFoundError
                return path

            payload, status = prepare_open_folder(resolve_path, "chat", False, True)
            assert status == 409
            assert payload["path"] == folder_path

            payload, status = prepare_open_folder(resolve_path, "chat", True, False)
            assert status == 409
            assert payload["path"] == folder_path

            opened = []
            payload, status = prepare_open_folder(resolve_path, "chat", True, True, opened.append)
            assert status == 200
            assert payload == {"ok": True, "path": folder_path}
            assert opened == [folder_path]

    def test_rename_and_delete_download_file(self):
        from src.files import delete_download_file, rename_download_file

        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "chat"))
            old_path = os.path.join(base, "chat", "old.mp4")
            with open(old_path, "wb") as handle:
                handle.write(b"abc")

            def resolve_path(*parts, must_exist=False):
                path = os.path.realpath(os.path.join(base, *parts))
                if os.path.commonpath([os.path.realpath(base), path]) != os.path.realpath(base):
                    raise ValueError("非法路径")
                if must_exist and not os.path.exists(path):
                    raise FileNotFoundError
                return path

            rename_download_file(resolve_path, "chat", "old.mp4", "new.mp4")
            new_path = os.path.join(base, "chat", "new.mp4")
            assert os.path.exists(new_path)

            with pytest.raises(ValueError):
                rename_download_file(resolve_path, "chat", "new.mp4", "../bad.mp4")

            with open(os.path.join(base, "chat", "taken.mp4"), "wb") as handle:
                handle.write(b"taken")
            with pytest.raises(FileExistsError):
                rename_download_file(resolve_path, "chat", "new.mp4", "taken.mp4")

            delete_download_file(resolve_path, "chat", "new.mp4")
            assert not os.path.exists(new_path)


class TestAccessControl:
    def test_local_ip_detection(self):
        from src.security import is_local_bind_only, request_ip_is_local

        assert is_local_bind_only("127.0.0.1") is True
        assert is_local_bind_only("0.0.0.0") is False
        assert request_ip_is_local("127.0.0.1") is True
        assert request_ip_is_local("10.0.0.1") is False
        # 默认不信任 X-Forwarded-For：伪造头不能把远程请求变成本地
        assert request_ip_is_local("10.0.0.1", "127.0.0.1, 10.0.0.1") is False
        # 显式声明可信反向代理后才采用 X-Forwarded-For
        assert request_ip_is_local("10.0.0.1", "127.0.0.1, 10.0.0.1", trust_forwarded=True) is True
        assert request_ip_is_local("127.0.0.1", "10.0.0.1", trust_forwarded=True) is False
        assert request_ip_is_local("localhost") is True

    def test_basic_auth_verification(self):
        from src.security import verify_basic_auth, web_auth_failure_kind

        assert verify_basic_auth("u", "p", "u", "p") is True
        assert verify_basic_auth("u", "bad", "u", "p") is False
        assert verify_basic_auth("u", "p", "", "p") is False

        assert web_auth_failure_kind("127.0.0.1", "", "127.0.0.1") is None
        assert web_auth_failure_kind("10.0.0.1", "", "0.0.0.0") == "forbidden"
        assert web_auth_failure_kind("10.0.0.1", "", "0.0.0.0", "u", "bad", "u", "p") == "auth_required"
        assert web_auth_failure_kind("10.0.0.1", "", "0.0.0.0", "u", "p", "u", "p") is None
        # 回归：绑定 127.0.0.1 时伪造 X-Forwarded-For 不能绕过认证
        assert web_auth_failure_kind("10.0.0.1", "127.0.0.1", "127.0.0.1") == "forbidden"
        assert (
            web_auth_failure_kind("10.0.0.1", "127.0.0.1", "127.0.0.1", trust_forwarded=True)
            is None
        )

    def test_flask_web_auth_response(self):
        from flask import Flask, request
        from src.security import require_web_auth

        app = Flask(__name__)

        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "127.0.0.1"}):
            assert require_web_auth(request, "127.0.0.1", "", "") is None

        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
            response, status = require_web_auth(request, "0.0.0.0", "", "")
            assert status == 403
            assert response.get_json()["error"] == "Web auth is required for non-local access"

        with app.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
            response = require_web_auth(request, "0.0.0.0", "u", "p")
            assert response.status_code == 401
            assert response.headers["WWW-Authenticate"] == 'Basic realm="tg-video-downloader"'

        headers = {"Authorization": "Basic dTpw"}
        with app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": "10.0.0.1"}):
            assert require_web_auth(request, "0.0.0.0", "u", "p") is None


class TestWebSessionAuth:
    """网页会话登录（auth_bp）：登录/失败/登出/鉴权状态。"""

    def _make_app(self, username="u", password="p", trust_forwarded=False):
        import os
        from flask import Flask
        from src.routes import auth

        auth.init_blueprint({
            "auth_username": username,
            "auth_password": password,
            "trust_forwarded": trust_forwarded,
        })
        templates = os.path.join(os.path.dirname(__file__), "..", "templates")
        app = Flask(__name__, template_folder=templates)
        app.secret_key = "test-secret"
        app.register_blueprint(auth.bp)
        return app

    def test_login_success_sets_session(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.post(
            "/api/login",
            json={"username": "u", "password": "p"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
        with client.session_transaction() as sess:
            assert sess.get("authed") is True

    def test_login_failure_returns_401(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.post(
            "/api/login",
            json={"username": "u", "password": "WRONG"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert resp.status_code == 401
        assert resp.get_json()["ok"] is False
        with client.session_transaction() as sess:
            assert sess.get("authed") is None

    def test_logout_clears_session(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        client.post(
            "/api/login",
            json={"username": "u", "password": "p"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        resp = client.post("/api/logout", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            assert sess.get("authed") is None

    def test_auth_status_non_local_requires_login(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.get("/api/auth/status", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        data = resp.get_json()
        assert data["auth_required"] is True
        assert data["local"] is False
        assert data["authed"] is False

    def test_auth_status_local_is_authed(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.get("/api/auth/status", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        data = resp.get_json()
        assert data["local"] is True
        assert data["authed"] is True

    def test_auth_status_no_credentials_means_no_auth_required(self):
        app = self._make_app("", "")
        client = app.test_client()
        resp = client.get("/api/auth/status", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        data = resp.get_json()
        assert data["auth_required"] is False
        assert data["authed"] is True

    def test_login_page_redirects_when_local(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.get("/login", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/")

    def test_login_page_renders_when_unauthed(self):
        app = self._make_app("u", "p")
        client = app.test_client()
        resp = client.get("/login", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 200
        assert b"password" in resp.data


class TestEnforceAccessControl:
    """app_new.enforce_access_control 四路放行（本地/会话/Basic/未认证）。"""

    def _client(self, monkeypatch, username="u", password="p"):
        import app_new

        monkeypatch.setattr(app_new, "WEB_AUTH_USERNAME", username)
        monkeypatch.setattr(app_new, "WEB_AUTH_PASSWORD", password)
        return app_new.app.test_client()

    def test_local_request_allowed_without_credentials(self, monkeypatch):
        # 无凭据 + 本地绑定 + 本地请求 → 放行
        client = self._client(monkeypatch, username="", password="")
        resp = client.get("/", environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert resp.status_code == 200

    def test_session_authed_allowed(self, monkeypatch):
        client = self._client(monkeypatch)
        with client.session_transaction() as sess:
            sess["authed"] = True
        resp = client.get("/", environ_base={"REMOTE_ADDR": "10.0.0.1"})
        assert resp.status_code == 200

    def test_basic_auth_allowed(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/",
            headers={"Authorization": "Basic dTpw"},  # u:p
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert resp.status_code == 200

    def test_unauthed_api_request_returns_401(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/api/status",
            headers={"Accept": "application/json"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert resp.status_code == 401

    def test_unauthed_browser_request_redirects_to_login(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.get(
            "/",
            headers={"Accept": "text/html"},
            environ_base={"REMOTE_ADDR": "10.0.0.1"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/login")

    def test_health_and_login_paths_are_exempt(self, monkeypatch):
        client = self._client(monkeypatch)
        for path in ("/api/health/live", "/api/health/ready", "/login",
                     "/api/auth/status"):
            resp = client.get(path, environ_base={"REMOTE_ADDR": "10.0.0.1"})
            assert resp.status_code != 401, path


class TestTelegramLoginRoutes:
    """Telegram 网页登录向导端点：send_code/sign_in/2FA/错误分支/finalize。"""

    def _make_client(self, login_runner, finalize=None, state=None):
        from flask import Flask
        from unittest.mock import Mock
        from src.routes import telegram

        telegram._login_pending.clear()
        deps = {
            "tg_client": Mock(),
            "run_async_func": lambda *a, **k: None,
            "kickoff_dialogs_func": lambda **k: False,
            "dialogs_snapshot_func": lambda: {
                "dialogs": [], "loading": False, "error": "", "updated_at": 0,
            },
            "resolve_entity_func": lambda **k: (None, ""),
            "video_info_func": lambda *a, **k: None,
            "make_excerpt_func": lambda *a, **k: "",
            "message_text_func": lambda *a, **k: "",
            "get_cached_message_func": lambda *a, **k: None,
            "resolve_message_func": lambda *a, **k: None,
            "abort_debug_func": lambda: None,
            "thumb_dir": "/tmp",
            "relay_token_secret": "",
            "dialogs_cache_ref": [],
            "current_entity_cache_ref": {},
            "videos_cache_ref": {},
            "replies_cache_ref": {},
            "video_service": Mock(),
            "login_run_async_func": login_runner,
            "finalize_login_func": finalize or (lambda: "Alice"),
            "get_login_state_func": state or (lambda: {"authorized": False, "needs_login": True}),
        }
        telegram.init_blueprint(deps)
        app = Flask(__name__)
        app.register_blueprint(telegram.bp)
        return app.test_client(), telegram

    def test_login_status_reports_state(self):
        client, _tg = self._make_client(
            lambda f: None,
            state=lambda: {"authorized": True, "needs_login": False},
        )
        data = client.get("/api/tg/login/status").get_json()
        assert data == {"authorized": True, "needs_login": False}

    def test_send_code_stores_hash(self):
        from unittest.mock import Mock

        client, tg = self._make_client(lambda f: Mock(phone_code_hash="HASH123"))
        resp = client.post("/api/tg/login/send_code", json={"phone": "+8613800138000"})
        assert resp.status_code == 200
        assert resp.get_json()["code_needed"] is True
        assert tg._login_pending["phone"] == "+8613800138000"
        assert tg._login_pending["phone_code_hash"] == "HASH123"

    def test_send_code_requires_phone(self):
        client, _tg = self._make_client(lambda f: None)
        resp = client.post("/api/tg/login/send_code", json={})
        assert resp.status_code == 400

    def test_sign_in_success_finalizes(self):
        from unittest.mock import Mock

        finalize_calls = []

        def finalize():
            finalize_calls.append(True)
            return "Alice (@alice)"

        client, tg = self._make_client(
            lambda f: Mock(phone_code_hash="H"), finalize=finalize
        )
        client.post("/api/tg/login/send_code", json={"phone": "+100"})

        # sign_in 成功（login_runner 返回 None，不抛异常）
        def runner(_factory):
            return None

        tg._login_run_async = runner
        resp = client.post("/api/tg/login/sign_in", json={"code": "12345"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["user"] == "Alice (@alice)"
        assert finalize_calls == [True]
        # 成功后 pending 清空
        assert tg._login_pending == {}

    def test_sign_in_password_needed(self):
        from unittest.mock import Mock
        from telethon.errors import SessionPasswordNeededError

        client, tg = self._make_client(lambda f: Mock(phone_code_hash="H"))
        client.post("/api/tg/login/send_code", json={"phone": "+100"})

        def runner(_factory):
            raise SessionPasswordNeededError(request=None)

        tg._login_run_async = runner
        resp = client.post("/api/tg/login/sign_in", json={"code": "12345"})
        assert resp.status_code == 200
        assert resp.get_json()["password_needed"] is True

    def test_sign_in_invalid_code_friendly_error(self):
        from unittest.mock import Mock
        from telethon.errors import PhoneCodeInvalidError

        client, tg = self._make_client(lambda f: Mock(phone_code_hash="H"))
        client.post("/api/tg/login/send_code", json={"phone": "+100"})

        def runner(_factory):
            raise PhoneCodeInvalidError(request=None)

        tg._login_run_async = runner
        resp = client.post("/api/tg/login/sign_in", json={"code": "00000"})
        assert resp.status_code == 400
        assert "验证码错误" in resp.get_json()["error"]

    def test_sign_in_without_pending_rejected(self):
        client, tg = self._make_client(lambda f: None)
        tg._login_pending.clear()
        resp = client.post("/api/tg/login/sign_in", json={"code": "123"})
        assert resp.status_code == 400
        assert "失效" in resp.get_json()["error"]

    def test_password_success_finalizes(self):
        finalize_calls = []
        client, tg = self._make_client(
            lambda f: None, finalize=lambda: finalize_calls.append(True) or "Bob"
        )
        resp = client.post("/api/tg/login/password", json={"password": "secret"})
        assert resp.status_code == 200
        assert resp.get_json()["user"] == "Bob"
        assert finalize_calls == [True]


class TestSystemStatusService:
    def test_status_and_health_payload(self):
        from src.system import SystemStatusService

        calls = {"ensure": 0}
        service = SystemStatusService(
            ensure_tg_connection=lambda allow_reconnect=True: calls.__setitem__("ensure", calls["ensure"] + 1),
            get_tg_connected=lambda: True,
            get_tg_error=lambda: "",
            get_tg_user=lambda: "user",
            get_queue_status=lambda: {"active": 0},
            get_tdl_status=lambda: {"active": 0},
            proxy_config=None,
            tdl_binary="/missing/tdl",
            get_tasks_persisted=lambda: 2,
            get_resume_count=lambda: 1,
            get_relay_status=lambda: {"active": 0},
        )

        status = service.status_payload()
        assert calls["ensure"] == 1
        assert status["connected"] is True
        assert status["queue"] == {"active": 0}

        health = service.health_payload()
        assert health["ok"] is True
        assert health["proxy"] == {"enabled": False, "ok": True, "label": "未启用"}
        assert health["tasks_persisted"] == 2
        assert health["resume_files"] == 1
        assert health["tdl"]["ok"] is False
        # tdl 不可用 → degraded 含 tdl，但 telegram 已连 → ok 仍 True
        assert "tdl" in health["degraded"]
        assert "telegram" not in health["degraded"]

    def test_liveness_and_readiness(self):
        from src.system import SystemStatusService

        def make(connected):
            return SystemStatusService(
                ensure_tg_connection=lambda allow_reconnect=True: None,
                get_tg_connected=lambda: connected,
                get_tg_error=lambda: "",
                get_tg_user=lambda: "user",
                get_queue_status=lambda: {"active": 0},
                get_tdl_status=lambda: {"active": 0},
                proxy_config=None,
                tdl_binary="/missing/tdl",
            )

        # liveness 与外部依赖无关，始终 alive
        assert make(False).liveness_payload() == {"status": "alive"}

        ready_payload, ready_status = make(True).readiness_payload()
        assert ready_status == 200
        assert ready_payload["ready"] is True
        assert ready_payload["degraded"] == []

        notready_payload, notready_status = make(False).readiness_payload()
        assert notready_status == 503
        assert notready_payload["ready"] is False
        assert "telegram" in notready_payload["degraded"]


class TestLogRedaction:
    def test_redacts_sensitive_values(self):
        from src.utils.log_filters import redact

        assert redact("password=hunter2 done") == "password=*** done"
        assert redact('WEB_AUTH_PASSWORD: "s3cr3t"') == 'WEB_AUTH_PASSWORD: "***"'
        assert redact("token=abcdef123456&next") == "token=***&next"
        assert redact("api_hash=deadbeefcafe") == "api_hash=***"
        auth = redact("Authorization: Basic dXNlcjpwYXNz")
        assert auth.startswith("Authorization: ***")
        assert "dXNlcjpwYXNz" not in auth
        # 无敏感词不改动
        assert redact("下载完成 task=-100:42 3MB") == "下载完成 task=-100:42 3MB"
        # 良性字段（敏感词仅作前缀、紧邻的是别的词）不得被误伤
        assert redact("token_ttl=1800 next") == "token_ttl=1800 next"
        assert redact("authorization_status=ok reply_count=3") == "authorization_status=ok reply_count=3"
        # RELAY_TOKEN_SECRET 这类以敏感词结尾的 env 键仍被脱敏
        assert redact("RELAY_TOKEN_SECRET=deadbeef") == "RELAY_TOKEN_SECRET=***"

    def test_filter_mutates_record(self):
        import logging
        from src.utils.log_filters import RedactionFilter

        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "login password=topsecret", None, None
        )
        assert RedactionFilter().filter(record) is True
        assert "topsecret" not in record.getMessage()
        assert "***" in record.getMessage()


class TestSystemStartup:
    def test_validate_runtime_config(self):
        from src.system import validate_runtime_config

        validate_runtime_config(123, "hash", "127.0.0.1", "", "")
        validate_runtime_config(123, "hash", "0.0.0.0", "user", "pass")

        with pytest.raises(RuntimeError, match="Missing TG_API_ID"):
            validate_runtime_config(0, "hash", "127.0.0.1", "", "")
        with pytest.raises(RuntimeError, match="Missing TG_API_ID"):
            validate_runtime_config(123, "", "127.0.0.1", "", "")
        with pytest.raises(RuntimeError, match="Non-local binding"):
            validate_runtime_config(123, "hash", "0.0.0.0", "", "")

        # relay token secret 最小长度校验：空放行、足够长放行、非空但过短抛错
        validate_runtime_config(123, "hash", "127.0.0.1", "", "", relay_token_secret="")
        validate_runtime_config(123, "hash", "127.0.0.1", "", "", relay_token_secret="a" * 64)
        with pytest.raises(RuntimeError, match="RELAY_TOKEN_SECRET too short"):
            validate_runtime_config(123, "hash", "127.0.0.1", "", "", relay_token_secret="abc")

    def test_start_runtime_services_orchestrates_background_work(self):
        from src.system import start_runtime_services

        calls = []

        class FakeThread:
            def __init__(self, target, daemon=False):
                self.target = target
                self.daemon = daemon
                self.started = False
                calls.append(("thread", target.__name__, daemon))

            def start(self):
                self.started = True
                calls.append(("start", self.target.__name__))

        class FakeWatchdog:
            def __init__(self):
                self.started = False

            def start(self):
                self.started = True
                calls.append(("watchdog", "start"))

        watchdog = FakeWatchdog()

        with tempfile.TemporaryDirectory() as base:
            download_dir = os.path.join(base, "downloads")
            result = start_runtime_services(
                download_dir=download_dir,
                load_persisted_states=lambda: calls.append(("load", None)) or 2,
                log_info=lambda message: calls.append(("log", message)),
                restore_resume_tasks=lambda: calls.append(("restore", None)),
                start_background_clients=lambda: calls.append(("background", None)) or {"tg_thread": "thread"},
                auto_resume_incomplete_tasks=lambda: None,
                download_watchdog=watchdog,
                thumbnail_cleanup_loop=lambda: None,
                task_database_backup_loop=lambda: None,
                thread_factory=FakeThread,
            )

            assert os.path.isdir(download_dir)

        assert result["restored_states"] == 2
        assert result["background"] == {"tg_thread": "thread"}
        assert watchdog.started is True
        assert calls[:4] == [
            ("load", None),
            ("log", "已加载持久化下载任务: 2"),
            ("restore", None),
            ("background", None),
        ]
        assert [item for item in calls if item[0] == "start"] == [
            ("start", "<lambda>"),
            ("start", "<lambda>"),
            ("start", "<lambda>"),
        ]


# ==================== 异步下载管理器 ====================
class TestAsyncManager:
    def test_stats(self):
        from src.download.async_manager import AsyncDownloadManager
        m = AsyncDownloadManager(max_concurrent=3)
        stats = m.get_stats()
        assert stats["max_concurrent"] == 3
        assert stats["active_tasks"] == 0
        assert stats["is_running"] is False


class TestDownloadWatchdog:
    def test_progress_tracking_and_cleanup(self):
        from src.download.watchdog import DownloadWatchdog

        tasks = {
            "t1": {"status": "downloading", "downloaded_bytes": 10},
            "t2": {"status": "done", "downloaded_bytes": 5},
        }
        watchdog = DownloadWatchdog(get_tasks_callback=lambda: tasks)
        watchdog.last_progress["t2"] = {"bytes": 5, "time": 1}

        watchdog._check_all_tasks()

        assert watchdog.last_progress["t1"]["bytes"] == 10
        assert "t2" not in watchdog.last_progress

    def test_stop_is_responsive(self):
        from src.download.watchdog import DownloadWatchdog

        # check_interval 很大；Event.wait 让 stop 立即唤醒线程退出（原 time.sleep 做不到）
        watchdog = DownloadWatchdog(check_interval=100, get_tasks_callback=lambda: {})
        watchdog.start()
        watchdog.stop()
        assert watchdog._thread is not None
        assert not watchdog._thread.is_alive()

    def test_updates_when_bytes_advance(self):
        from src.download.watchdog import DownloadWatchdog

        watchdog = DownloadWatchdog(stall_timeout=5)
        watchdog._check_task("t1", {"downloaded_bytes": 10}, current_time=10)
        watchdog._check_task("t1", {"downloaded_bytes": 20}, current_time=12)

        assert watchdog.last_progress["t1"] == {"bytes": 20, "time": 12}

    def test_stalled_task_triggers_restart_and_clears_record(self):
        from src.download.watchdog import DownloadWatchdog

        calls = []
        logs = []
        watchdog = DownloadWatchdog(
            stall_timeout=5,
            restart_task_callback=lambda task_id, task: calls.append((task_id, task)) or {"ok": True},
            log_info=logs.append,
            log_warning=logs.append,
            log_error=logs.append,
        )
        task = {
            "status": "downloading",
            "downloaded_bytes": 10,
            "downloaded": "10B",
            "progress": 1,
            "entity_id": 123,
            "msg_id": 4,
        }

        watchdog._check_task("t1", task, current_time=10)
        watchdog._check_task("t1", task, current_time=16)

        assert calls == [("t1", task)]
        assert "t1" not in watchdog.last_progress
        assert any("重启成功" in item for item in logs)

    def test_stalled_task_missing_identity_does_not_restart(self):
        from src.download.watchdog import DownloadWatchdog

        calls = []
        watchdog = DownloadWatchdog(
            stall_timeout=5,
            restart_task_callback=lambda task_id, task: calls.append((task_id, task)),
        )

        watchdog._check_task("t1", {"downloaded_bytes": 10}, current_time=10)
        watchdog._check_task("t1", {"downloaded_bytes": 10}, current_time=16)

        assert calls == []


class TestDownloadStatusPayload:
    def test_build_download_status_payload_cleans_stale_terminal_tasks(self):
        from src.download import build_download_status_payload

        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        calls = []
        states = {
            "old": {"status": "done", "finish_time": 10},
            "active": {"status": "downloading", "progress": 5},
            "recent": {"status": "error", "finish_time": 95},
        }

        payload = build_download_status_payload(
            recover_stalled_tasks=lambda: calls.append("recover"),
            restore_resume_tasks=lambda: calls.append("restore"),
            status_lock=Lock(),
            download_status=states,
            terminal_states={"done", "error", "cancelled"},
            drop_task_state=lambda task_id: states.pop(task_id),
            get_queue_status=lambda: {"queued": 1},
            now_func=lambda: 100,
            terminal_ttl=60,
        )

        assert calls == ["recover", "restore"]
        assert "old" not in states
        assert set(payload["tasks"]) == {"active", "recent"}
        assert payload["queue"] == {"queued": 1}

        payload["tasks"]["active"]["progress"] = 99
        assert states["active"]["progress"] == 5


class TestDownloadTaskActions:
    def test_clear_tasks_by_scope(self):
        from src.download import clear_tasks_by_scope

        class Lock:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        states = {
            "done": {"status": "done"},
            "error": {"status": "error"},
            "active": {"status": "downloading"},
        }
        calls = []

        cleared = clear_tasks_by_scope(
            scope="all",
            terminal_states={"done", "cancelled"},
            status_lock=Lock(),
            download_status=states,
            drop_task_state=lambda task_id: calls.append(("drop", task_id)),
            clear_download_cancelled=lambda task_id: calls.append(("cancel", task_id)),
            clear_tdl_error=lambda task_id: calls.append(("tdl", task_id)),
            clear_resume_info=lambda task_id: calls.append(("resume", task_id)),
        )

        assert cleared == 2
        assert ("drop", "done") in calls
        assert ("drop", "error") in calls
        assert ("drop", "active") not in calls

    def test_query_task_history_payload(self):
        from src.download import query_task_history_payload

        payload = query_task_history_payload(
            lambda status, query, page, per_page: (
                [{"status": status, "query": query, "page": page, "per_page": per_page}],
                1,
            ),
            status="done",
            query="video",
            page=0,
            per_page=999,
        )

        assert payload == {
            "items": [{"status": "done", "query": "video", "page": 1, "per_page": 100}],
            "total": 1,
            "page": 1,
            "per_page": 100,
        }

    def test_recover_candidate_tasks(self):
        from src.download import recover_candidate_tasks

        def resume_task(task_id, dialog_name=None, auto=False):
            if task_id == "ok":
                return {"ok": True}
            if task_id == "bad":
                return {"ok": False, "error": "failed"}
            raise RuntimeError("boom")

        payload = recover_candidate_tasks(
            task_ids=["ok", "bad", "missing", "raise"],
            get_recovery_candidates=lambda limit: [
                {"task_id": "ok"},
                {"task_id": "bad"},
                {"task_id": "raise"},
            ],
            resume_task=resume_task,
        )

        assert payload["submitted"] == ["ok"]
        assert payload["errors"] == {
            "bad": "failed",
            "missing": "任务不在可恢复日志列表中",
            "raise": "boom",
        }


# ==================== Telegram 健康检查 ====================
class TestHealthChecker:
    def test_init(self):
        from src.telegram.health_checker import TelegramHealthChecker
        checker = TelegramHealthChecker(
            client=Mock(),
            loop=Mock(),
            check_interval=60,
            max_retry=5,
        )
        assert checker.check_interval == 60
        assert checker.max_retry == 5
        assert checker._running is False

    def test_get_stats(self):
        from src.telegram.health_checker import TelegramHealthChecker
        checker = TelegramHealthChecker(client=Mock(), loop=Mock())
        stats = checker.get_stats()
        assert stats["running"] is False
        assert stats["status"] == "healthy"

    def test_async_check_uses_light_dialog_ping(self):
        from src.telegram.health_checker import TelegramHealthChecker

        client = Mock()
        client.is_connected.return_value = True
        client.get_dialogs = AsyncMock(return_value=[])
        checker = TelegramHealthChecker(client=client, loop=Mock())

        assert asyncio.run(checker._async_check()) is True
        client.get_dialogs.assert_awaited_once_with(limit=1)

    def test_failure_threshold_triggers_reconnect(self):
        from src.telegram.health_checker import TelegramHealthChecker

        calls = []
        checker = TelegramHealthChecker(client=Mock(), loop=Mock(), max_retry=1)
        checker._attempt_reconnect = lambda: calls.append("reconnect")

        checker._handle_check_failure()

        assert calls == ["reconnect"]
        assert checker.get_stats()["failure_count"] == 0

    def test_health_checker_stop_is_responsive(self):
        from src.telegram.health_checker import TelegramHealthChecker

        # check_interval 很大；若仍用 time.sleep 则 stop 后线程仍存活，改用
        # 可中断的 Event.wait 后 stop 立即唤醒线程退出。
        hc = TelegramHealthChecker(client=Mock(), loop=Mock(), check_interval=100)
        hc.start()
        hc.stop()
        assert hc._thread is not None
        assert not hc._thread.is_alive()


class TestGracefulShutdown:
    def test_shutdown_order_and_idempotent(self):
        import threading as _th
        from src.system import GracefulShutdown

        events = []
        stop_event = _th.Event()

        class Stoppable:
            def __init__(self, name):
                self.name = name

            def stop(self):
                events.append(f"stop:{self.name}")

        class FakeThread:
            def join(self, timeout=None):
                events.append("join")

        gs = GracefulShutdown(
            stop_event=stop_event,
            stoppables=[Stoppable("wd"), Stoppable("hc")],
            disconnect_clients=lambda: events.append("disconnect"),
            close_persistence=lambda: events.append("close"),
            join_threads=[FakeThread(), lambda: None],
        )
        gs.shutdown()
        assert stop_event.is_set()
        assert events == ["stop:wd", "stop:hc", "disconnect", "join", "close"]
        # 幂等：重复信号只执行一次
        gs.shutdown()
        assert events == ["stop:wd", "stop:hc", "disconnect", "join", "close"]

    def test_shutdown_is_defensive(self):
        import threading as _th
        from src.system import GracefulShutdown

        calls = []

        class Boom:
            def stop(self):
                raise RuntimeError("boom")

        def _raise():
            raise RuntimeError("disconnect failed")

        gs = GracefulShutdown(
            stop_event=_th.Event(),
            stoppables=[Boom()],
            disconnect_clients=_raise,
            close_persistence=lambda: calls.append("close"),
        )
        gs.shutdown()  # 单步异常不得阻塞后续，也不抛出
        assert calls == ["close"]


class TestTelegramStartup:
    def test_run_main_telegram_client_success(self):
        from src.telegram import run_main_telegram_client

        class Runtime:
            def __init__(self):
                self.connected = False
                self.connect_error = ""
                self.user_info = ""

            def mark_error(self, message):
                self.connected = False
                self.connect_error = message

            def mark_connected(self, user_info):
                self.connected = True
                self.connect_error = ""
                self.user_info = user_info

        class Client:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return Mock(first_name="Alice", username="alice")

            async def disconnect(self):
                return None

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        callbacks = []

        try:
            run_main_telegram_client(
                client=Client(),
                loop=loop,
                runtime=runtime,
                format_user_display=lambda user: f"{user.first_name} (@{user.username})",
                init_health_checker=lambda: (callbacks.append("health"), loop.stop()),
                on_connecting=lambda message: callbacks.append(("connecting", message)),
                on_connected=lambda user_info: callbacks.append(("connected", user_info)),
                on_error=lambda message: callbacks.append(("error", message)),
                log_info=lambda _message: None,
                print_func=lambda _message: None,
                sleep_func=lambda _seconds: None,
            )
        finally:
            loop.close()

        assert runtime.connected is True
        assert runtime.user_info == "Alice (@alice)"
        assert ("connecting", "正在连接 Telegram...") in callbacks
        assert ("connected", "Alice (@alice)") in callbacks
        assert "health" in callbacks

    def test_run_main_telegram_client_stays_alive_when_unauthorized(self):
        # 未授权时不再退出进程：应 mark_needs_login、不调用 exit_func、保活 run_forever。
        from src.telegram import run_main_telegram_client

        class Runtime:
            def __init__(self):
                self.connect_error = ""
                self.needs_login = False

            def mark_error(self, message):
                self.connect_error = message

            def mark_needs_login(self, message):
                self.needs_login = True
                self.connect_error = message

            def mark_connected(self, _user_info):
                raise AssertionError("should not connect")

        class Client:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                return False

            async def disconnect(self):
                return None

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        errors = []
        exit_calls = []
        health_calls = []

        try:
            run_main_telegram_client(
                client=Client(),
                loop=loop,
                runtime=runtime,
                format_user_display=lambda user: user.first_name,
                init_health_checker=lambda: health_calls.append(True),
                on_connecting=lambda _message: None,
                # 未授权分支在 run_forever 前调用 on_error；此处顺手停 loop 让其立即返回
                on_connected=lambda _user_info: None,
                on_error=lambda message: (errors.append(message), loop.stop()),
                log_info=lambda _message: None,
                print_func=lambda _message: None,
                sleep_func=lambda _seconds: None,
                exit_func=lambda _code: exit_calls.append(_code),
            )
        finally:
            loop.close()

        assert runtime.needs_login is True
        assert exit_calls == []          # 关键：绝不退出进程
        assert health_calls == []        # 未授权不启动健康检查
        assert errors and "网页向导" in errors[0]

    def test_run_main_telegram_client_retries_on_connect_error(self):
        from src.telegram import run_main_telegram_client

        class Runtime:
            def __init__(self):
                self.connect_error = ""

            def mark_error(self, message):
                self.connect_error = message

            def mark_connected(self, _user_info):
                raise AssertionError("should not connect")

        class Client:
            async def connect(self):
                raise RuntimeError("boom")

            async def disconnect(self):
                return None

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        errors = []

        try:
            with pytest.raises(SystemExit):
                run_main_telegram_client(
                    client=Client(),
                    loop=loop,
                    runtime=runtime,
                    format_user_display=lambda user: user.first_name,
                    init_health_checker=lambda: None,
                    on_connecting=lambda _message: None,
                    on_connected=lambda _user_info: None,
                    on_error=errors.append,
                    log_info=lambda _message: None,
                    print_func=lambda _message: None,
                    sleep_func=lambda _seconds: (_ for _ in ()).throw(SystemExit),
                )
        finally:
            loop.close()

        assert runtime.connect_error == "连接失败: boom，5秒后重试..."
        assert errors[-1] == "连接失败: boom，5秒后重试..."

    def test_run_relay_telegram_client_success(self):
        from src.telegram import run_relay_telegram_client

        class Runtime:
            def __init__(self):
                self.client = None
                self.connected = False
                self.connect_error = ""

            def mark_error(self, message):
                self.connected = False
                self.connect_error = message

            def mark_connected(self):
                self.connected = True
                self.connect_error = ""

        class Client:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                return True

            async def disconnect(self):
                return None

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        client = Client()
        callbacks = []

        try:
            run_relay_telegram_client(
                loop=loop,
                runtime=runtime,
                wait_for_main_ready=lambda timeout: True,
                get_main_error=lambda: "",
                recreate_client=lambda: client,
                on_client_recreated=lambda value: callbacks.append(("client", value)),
                on_connecting=lambda message: callbacks.append(("connecting", message)),
                on_connected=lambda: (callbacks.append("connected"), loop.stop()),
                on_error=lambda message: callbacks.append(("error", message)),
                log_info=lambda _message: None,
                log_warning=lambda _message: None,
                log_error=lambda _message: None,
                sleep_func=lambda _seconds: None,
            )
        finally:
            loop.close()

        assert runtime.connected is True
        assert runtime.client is client
        assert ("client", client) in callbacks
        assert ("connecting", "正在连接 Relay Telegram...") in callbacks
        assert "connected" in callbacks

    def test_run_relay_telegram_client_returns_when_unauthorized(self):
        from src.telegram import run_relay_telegram_client

        class Runtime:
            def __init__(self):
                self.client = None
                self.connected = False
                self.connect_error = ""

            def mark_error(self, message):
                self.connected = False
                self.connect_error = message

            def mark_connected(self):
                raise AssertionError("should not connect")

        class Client:
            async def connect(self):
                return None

            async def is_user_authorized(self):
                return False

            async def disconnect(self):
                return None

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        errors = []

        try:
            run_relay_telegram_client(
                loop=loop,
                runtime=runtime,
                wait_for_main_ready=lambda timeout: True,
                get_main_error=lambda: "",
                recreate_client=Client,
                on_client_recreated=lambda _client: None,
                on_connecting=lambda _message: None,
                on_connected=lambda: None,
                on_error=errors.append,
                log_info=lambda _message: None,
                log_warning=lambda _message: None,
                log_error=lambda _message: None,
                sleep_func=lambda _seconds: None,
            )
        finally:
            loop.close()

        assert runtime.connect_error == "Relay Telegram 未登录"
        assert errors == ["Relay Telegram 未登录"]

    def test_run_relay_telegram_client_retries_on_main_not_ready(self):
        from src.telegram import run_relay_telegram_client

        class Runtime:
            def __init__(self):
                self.connect_error = ""

            def mark_error(self, message):
                self.connect_error = message

            def mark_connected(self):
                raise AssertionError("should not connect")

        loop = asyncio.new_event_loop()
        runtime = Runtime()
        errors = []

        try:
            with pytest.raises(SystemExit):
                run_relay_telegram_client(
                    loop=loop,
                    runtime=runtime,
                    wait_for_main_ready=lambda timeout: False,
                    get_main_error=lambda: "main down",
                    recreate_client=lambda: None,
                    on_client_recreated=lambda _client: None,
                    on_connecting=lambda _message: None,
                    on_connected=lambda: None,
                    on_error=errors.append,
                    log_info=lambda _message: None,
                    log_warning=lambda _message: None,
                    log_error=lambda _message: None,
                    sleep_func=lambda _seconds: (_ for _ in ()).throw(SystemExit),
                )
        finally:
            loop.close()

        assert runtime.connect_error == "Relay 连接失败: main down，5秒后重试..."
        assert errors[-1] == "Relay 连接失败: main down，5秒后重试..."


# ==================== 辅助函数 ====================
class TestHelpers:
    def test_make_task_id(self):
        from src.helpers import make_task_id
        assert make_task_id(123, 456) == "123_456"
        assert make_task_id(None, 456) is None

    def test_make_excerpt(self):
        from src.helpers import make_excerpt
        assert make_excerpt("short", 10) == "short"
        assert make_excerpt("very long text here", 10) == "very long ..."

    def test_sanitize_filename(self):
        from src.helpers import sanitize_filename
        assert sanitize_filename("file<>name.mp4") == "file__name.mp4"
        assert sanitize_filename("") == "unnamed"

    def test_supports_tdl_download(self):
        from src.helpers import supports_tdl_download
        assert supports_tdl_download(-1001234567890) is True
        assert supports_tdl_download(None) is False

    def test_build_tdl_message_url(self):
        from src.helpers import build_tdl_message_url
        url = build_tdl_message_url(-1001234567890, 42)
        assert url == "https://t.me/c/1234567890/42"


class TestTelegramRuntime:
    def test_message_cache_and_ids(self):
        from src.telegram.runtime import TelegramRuntime

        runtime = TelegramRuntime(client=Mock(), loop=Mock(), max_message_cache_size=2)
        message = Mock()
        message.id = 42

        assert runtime.make_task_id(-100123, 42) == "-100123:42"
        assert runtime.make_msg_cache_key(-100123, 42) == (-100123, 42)

        runtime.cache_message(message, -100123)
        assert runtime.get_cached_message(42, -100123) is message

        runtime.current_entity_cache["entity_id"] = -100123
        assert runtime.get_cached_message(42) is message

    def test_get_cached_message_does_not_cross_entity(self):
        from src.telegram.runtime import TelegramRuntime

        runtime = TelegramRuntime(client=Mock(), loop=Mock())
        msg_a = Mock()
        msg_a.id = 100
        runtime.cache_message(msg_a, -100111)

        # 指定了另一个频道且未命中：不得返回其他频道同 msg_id 的消息
        assert runtime.get_cached_message(100, -100999) is None
        # 同频道精确命中仍正常
        assert runtime.get_cached_message(100, -100111) is msg_a
        # entity 缺省时跨频道兜底仍生效
        assert runtime.get_cached_message(100) is msg_a

    def test_ensure_connection_reconnects_in_background(self):
        from src.telegram.runtime import TelegramRuntime

        client = Mock()
        client.is_connected.return_value = False
        loop = Mock()
        loop.is_running.return_value = True
        runtime = TelegramRuntime(client=client, loop=loop)
        runtime.reconnect_grace_seconds = 0  # 关闭宽限等待，保持测试快速

        started = []

        class FakeThread:
            def __init__(self, target=None, daemon=None):
                self.target = target

            def start(self):
                started.append(self.target)

        runtime._reconnect_thread_factory = FakeThread

        # 断开状态：立即快速失败返回 False（不阻塞），并排布一个后台重连线程
        assert runtime.ensure_connection() is False
        assert len(started) == 1
        assert "正在重连" in runtime.connect_error
        assert runtime.reconnect_in_progress is True

        # 冷却窗口内 / 已有重连进行中：不重复排布线程
        assert runtime.ensure_connection() is False
        assert len(started) == 1

    def test_start_reconnect_failure_rolls_back_in_progress(self):
        from src.telegram.runtime import TelegramRuntime

        client = Mock()
        client.is_connected.return_value = False
        loop = Mock()
        loop.is_running.return_value = True
        runtime = TelegramRuntime(client=client, loop=loop)
        runtime.reconnect_grace_seconds = 0

        def _boom_factory(*_args, **_kwargs):
            raise RuntimeError("cannot start thread")

        runtime._reconnect_thread_factory = _boom_factory
        # 线程启动失败：不得永久卡在“重连中”，in_progress 必须回滚
        assert runtime.ensure_connection() is False
        assert runtime.reconnect_in_progress is False

    def test_ensure_connection_grace_returns_true_on_quick_reconnect(self):
        from src.telegram.runtime import TelegramRuntime

        client = Mock()
        # 首次断开触发重连；宽限窗口内 is_connected 变 True → 本次请求即返回成功
        client.is_connected.side_effect = [False, False, True]
        loop = Mock()
        loop.is_running.return_value = True
        runtime = TelegramRuntime(client=client, loop=loop)
        runtime.reconnect_grace_seconds = 1.0
        runtime._reconnect_thread_factory = lambda *a, **k: type(
            "T", (), {"start": lambda self: None}
        )()

        assert runtime.ensure_connection() is True
        assert runtime.connected is True

    def test_reconnect_cooldown_exponential_backoff(self):
        from src.telegram.runtime import TelegramRuntime

        runtime = TelegramRuntime(client=Mock(), loop=Mock())
        runtime.reconnect_failures = 0
        assert runtime._reconnect_cooldown() == 8
        runtime.reconnect_failures = 1
        assert runtime._reconnect_cooldown() == 16
        runtime.reconnect_failures = 2
        assert runtime._reconnect_cooldown() == 32
        runtime.reconnect_failures = 10  # 触顶封顶
        assert runtime._reconnect_cooldown() == 120

    def test_dialog_serialization_prioritizes_saved_messages(self):
        from src.telegram.runtime import TelegramRuntime

        runtime = TelegramRuntime(client=Mock(), loop=Mock())

        saved = Mock()
        saved.is_channel = False
        saved.is_group = False
        saved.name = "Saved"
        saved.id = 1
        saved.entity = Mock(is_self=True)

        channel = Mock()
        channel.is_channel = True
        channel.is_group = False
        channel.name = "Channel"
        channel.id = 2
        channel.entity = Mock(is_self=False)

        serialized = runtime.serialize_dialogs([channel, saved])
        assert serialized[0]["is_saved"] is True
        assert serialized[0]["name"].startswith("⭐")
        assert serialized[1]["type"] == "频道"


class TestTelegramVideoService:
    def test_list_videos_scans_and_caches(self):
        import threading
        from src.telegram import TelegramVideoService

        entity = Mock(id=123)
        message = Mock(id=7, message="hello video")
        message.replies = Mock(replies=2)
        calls = {"iter": 0}

        class Client:
            def iter_messages(self, _entity, **_kwargs):
                calls["iter"] += 1

                async def gen():
                    yield message

                return gen()

        service = TelegramVideoService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            resolve_requested_entity=lambda *_args: (entity, "chat"),
            video_info_for_message=lambda msg, eid, source="主消息", extra=None: {
                "id": msg.id,
                "entity_id": eid,
                "source": source,
                **(extra or {}),
            },
            message_text=lambda msg: msg.message,
            make_excerpt=lambda text, limit: text[:limit],
            cache_lock=threading.RLock(),
            current_entity_cache={},
            videos_cache={},
            replies_cache={},
        )

        payload, status = service.list_videos(entity_id=123, include_replies=True)
        cached_payload, cached_status = service.list_videos(entity_id=123, include_replies=True)

        assert status == 200
        assert payload["videos"] == [{"id": 7, "entity_id": 123, "source": "主消息"}]
        assert payload["posts_with_replies"] == [{"id": 7, "count": 2, "text_excerpt": "hello video"}]
        assert cached_status == 200
        assert cached_payload["cached"] is True
        assert calls["iter"] == 1

    def test_list_videos_ttl_expiry_triggers_rescan(self):
        import threading
        import time as _time
        from src.telegram import TelegramVideoService

        entity = Mock(id=123)
        message = Mock(id=7, message="hello video")
        message.replies = None
        calls = {"iter": 0}

        class Client:
            def iter_messages(self, _entity, **_kwargs):
                calls["iter"] += 1

                async def gen():
                    yield message

                return gen()

        videos_cache = {}
        service = TelegramVideoService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            resolve_requested_entity=lambda *_args: (entity, "chat"),
            video_info_for_message=lambda msg, eid, source="主消息", extra=None: {
                "id": msg.id,
                "entity_id": eid,
            },
            message_text=lambda msg: msg.message,
            make_excerpt=lambda text, limit: text[:limit],
            cache_lock=threading.RLock(),
            current_entity_cache={},
            videos_cache=videos_cache,
            replies_cache={},
            video_cache_ttl=100,
        )

        service.list_videos(entity_id=123)
        assert calls["iter"] == 1
        # 把缓存条目的时间戳改旧，超过 TTL：下次读应重扫而非返回过期缓存
        for entry in videos_cache.values():
            entry["time"] = _time.time() - 200
        payload, status = service.list_videos(entity_id=123)
        assert status == 200
        assert payload.get("cached") is False
        assert calls["iter"] == 2

    def test_list_replies_scans_parent_context(self):
        import threading
        from src.telegram import TelegramVideoService

        entity = Mock(id=123)
        parent = Mock(id=9, message="parent text")
        reply = Mock(id=10, message="reply video")

        class Client:
            async def get_entity(self, entity_id):
                return Mock(id=entity_id)

            async def get_messages(self, _entity, ids):
                assert ids == 9
                return parent

            def iter_messages(self, _entity, **kwargs):
                assert kwargs["reply_to"] == 9

                async def gen():
                    yield reply

                return gen()

        service = TelegramVideoService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            resolve_requested_entity=lambda *_args: (entity, "chat"),
            video_info_for_message=lambda msg, eid, source="主消息", extra=None: {
                "id": msg.id,
                "entity_id": eid,
                "source": source,
                **(extra or {}),
            },
            message_text=lambda msg: msg.message,
            make_excerpt=lambda text, limit: text[:limit],
            cache_lock=threading.RLock(),
            current_entity_cache={"entity": entity},
            videos_cache={},
            replies_cache={},
        )

        payload, status = service.list_replies(entity_id=123, post_id=9)

        assert status == 200
        assert payload["videos"] == [{
            "id": 10,
            "entity_id": 123,
            "source": "评论@帖子9",
            "parent_post_id": 9,
            "parent_text": "parent text",
            "parent_text_excerpt": "parent text",
        }]

    def test_search_videos_scans_channel_and_comments(self):
        import threading
        from src.telegram import TelegramVideoService

        entity = Mock(id=123)
        search_hit = Mock(id=1, message="needle", replies=None)
        file_hit = Mock(id=2, message="plain", replies=Mock(replies=1))
        reply_hit = Mock(id=3, message="needle in reply", replies=None)
        calls = []

        class Client:
            def iter_messages(self, _entity, **kwargs):
                calls.append(kwargs)

                async def gen():
                    if "search" in kwargs:
                        yield search_hit
                    elif kwargs.get("reply_to") == 2:
                        yield reply_hit
                    else:
                        yield file_hit

                return gen()

        def video_info(msg, eid, source="主消息", extra=None):
            filename = "needle.mp4" if msg.id == 2 else f"video-{msg.id}.mp4"
            return {
                "id": msg.id,
                "entity_id": eid,
                "filename": filename,
                "text": msg.message,
                "text_excerpt": msg.message,
                "date": f"2026-01-0{msg.id}",
                "source": source,
                **(extra or {}),
            }

        service = TelegramVideoService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            resolve_requested_entity=lambda *_args: (entity, "chat"),
            video_info_for_message=video_info,
            message_text=lambda msg: msg.message,
            make_excerpt=lambda text, limit: text[:limit],
            cache_lock=threading.RLock(),
            current_entity_cache={},
            videos_cache={},
            replies_cache={},
        )

        payload, status = service.search_videos(
            query="needle",
            entity_id=123,
            limit=10,
            scan_limit=20,
            include_comments=True,
            comment_post_limit=5,
            comment_limit=10,
        )

        assert status == 200
        assert [item["id"] for item in payload["videos"]] == [3, 2, 1]
        assert payload["telegram_hits"] == 1
        assert payload["scanned"] == 1
        assert payload["comments_scanned"] == 1
        assert payload["comment_hits"] == 1
        assert {"search": "needle", "limit": 10} in calls
        assert {"limit": 20} in calls
        assert {"reply_to": 2, "limit": 10} in calls


class TestTelegramDebugService:
    def test_inspect_messages_reads_dialog_and_media_attrs(self):
        import threading
        from src.telegram import TelegramDebugService

        entity = Mock(id=123)
        dialog = Mock(entity=entity)
        attr = Mock(file_name="video.mp4")
        doc = Mock(mime_type="video/mp4", size=123, attributes=[attr])
        msg = Mock(id=7, text="hello", media=Mock(document=doc))

        class Client:
            def iter_messages(self, selected_entity, **kwargs):
                assert selected_entity is entity
                assert kwargs == {"limit": 20}

                async def gen():
                    yield msg

                return gen()

        service = TelegramDebugService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            dialogs_cache=[dialog],
            cache_lock=threading.RLock(),
        )

        payload, status = service.inspect_messages(0)

        assert status == 200
        assert payload[0]["id"] == 7
        assert payload[0]["doc_mime"] == "video/mp4"
        assert payload[0]["attr_details"] == [{"file_name": "video.mp4"}]

    def test_inspect_full_messages_returns_markup_and_entities(self):
        import threading
        from src.telegram import TelegramDebugService

        entity = Mock(id=123)
        dialog = Mock(entity=entity)
        msg = Mock(id=8, text="full text", reply_markup="buttons", entities=["bold"])

        class Client:
            def iter_messages(self, selected_entity, **kwargs):
                assert selected_entity is entity
                assert kwargs == {"limit": 5}

                async def gen():
                    yield msg

                return gen()

        service = TelegramDebugService(
            client=Client(),
            run_async=lambda factory: asyncio.run(factory()),
            dialogs_cache=[dialog],
            cache_lock=threading.RLock(),
        )

        payload, status = service.inspect_full_messages(0)

        assert status == 200
        assert payload == [{
            "id": 8,
            "text": "full text",
            "reply_markup": "buttons",
            "entities": ["bold"],
        }]


# ==================== 路由 Blueprint ====================
class TestRoutes:
    def test_blueprints_registered(self):
        from src.routes import (
            files_bp, system_bp, telegram_bp,
            download_bp, misc_bp, relay_bp,
        )
        assert files_bp.name == "files"
        assert system_bp.name == "system"
        assert telegram_bp.name == "telegram"
        assert download_bp.name == "download"
        assert misc_bp.name == "fileservice"
        assert relay_bp.name == "relay"

    def test_misc_clear_tasks_accepts_task_ids(self):
        from flask import Flask
        from src.routes import misc

        calls = {}
        app = Flask(__name__)
        misc.init_blueprint({
            "download_dir": "/tmp",
            "format_size_func": lambda size: str(size),
            "query_task_history_func": lambda *_args: {},
            "get_download_status_func": lambda: {},
            "clear_all_tasks_func": lambda scope: calls.setdefault("scope", scope) or 0,
            "get_recovery_candidates_func": lambda: [],
            "recover_candidates_func": lambda task_ids: {"ok": True, "submitted": task_ids, "errors": {}},
            "abort_debug_func": lambda: None,
            "resolve_download_path_func": lambda *_args, **_kwargs: "/tmp",
            "clear_task_ids_func": lambda task_ids: {"ok": True, "cleared": len(task_ids), "skipped": 0},
            "debug_service": Mock(),
        })
        app.register_blueprint(misc.bp)

        response = app.test_client().post("/api/clear_tasks", json={"task_ids": ["a", "b"]})

        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "cleared": 2, "skipped": 0}
        assert "scope" not in calls

    def test_misc_recover_candidates_returns_runtime_payload(self):
        from flask import Flask
        from src.routes import misc

        app = Flask(__name__)
        misc.init_blueprint({
            "download_dir": "/tmp",
            "format_size_func": lambda size: str(size),
            "query_task_history_func": lambda *_args: {},
            "get_download_status_func": lambda: {},
            "clear_all_tasks_func": lambda _scope: 0,
            "get_recovery_candidates_func": lambda: [],
            "recover_candidates_func": lambda task_ids: {"ok": True, "submitted": task_ids, "errors": {}},
            "abort_debug_func": lambda: None,
            "resolve_download_path_func": lambda *_args, **_kwargs: "/tmp",
            "clear_task_ids_func": lambda task_ids: {"ok": True, "cleared": len(task_ids), "skipped": 0},
            "debug_service": Mock(),
        })
        app.register_blueprint(misc.bp)

        response = app.test_client().post("/api/recover_candidates", json={"task_ids": ["t1"]})

        assert response.status_code == 200
        assert response.get_json() == {"ok": True, "submitted": ["t1"], "errors": {}}

    def _make_system_client(self, connected):
        from flask import Flask
        from src.routes import system

        system.init_blueprint({
            "ensure_tg_conn_func": lambda allow_reconnect=True: None,
            "get_tg_connected_func": lambda: connected,
            "get_tg_error_func": lambda: "",
            "get_tg_user_func": lambda: "user",
            "get_queue_func": lambda: {"active": 0},
            "get_tdl_func": lambda: {"active": 0},
            "proxy_config": None,
            "tdl_binary": "/missing/tdl",
        })
        app = Flask(__name__)
        app.register_blueprint(system.bp)
        return app.test_client()

    def test_health_live_always_ok(self):
        client = self._make_system_client(connected=False)
        response = client.get("/api/health/live")
        assert response.status_code == 200
        assert response.get_json()["status"] == "alive"

    def test_health_ready_reflects_connection(self):
        ok = self._make_system_client(connected=True).get("/api/health/ready")
        assert ok.status_code == 200
        assert ok.get_json()["ready"] is True

        down = self._make_system_client(connected=False).get("/api/health/ready")
        assert down.status_code == 503
        assert down.get_json()["ready"] is False
        assert "telegram" in down.get_json()["degraded"]

    def _make_relay_client(self, secret, verify=None, media=None):
        from flask import Flask
        from werkzeug.routing import BaseConverter
        from src.routes import relay

        class _SignedIntConverter(BaseConverter):
            regex = r"-?\d+"

            def to_python(self, value):
                return int(value)

            def to_url(self, value):
                return str(int(value))

        relay.active_relays = 0
        relay.init_blueprint({
            "relay_token_secret": secret,
            "max_concurrent_relays": 2,
            "verify_relay_token_func": verify or (lambda **_kwargs: None),
            "get_relay_media_func": media or (lambda *_args: {"file_name": "v.mp4", "size": 10}),
            "parse_range_func": lambda _range, total: (0, max(0, total - 1), 200),
            "iter_relay_bytes_func": lambda *_args: iter([b"data"]),
            "log_warning_func": lambda *_a, **_k: None,
            "log_info_func": lambda *_a, **_k: None,
            "log_error_func": lambda *_a, **_k: None,
        })
        app = Flask(__name__)
        app.url_map.converters["signed_int"] = _SignedIntConverter
        app.register_blueprint(relay.bp)
        return app, relay

    def test_relay_disabled_without_secret(self):
        app, _relay = self._make_relay_client(secret="")
        response = app.test_client().get("/relay/-100/42?file_name=v.mp4&token=x")
        assert response.status_code == 503

    def test_relay_missing_token_releases_slot(self):
        app, relay = self._make_relay_client(secret="s" * 32)
        response = app.test_client().get("/relay/-100/42?file_name=v.mp4")
        assert response.status_code == 400
        # 早退路径必须释放并发槽位（原实现在此泄漏）
        assert relay.active_relays == 0

    def test_relay_invalid_token_forbidden_and_releases_slot(self):
        def _bad_verify(**_kwargs):
            raise ValueError("invalid token signature")

        app, relay = self._make_relay_client(secret="s" * 32, verify=_bad_verify)
        response = app.test_client().get("/relay/-100/42?file_name=v.mp4&token=bad")
        assert response.status_code == 403
        assert relay.active_relays == 0

