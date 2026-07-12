/**
 * WebSocket 客户端 - 实时进度更新
 */

class ProgressWebSocket {
    constructor(url) {
        this.url = url;
        this.ws = null;
        this.reconnectDelay = 1000;
        this.maxReconnectDelay = 30000;
        this.reconnectAttempts = 0;
        this.listeners = new Map();
        this.isConnecting = false;
        this.isManualClose = false;
    }

    /**
     * 连接 WebSocket
     */
    connect() {
        if (this.isConnecting || (this.ws && this.ws.readyState === WebSocket.OPEN)) {
            console.log('[WS] 已连接或正在连接中');
            return;
        }

        this.isConnecting = true;
        this.isManualClose = false;

        try {
            console.log('[WS] 正在连接...', this.url);
            this.ws = new WebSocket(this.url);

            this.ws.onopen = () => {
                console.log('[WS] 连接成功');
                this.isConnecting = false;
                this.reconnectAttempts = 0;
                this.reconnectDelay = 1000;
                this.emit('connected');

                // 启动心跳
                this.startHeartbeat();
            };

            this.ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);
                    this.handleMessage(data);
                } catch (e) {
                    console.error('[WS] 解析消息失败:', e);
                }
            };

            this.ws.onerror = (error) => {
                console.error('[WS] 错误:', error);
                this.isConnecting = false;
                this.emit('error', error);
            };

            this.ws.onclose = (event) => {
                console.log('[WS] 连接关闭:', event.code, event.reason);
                this.isConnecting = false;
                this.stopHeartbeat();
                this.emit('disconnected');

                // 自动重连
                if (!this.isManualClose) {
                    this.scheduleReconnect();
                }
            };

        } catch (e) {
            console.error('[WS] 连接失败:', e);
            this.isConnecting = false;
            this.scheduleReconnect();
        }
    }

    /**
     * 处理消息
     */
    handleMessage(data) {
        const { type, task_id, data: payload } = data;

        console.log('[WS] 收到消息:', type, task_id);

        switch (type) {
            case 'init':
                // 初始化状态
                this.emit('init', payload);
                break;

            case 'progress_update':
                // 进度更新
                this.emit('progress', payload);
                break;

            case 'task_added':
                // 新任务添加
                this.emit('task_added', { task_id, data: payload });
                break;

            case 'task_completed':
                // 任务完成
                this.emit('task_completed', { task_id, data: payload });
                break;

            case 'task_error':
                // 任务错误
                this.emit('task_error', { task_id, data: payload });
                break;

            default:
                console.warn('[WS] 未知消息类型:', type);
        }
    }

    /**
     * 心跳机制
     */
    startHeartbeat() {
        this.heartbeatInterval = setInterval(() => {
            if (this.ws && this.ws.readyState === WebSocket.OPEN) {
                this.ws.send('ping');
            }
        }, 30000); // 30秒心跳
    }

    stopHeartbeat() {
        if (this.heartbeatInterval) {
            clearInterval(this.heartbeatInterval);
            this.heartbeatInterval = null;
        }
    }

    /**
     * 计划重连
     */
    scheduleReconnect() {
        this.reconnectAttempts++;

        // 指数退避
        const delay = Math.min(
            this.reconnectDelay * Math.pow(1.5, this.reconnectAttempts - 1),
            this.maxReconnectDelay
        );

        console.log(`[WS] ${delay / 1000}秒后重连 (第 ${this.reconnectAttempts} 次)`);

        setTimeout(() => {
            this.connect();
        }, delay);
    }

    /**
     * 断开连接
     */
    disconnect() {
        this.isManualClose = true;
        this.stopHeartbeat();

        if (this.ws) {
            this.ws.close();
            this.ws = null;
        }
    }

    /**
     * 监听事件
     */
    on(event, callback) {
        if (!this.listeners.has(event)) {
            this.listeners.set(event, []);
        }
        this.listeners.get(event).push(callback);
    }

    /**
     * 取消监听
     */
    off(event, callback) {
        if (!this.listeners.has(event)) return;

        const callbacks = this.listeners.get(event);
        const index = callbacks.indexOf(callback);

        if (index > -1) {
            callbacks.splice(index, 1);
        }
    }

    /**
     * 触发事件
     */
    emit(event, data) {
        if (!this.listeners.has(event)) return;

        const callbacks = this.listeners.get(event);
        callbacks.forEach(callback => {
            try {
                callback(data);
            } catch (e) {
                console.error('[WS] 事件回调错误:', e);
            }
        });
    }

    /**
     * 获取连接状态
     */
    isConnected() {
        return this.ws && this.ws.readyState === WebSocket.OPEN;
    }
}

// 导出
window.ProgressWebSocket = ProgressWebSocket;
