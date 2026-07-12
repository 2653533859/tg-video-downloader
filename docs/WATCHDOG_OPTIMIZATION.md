# Telegram 下载器 - 下载监控看门狗优化方案

## 📋 优化概述

**问题**: Telegram 下载任务在长时间下载时会因网络波动、API 超时等原因卡死，导致进度停滞但状态显示正常。

**解决方案**: 实施方案 1 - 添加下载超时监控（DownloadWatchdog）

## ✅ 已实施功能

### 1. 自动监控机制
- **监控间隔**: 每 60 秒检查一次
- **超时阈值**: 5 分钟（300秒）无进度视为停滞
- **监控范围**: 所有状态为 `downloading` 的任务

### 2. 智能重启机制
- 检测到停滞后自动触发重启
- 利用项目已有的断点续传功能
- 优雅中断：先标记取消 → 等待响应 → 清除标记 → 重启任务

### 3. 日志记录
- 启动日志：`[watchdog] 下载监控已启动`
- 检测日志：`[watchdog] 任务 xxx 已停滞 xxxs，触发自动重启`
- 结果日志：`[watchdog] 任务 xxx 重启成功/失败`

## 📁 文件变更

### 修改的文件
- `app.py` - 主程序文件（已备份为 `app.py.bak.20260621190603`）

### 新增的文件
- `watchdog_patch.py` - 监控代码补丁
- `apply_watchdog.py` - 自动应用补丁脚本
- `WATCHDOG_OPTIMIZATION.md` - 本文档

## 🔧 技术实现

### 核心类：DownloadWatchdog

```python
class DownloadWatchdog:
    """下载监控看门狗"""
    - check_interval: 60s  # 检查间隔
    - stall_timeout: 300s  # 停滞超时
    - last_progress: {}    # 进度跟踪字典
```

### 监控逻辑

1. **进度跟踪**
   - 首次见到任务：记录初始字节数和时间
   - 后续检查：对比字节数变化

2. **停滞判定**
   ```
   if (当前字节数 == 上次字节数) AND (经过时间 > 300s):
       触发重启
   ```

3. **重启流程**
   ```
   标记任务为 error 状态
   → 设置取消标记 (_mark_download_cancelled)
   → 等待 2 秒（让下载线程响应）
   → 清除取消标记
   → 调用 _resume_task(auto=True)
   ```

## 📊 效果验证

### 启动验证
```bash
docker logs 65e79462bf76 | grep watchdog
```
预期输出：
```
[watchdog] 下载监控已启动 (检查间隔:60s, 超时阈值:300s)
```

### 运行时监控
```bash
# 查看当前下载状态
curl -u <用户名>:<密码> http://127.0.0.1:5003/api/download_status

# 查看日志
docker logs -f 65e79462bf76 | grep watchdog
```

## 🎯 预期效果

### 优化前
- 下载卡死后需要**手动重启容器**
- 数据保留在断点，但需人工干预
- 用户体验差

### 优化后
- **自动检测**停滞任务（5分钟）
- **自动重启**卡死的下载
- **无需人工干预**
- **断点续传**，不浪费已下载数据

## ⚙️ 配置调整

如需调整监控参数，编辑 `app.py` 第 120 行附近：

```python
download_watchdog = DownloadWatchdog(
    check_interval=60,      # 检查间隔（秒）- 可改为 30/120
    stall_timeout=300       # 超时阈值（秒）- 可改为 180/600
)
```

### 参数建议
- **快速响应**：`check_interval=30, stall_timeout=180`
- **当前配置**：`check_interval=60, stall_timeout=300` ✅
- **保守模式**：`check_interval=120, stall_timeout=600`

## 🔄 回滚方案

如果遇到问题，可快速回滚：

```bash
cd ~/tg-video-downloader-aria2
docker cp app.py.bak.20260621190603 65e79462bf76:/app/app.py
docker restart 65e79462bf76
```

## 📈 未来优化方向

### 短期（已完成）
- ✅ 下载超时监控
- ✅ 自动重启机制

### 中期（可选）
- Telegram 连接健康检查
- 多次重启失败后的降级策略
- 监控指标导出（Prometheus）

### 长期（可选）
- 下载任务分块策略
- 多客户端负载均衡
- 智能速度优化

## 📝 维护说明

### 日常维护
- 无需特殊维护，监控自动运行
- 定期查看日志中的 `[watchdog]` 条目

### 故障排查
1. 检查监控是否启动：`docker logs 65e79462bf76 | grep "下载监控已启动"`
2. 查看是否有重启记录：`docker logs 65e79462bf76 | grep "已停滞"`
3. 如频繁重启，考虑调大 `stall_timeout`

## 🎉 部署时间

- **开发时间**: 2026-06-21
- **部署时间**: 2026-06-21 19:06
- **容器ID**: 65e79462bf76
- **状态**: ✅ 运行中

## 📞 联系方式

如有问题或建议，请检查：
- 项目日志：`docker logs 65e79462bf76`
- 应用日志：`docker exec 65e79462bf76 tail -f /app/logs/app.log`

---

**优化完成！** 下载监控看门狗已成功集成到项目中。
