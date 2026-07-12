# ==================== 下载监控看门狗 ====================
# 此代码需要插入到 app.py 中的全局变量区域之后

class DownloadWatchdog:
    """
    下载监控看门狗
    - 检测下载任务是否停滞（长时间无进度）
    - 自动重启卡死的下载任务
    """
    def __init__(self, check_interval=60, stall_timeout=300):
        self.check_interval = check_interval  # 检查间隔（秒）
        self.stall_timeout = stall_timeout    # 无进度超时（秒）
        self.last_progress = {}               # {task_id: {'bytes': int, 'time': float}}
        self.running = False
        self.thread = None
        
    def start(self):
        """启动监控线程"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()
        log_info(f"[watchdog] 下载监控已启动 (检查间隔:{self.check_interval}s, 超时阈值:{self.stall_timeout}s)")
    
    def stop(self):
        """停止监控"""
        self.running = False
        
    def _monitor_loop(self):
        """监控主循环"""
        while self.running:
            try:
                time.sleep(self.check_interval)
                self._check_all_tasks()
            except Exception as e:
                log_error(f"[watchdog] 监控异常: {e}")
    
    def _check_all_tasks(self):
        """检查所有下载任务"""
        current_time = time.time()
        
        with status_lock:
            tasks_snapshot = list(download_status.items())
        
        for task_id, task in tasks_snapshot:
            # 只监控正在下载的任务
            if task.get('status') != 'downloading':
                # 清理非下载状态的监控记录
                self.last_progress.pop(task_id, None)
                continue
            
            current_bytes = task.get('downloaded_bytes', 0)
            
            # 初次见到此任务，记录初始状态
            if task_id not in self.last_progress:
                self.last_progress[task_id] = {
                    'bytes': current_bytes,
                    'time': current_time
                }
                continue
            
            last_info = self.last_progress[task_id]
            elapsed = current_time - last_info['time']
            bytes_diff = current_bytes - last_info['bytes']
            
            # 检查是否停滞
            if bytes_diff == 0 and elapsed > self.stall_timeout:
                log_warning(
                    f"[watchdog] 任务 {task_id} 已停滞 {elapsed:.0f}s "
                    f"(进度: {task.get('progress', 0)}%, "
                    f"已下载: {task.get('downloaded', '0B')}), "
                    f"触发自动重启"
                )
                self._restart_stuck_task(task_id, task)
                # 清理监控记录，等待重启后重新监控
                self.last_progress.pop(task_id, None)
                
            elif bytes_diff > 0:
                # 有进度，更新记录
                self.last_progress[task_id] = {
                    'bytes': current_bytes,
                    'time': current_time
                }
    
    def _restart_stuck_task(self, task_id, task):
        """重启卡死的任务"""
        try:
            entity_id = task.get('entity_id')
            msg_id = task.get('msg_id')
            dialog_name = task.get('dialog_name', '')
            
            if not entity_id or not msg_id:
                log_error(f"[watchdog] 任务 {task_id} 缺少必要信息，无法重启")
                return
            
            # 标记任务为错误状态（触发重启前先中断当前下载）
            with status_lock:
                if task_id in download_status:
                    download_status[task_id]['status'] = 'error'
                    download_status[task_id]['error'] = 'watchdog 检测到停滞，自动重启中...'
            
            # 标记取消（中断可能卡住的下载线程）
            _mark_download_cancelled(task_id)
            
            # 等待一小段时间让线程响应取消信号
            time.sleep(2)
            
            # 清除取消标记，准备重启
            _clear_download_cancelled(task_id)
            
            # 调用恢复任务函数（支持断点续传）
            log_info(f"[watchdog] 正在重启任务 {task_id}...")
            result = _resume_task(task_id, dialog_name=dialog_name, auto=True)
            
            if result.get('ok'):
                log_info(f"[watchdog] 任务 {task_id} 重启成功")
            else:
                log_error(f"[watchdog] 任务 {task_id} 重启失败: {result.get('error', 'unknown')}")
                
        except Exception as e:
            log_error(f"[watchdog] 重启任务 {task_id} 时异常: {e}")


# 全局看门狗实例
download_watchdog = DownloadWatchdog(
    check_interval=60,      # 每 60 秒检查一次
    stall_timeout=300       # 5 分钟无进度视为停滞
)
