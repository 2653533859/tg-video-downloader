"""
Prometheus 监控指标导出
提供下载速度、队列长度、任务状态等监控指标
"""
from typing import Dict, Any
import time
import threading


class MetricsCollector:
    """监控指标收集器"""

    def __init__(self):
        self._lock = threading.Lock()
        self._metrics = {
            # 任务计数
            "tasks_total": 0,
            "tasks_downloading": 0,
            "tasks_queued": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "tasks_cancelled": 0,

            # 下载统计
            "download_speed_bps": 0.0,
            "total_downloaded_bytes": 0,
            "total_uploaded_bytes": 0,

            # 队列统计
            "queue_length": 0,
            "queue_wait_time_seconds": 0.0,

            # 错误统计
            "errors_total": 0,
            "errors_telegram_connection": 0,
            "errors_download_timeout": 0,
            "errors_file_write": 0,

            # 系统状态
            "telegram_connected": 0,
            "relay_connected": 0,
            "watchdog_restarts": 0,
            "health_check_failures": 0,

            # 性能指标
            "active_downloads": 0,
            "concurrent_relays": 0,
            "cache_hits": 0,
            "cache_misses": 0,

            # 时间戳
            "last_update": time.time(),
        }

        # 历史数据（用于计算速率）
        self._history = {
            "download_bytes_samples": [],
            "error_samples": [],
            "task_completion_times": [],
        }

    def increment(self, metric: str, value: float = 1.0):
        """增加指标值"""
        with self._lock:
            if metric in self._metrics:
                self._metrics[metric] += value
                self._metrics["last_update"] = time.time()

    def set_gauge(self, metric: str, value: float):
        """设置瞬时值（gauge）"""
        with self._lock:
            if metric in self._metrics:
                self._metrics[metric] = value
                self._metrics["last_update"] = time.time()

    def record_download_speed(self, speed_bps: float):
        """记录下载速度"""
        with self._lock:
            self._metrics["download_speed_bps"] = speed_bps
            self._metrics["last_update"] = time.time()

    def record_task_state_change(self, old_status: str, new_status: str):
        """记录任务状态变化"""
        with self._lock:
            # 减少旧状态计数
            if old_status:
                old_key = f"tasks_{old_status}"
                if old_key in self._metrics:
                    self._metrics[old_key] = max(0, self._metrics[old_key] - 1)

            # 增加新状态计数
            if new_status:
                new_key = f"tasks_{new_status}"
                if new_key in self._metrics:
                    self._metrics[new_key] += 1

            self._metrics["last_update"] = time.time()

    def record_error(self, error_type: str = "general"):
        """记录错误"""
        with self._lock:
            self._metrics["errors_total"] += 1

            error_key = f"errors_{error_type}"
            if error_key in self._metrics:
                self._metrics[error_key] += 1

            # 记录到历史
            self._history["error_samples"].append(time.time())
            # 只保留最近1小时的样本
            cutoff = time.time() - 3600
            self._history["error_samples"] = [
                t for t in self._history["error_samples"] if t > cutoff
            ]

            self._metrics["last_update"] = time.time()

    def record_task_completion(self, duration_seconds: float):
        """记录任务完成时间"""
        with self._lock:
            self._history["task_completion_times"].append(duration_seconds)
            # 只保留最近100个样本
            if len(self._history["task_completion_times"]) > 100:
                self._history["task_completion_times"] = \
                    self._history["task_completion_times"][-100:]

    def get_metrics(self) -> Dict[str, Any]:
        """获取所有指标（快照）"""
        with self._lock:
            return dict(self._metrics)

    def get_metrics_prometheus_format(self) -> str:
        """
        导出 Prometheus 格式的指标

        Returns:
            Prometheus text format 字符串
        """
        with self._lock:
            lines = []

            # Counter 类型指标
            counters = [
                ("tasks_total", "Total number of tasks created"),
                ("tasks_completed", "Total number of completed tasks"),
                ("tasks_failed", "Total number of failed tasks"),
                ("tasks_cancelled", "Total number of cancelled tasks"),
                ("errors_total", "Total number of errors"),
                ("errors_telegram_connection", "Telegram connection errors"),
                ("errors_download_timeout", "Download timeout errors"),
                ("errors_file_write", "File write errors"),
                ("watchdog_restarts", "Number of watchdog restarts"),
                ("health_check_failures", "Number of health check failures"),
                ("total_downloaded_bytes", "Total bytes downloaded"),
                ("cache_hits", "Total cache hits"),
                ("cache_misses", "Total cache misses"),
            ]

            for metric, help_text in counters:
                value = self._metrics.get(metric, 0)
                lines.append(f"# HELP tg_downloader_{metric} {help_text}")
                lines.append(f"# TYPE tg_downloader_{metric} counter")
                lines.append(f"tg_downloader_{metric} {value}")
                lines.append("")

            # Gauge 类型指标
            gauges = [
                ("tasks_downloading", "Number of currently downloading tasks"),
                ("tasks_queued", "Number of queued tasks"),
                ("download_speed_bps", "Current download speed in bytes per second"),
                ("queue_length", "Current queue length"),
                ("queue_wait_time_seconds", "Average queue wait time in seconds"),
                ("telegram_connected", "Telegram connection status (1=connected, 0=disconnected)"),
                ("relay_connected", "Relay connection status (1=connected, 0=disconnected)"),
                ("active_downloads", "Number of active downloads"),
                ("concurrent_relays", "Number of concurrent relay streams"),
            ]

            for metric, help_text in gauges:
                value = self._metrics.get(metric, 0)
                lines.append(f"# HELP tg_downloader_{metric} {help_text}")
                lines.append(f"# TYPE tg_downloader_{metric} gauge")
                lines.append(f"tg_downloader_{metric} {value}")
                lines.append("")

            # 计算错误率（最近1小时）
            error_count = len(self._history.get("error_samples", []))
            error_rate = error_count / 3600.0 if error_count > 0 else 0.0
            lines.append("# HELP tg_downloader_error_rate_per_second Error rate per second (1 hour window)")
            lines.append("# TYPE tg_downloader_error_rate_per_second gauge")
            lines.append(f"tg_downloader_error_rate_per_second {error_rate:.6f}")
            lines.append("")

            # 平均任务完成时间
            completion_times = self._history.get("task_completion_times", [])
            if completion_times:
                avg_time = sum(completion_times) / len(completion_times)
                lines.append("# HELP tg_downloader_avg_task_duration_seconds Average task completion time")
                lines.append("# TYPE tg_downloader_avg_task_duration_seconds gauge")
                lines.append(f"tg_downloader_avg_task_duration_seconds {avg_time:.2f}")
                lines.append("")

            return "\n".join(lines)

    def reset_counters(self):
        """重置计数器（用于测试）"""
        with self._lock:
            for key in self._metrics:
                if "total" in key or "errors_" in key:
                    self._metrics[key] = 0
            self._history = {
                "download_bytes_samples": [],
                "error_samples": [],
                "task_completion_times": [],
            }


