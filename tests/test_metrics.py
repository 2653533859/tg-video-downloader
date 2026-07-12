"""
测试 metrics 模块
"""
import pytest
import time
from metrics import MetricsCollector, metrics, update_task_metrics, update_queue_metrics, update_connection_metrics


class TestMetricsCollector:
    """指标收集器测试"""

    @pytest.fixture
    def collector(self):
        """创建新的收集器实例"""
        collector = MetricsCollector()
        yield collector
        collector.reset_counters()

    def test_increment_counter(self, collector):
        """测试增加计数器"""
        initial = collector.get_metrics()["tasks_total"]
        collector.increment("tasks_total", 1)
        assert collector.get_metrics()["tasks_total"] == initial + 1

    def test_set_gauge(self, collector):
        """测试设置瞬时值"""
        collector.set_gauge("download_speed_bps", 1024000)
        assert collector.get_metrics()["download_speed_bps"] == 1024000

    def test_record_download_speed(self, collector):
        """测试记录下载速度"""
        collector.record_download_speed(2048000)
        assert collector.get_metrics()["download_speed_bps"] == 2048000

    def test_record_task_state_change(self, collector):
        """测试记录任务状态变化"""
        # 初始状态
        collector.set_gauge("tasks_queued", 0)
        collector.set_gauge("tasks_downloading", 0)

        # 从 queued 变为 downloading
        collector.record_task_state_change("queued", "downloading")

        metrics_data = collector.get_metrics()
        assert metrics_data["tasks_downloading"] == 1

    def test_record_error(self, collector):
        """测试记录错误"""
        initial = collector.get_metrics()["errors_total"]
        collector.record_error("telegram_connection")

        metrics_data = collector.get_metrics()
        assert metrics_data["errors_total"] == initial + 1
        assert metrics_data["errors_telegram_connection"] >= 1

    def test_record_task_completion(self, collector):
        """测试记录任务完成时间"""
        collector.record_task_completion(120.5)
        collector.record_task_completion(90.3)

        # 验证历史记录
        metrics_data = collector.get_metrics()
        # 应该不会报错

    def test_prometheus_format_output(self, collector):
        """测试 Prometheus 格式输出"""
        collector.set_gauge("tasks_downloading", 3)
        collector.increment("tasks_completed", 10)
        collector.record_download_speed(1024000)

        output = collector.get_metrics_prometheus_format()

        # 验证格式
        assert "# HELP" in output
        assert "# TYPE" in output
        assert "tg_downloader_tasks_downloading 3" in output
        assert "tg_downloader_tasks_completed 10" in output
        assert "tg_downloader_download_speed_bps 1024000" in output

    def test_error_rate_calculation(self, collector):
        """测试错误率计算"""
        # 记录一些错误
        for _ in range(5):
            collector.record_error()

        output = collector.get_metrics_prometheus_format()
        assert "tg_downloader_error_rate_per_second" in output

    def test_average_task_duration(self, collector):
        """测试平均任务时长计算"""
        collector.record_task_completion(100.0)
        collector.record_task_completion(200.0)
        collector.record_task_completion(150.0)

        output = collector.get_metrics_prometheus_format()
        assert "tg_downloader_avg_task_duration_seconds" in output
        # 平均值应该是 150
        assert "150.00" in output

    def test_reset_counters(self, collector):
        """测试重置计数器"""
        collector.increment("tasks_total", 10)
        collector.increment("errors_total", 5)

        collector.reset_counters()

        metrics_data = collector.get_metrics()
        assert metrics_data["tasks_total"] == 0
        assert metrics_data["errors_total"] == 0

    def test_concurrent_access(self, collector):
        """测试并发访问（线程安全）"""
        import threading

        def increment_worker():
            for _ in range(100):
                collector.increment("tasks_total")

        threads = [threading.Thread(target=increment_worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 应该正确累加
        assert collector.get_metrics()["tasks_total"] == 500


class TestMetricsHelpers:
    """指标辅助函数测试"""

    def test_update_task_metrics(self):
        """测试更新任务指标"""
        task_states = {
            "task_1": {"status": "downloading", "speed_bps": 1024000},
            "task_2": {"status": "downloading", "speed_bps": 2048000},
            "task_3": {"status": "queued"},
            "task_4": {"status": "completed"},
        }

        update_task_metrics(task_states)

        metrics_data = metrics.get_metrics()
        assert metrics_data["tasks_downloading"] == 2
        assert metrics_data["tasks_queued"] == 1
        assert metrics_data["active_downloads"] == 2

    def test_update_queue_metrics(self):
        """测试更新队列指标"""
        update_queue_metrics(queue_length=5, avg_wait_time=30.5)

        metrics_data = metrics.get_metrics()
        assert metrics_data["queue_length"] == 5
        assert metrics_data["queue_wait_time_seconds"] == 30.5

    def test_update_connection_metrics(self):
        """测试更新连接状态指标"""
        update_connection_metrics(telegram_connected=True, relay_connected=False)

        metrics_data = metrics.get_metrics()
        assert metrics_data["telegram_connected"] == 1
        assert metrics_data["relay_connected"] == 0

    def test_update_connection_metrics_disconnected(self):
        """测试连接断开状态"""
        update_connection_metrics(telegram_connected=False, relay_connected=False)

        metrics_data = metrics.get_metrics()
        assert metrics_data["telegram_connected"] == 0
        assert metrics_data["relay_connected"] == 0
