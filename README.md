# Telegram 视频下载器

基于 Flask + Telethon 的 Telegram 视频下载 Web 应用：浏览频道/群组、队列下载、断点续传、在线播放，内置下载看门狗和连接健康监控。

> 架构与开发文档见 `CLAUDE.md`（2026-07-13 更新，与代码同步）；优化任务清单见 `docs/Task.md`。

## 功能

- **Web UI**：浏览/搜索频道视频、缩略图预览、回复查看
- **两种下载方式**：Telethon 直连（默认）、tdl 外部二进制（特定频道自动回退）
- **队列管理**：排序、取消、重试；SQLite 持久化，重启后自动恢复未完成任务
- **断点续传**：按字节偏移续传，配合看门狗自动重启停滞任务
- **在线播放**：HMAC 签名的 relay URL，外部播放器可直接拉流
- **监控**：下载看门狗（5 分钟停滞检测）、Telegram 连接健康检查（2 分钟间隔）、Docker HEALTHCHECK

## 快速开始

### Docker Compose（推荐）

```bash
# 1. 配置环境变量
cp .env.example .env
vim .env          # 填写 TG_API_ID、TG_API_HASH、认证信息

# 2. 首次登录生成 session（宿主机执行）
python3 login.py

# 3. 启动
docker compose up -d --build

# 4. 访问 Web UI
# http://localhost:5003
```

### 本地运行

```bash
pip install -r requirements.txt

export TG_API_ID=你的API_ID
export TG_API_HASH=你的API_HASH
export WEB_AUTH_USERNAME=用户名
export WEB_AUTH_PASSWORD=密码

python3 login.py     # 首次登录
python3 app.py       # 启动（与 python3 app_new.py 等价，实际 serve Blueprint 应用）
```

## 项目结构

```
tg-downloader-optimized/
├── app.py              # 运行时模块 + 启动器（__main__ 委托 app_new）
├── app_new.py          # Blueprint 装配入口
├── config.py           # 环境变量配置
├── login.py            # 首次登录工具（生成 session）
├── relay_tokens.py     # Relay token 签名/验证
├── src/                # 模块化代码（routes/download/telegram/state/files/security/...）
├── templates/ static/  # 前端
├── tests/              # pytest 测试套件
├── docs/               # 文档（含 Task.md 任务清单、CHANGELOG.md、前端/WS 指南）
└── Dockerfile / docker-compose.yml / healthcheck.sh
```

## 配置

主要环境变量（完整列表见 `.env.example` 和 `CLAUDE.md`）：

```bash
# 必需
TG_API_ID=...
TG_API_HASH=...
WEB_AUTH_USERNAME=...      # 非本地绑定必须配置（fail-closed）
WEB_AUTH_PASSWORD=...

# 常用可选
WEB_BIND_PORT=5003         # 默认 5003
TG_PROXY_ENABLED=false
TG_PROXY_TYPE=http         # 仅支持 http
RELAY_TOKEN_SECRET=...     # 空 = 禁用在线播放
PUBLIC_BASE_URL=http://localhost:5003
TRUST_FORWARDED_FOR=false  # 仅在可信反向代理后设 true
```

## 测试

```bash
pip install -r requirements-dev.txt
PYTHONPATH=. python3 -m pytest -q
```

## 故障排查

```bash
# 监控是否启动
docker compose logs tg-downloader | grep -E "watchdog|tg-health"

# 健康检查
docker compose exec tg-downloader sh /app/healthcheck.sh

# 任务状态库
sqlite3 .task_state/tasks.sqlite3 "SELECT task_id, json_extract(state_json,'$.status') FROM task_states;"

# 应用日志
tail -f logs/app.log
```

## 安全说明

- `.env`、`*.session` 已被 `.gitignore` / `.dockerignore` 排除，**不要**提交或打进镜像
- 非本地绑定强制 Basic Auth；`X-Forwarded-For` 默认不被信任（防伪造绕过）
- relay URL 由 HMAC-SHA256 签名 token 保护，默认 TTL 30 分钟