# 全局指标收集器实例
metrics = MetricsCollector()


def update_task_metrics(task_states: Dict[str, Any]):
    """
    根据任务状态更新指标

    Args:
        task_states: 任务状态字典 {task_id: state}
    """
    status_counts = {
        "downloading": 0,
        "queued": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
    }

    total_speed = 0.0
    active_count = 0

    for task_id, state in task_states.items():
        status = state.get("status", "")
        if status in status_counts:
            status_counts[status] += 1

        if status == "downloading":
            speed_bps = state.get("speed_bps", 0.0)
            if speed_bps > 0:
                total_speed += speed_bps
                active_count += 1

    # 更新 gauge 指标
    metrics.set_gauge("tasks_downloading", status_counts["downloading"])
    metrics.set_gauge("tasks_queued", status_counts["queued"])
    metrics.set_gauge("active_downloads", active_count)

    # 更新平均下载速度
    if active_count > 0:
        metrics.set_gauge("download_speed_bps", total_speed / active_count)
    else:
        metrics.set_gauge("download_speed_bps", 0.0)


def update_queue_metrics(queue_length: int, avg_wait_time: float = 0.0):
    """
    更新队列指标

    Args:
        queue_length: 队列长度
        avg_wait_time: 平均等待时间（秒）
    """
    metrics.set_gauge("queue_length", queue_length)
    metrics.set_gauge("queue_wait_time_seconds", avg_wait_time)


def update_connection_metrics(telegram_connected: bool, relay_connected: bool):
    """
    更新连接状态指标

    Args:
        telegram_connected: Telegram 连接状态
        relay_connected: Relay 连接状态
    """
    metrics.set_gauge("telegram_connected", 1 if telegram_connected else 0)
    metrics.set_gauge("relay_connected", 1 if relay_connected else 0)
