# ==================== Telegram 连接健康检查 ====================
# 此代码需要添加到 app.py 中

import asyncio
import threading
import time

class TelegramHealthChecker:
    """
    Telegram 连接健康检查
    - 定期检查 Telegram 客户端连接状态
    - 发现异常时主动重连
    - 减少下载卡死的概率
    """
    def __init__(self, client, loop, check_interval=120, max_retry=3):
        self.client = client                # TelegramClient 实例
        self.loop = loop                    # asyncio event loop
        self.check_interval = check_interval  # 检查间隔（秒）
        self.max_retry = max_retry          # 最大重试次数
        self.running = False
        self.thread = None
        self.last_check_ok = True
        self.consecutive_failures = 0
        
    def start(self):
        """启动健康检查线程"""
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._check_loop, daemon=True)
        self.thread.start()
        log_info(f"[tg-health] Telegram 连接健康检查已启动 (间隔:{self.check_interval}s)")
    
    def stop(self):
        """停止健康检查"""
        self.running = False
        
    def _check_loop(self):
        """健康检查主循环"""
        while self.running:
            try:
                time.sleep(self.check_interval)
                self._perform_health_check()
            except Exception as e:
                log_error(f"[tg-health] 健康检查异常: {e}")
    
    def _perform_health_check(self):
        """执行健康检查"""
        try:
            # 使用 asyncio.run_coroutine_threadsafe 在事件循环中执行异步检查
            future = asyncio.run_coroutine_threadsafe(
                self._async_health_check(),
                self.loop
            )
            # 等待结果，设置超时
            result = future.result(timeout=30)
            
            if result:
                if not self.last_check_ok:
                    log_info("[tg-health] Telegram 连接已恢复正常")
                self.last_check_ok = True
                self.consecutive_failures = 0
            else:
                self._handle_check_failure()
                
        except Exception as e:
            log_error(f"[tg-health] 健康检查执行失败: {e}")
            self._handle_check_failure()
    
    async def _async_health_check(self):
        """异步健康检查（轻量级操作）"""
        try:
            # 检查客户端是否已连接
            if not self.client.is_connected():
                log_warning("[tg-health] 客户端未连接")
                return False
            
            # 尝试获取一个对话（轻量级操作）
            await asyncio.wait_for(
                self.client.get_dialogs(limit=1),
                timeout=10.0
            )
            return True
            
        except asyncio.TimeoutError:
            log_warning("[tg-health] 健康检查超时")
            return False
        except Exception as e:
            log_warning(f"[tg-health] 健康检查失败: {e}")
            return False
    
    def _handle_check_failure(self):
        """处理检查失败"""
        self.consecutive_failures += 1
        self.last_check_ok = False
        
        log_warning(
            f"[tg-health] Telegram 连接异常 "
            f"(连续失败: {self.consecutive_failures}/{self.max_retry})"
        )
        
        # 连续失败达到阈值，触发重连
        if self.consecutive_failures >= self.max_retry:
            log_warning("[tg-health] 触发 Telegram 重连")
            self._trigger_reconnect()
            self.consecutive_failures = 0
    
    def _trigger_reconnect(self):
        """触发 Telegram 重连"""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._async_reconnect(),
                self.loop
            )
            future.result(timeout=30)
        except Exception as e:
            log_error(f"[tg-health] 重连失败: {e}")
    
    async def _async_reconnect(self):
        """异步重连"""
        try:
            log_info("[tg-health] 正在断开连接...")
            await self.client.disconnect()
            await asyncio.sleep(5)
            
            log_info("[tg-health] 正在重新连接...")
            await self.client.connect()
            
            if self.client.is_connected():
                log_info("[tg-health] Telegram 重连成功")
                global tg_connected
                tg_connected = True
            else:
                log_error("[tg-health] Telegram 重连失败")
                
        except Exception as e:
            log_error(f"[tg-health] 重连过程异常: {e}")


# 全局健康检查实例（需要在 tg_client 初始化后创建）
tg_health_checker = None

def init_tg_health_checker():
    """初始化 Telegram 健康检查器"""
    global tg_health_checker
    if tg_health_checker is None:
        tg_health_checker = TelegramHealthChecker(
            client=tg_client,
            loop=tg_loop,
            check_interval=120,    # 每 2 分钟检查一次
            max_retry=3            # 连续 3 次失败触发重连
        )
        tg_health_checker.start()
