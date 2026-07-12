# 项目下载说明

## 📦 包含内容

本压缩包包含优化后的 Telegram 视频下载器完整源码：

### 核心文件
- `app_new.py` - 模块化 Flask 装配入口
- `app.py` - 兼容 runtime，承载渐进迁出的真实依赖
- `src/` - 模块化业务代码
- `templates/` / `static/` - Web UI 资源
- `healthcheck.sh` - Docker 健康检查脚本
- `requirements.txt` - Python 依赖
- `config.py` - 配置文件模板

### 补丁文件 (patches/)
- `watchdog_patch.py` - 方案1：下载监控代码
- `telegram_health_check.py` - 方案2：Telegram 健康检查代码
- `apply_watchdog.py` - 方案1部署脚本
- `apply_health_checks.py` - 方案2+4部署脚本

### 文档 (docs/)
- `OPTIMIZATION_COMPLETE.md` - 完整优化报告
- `WATCHDOG_OPTIMIZATION.md` - 方案1详细文档

## 🚀 使用方法

### 方式1: 解压查看源码
```bash
tar -xzf tg-downloader-optimized.tar.gz
cd tg-downloader-optimized
cat README.md
```

### 方式2: 部署到现有容器
```bash
# 解压
tar -xzf tg-downloader-optimized.tar.gz
cd tg-downloader-optimized

# 部署
docker cp app.py <容器ID>:/app/app.py
docker cp app_new.py <容器ID>:/app/app_new.py
docker cp src <容器ID>:/app/src
docker cp templates <容器ID>:/app/templates
docker cp static <容器ID>:/app/static
docker cp healthcheck.sh <容器ID>:/app/healthcheck.sh
docker restart <容器ID>
```

### 方式3: 全新部署
1. 解压文件
2. 配置 `config.py` 中的 API 密钥
3. 安装依赖：`pip install -r requirements.txt`
4. 运行：`python3 app_new.py`

## 📊 优化内容

✅ **方案1**: 自动检测下载停滞并重启（5分钟）  
✅ **方案2**: Telegram 连接健康检查（2分钟）  
✅ **方案4**: Docker 容器健康检查  

## 📞 技术支持

详细文档请查看：
- `README.md` - 项目说明
- `docs/OPTIMIZATION_COMPLETE.md` - 完整优化报告

---

**优化版本**: v1.0-optimized  
**打包时间**: 2026-06-21 19:17  
**文件大小**: 42KB (压缩)，220KB (解压)
