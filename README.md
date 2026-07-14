# Telegram 视频下载器

基于 Flask + Telethon 的 Telegram 视频下载 Web 应用：浏览频道/群组、队列下载、断点续传、在线播放，内置下载看门狗和连接健康监控。

> 架构与开发文档见 `CLAUDE.md`（2026-07-14 更新，与代码同步）；优化任务清单见 `docs/Task.md`。

## 功能

- **Web UI**：浏览/搜索频道视频、缩略图预览、回复查看
- **两种下载方式**：Telethon 直连（默认）、tdl 外部二进制（特定频道自动回退）
- **队列管理**：排序、取消、重试；SQLite 持久化，重启后自动恢复未完成任务
- **断点续传**：按字节偏移续传，配合看门狗自动重启停滞任务
- **在线播放**：HMAC 签名的 relay URL，外部播放器可直接拉流
- **混合鉴权**：网页会话登录（cookie）+ 保留 HTTP Basic Auth，healthcheck / API 客户端零改动
- **网页登录 Telegram**：session 缺失时进程不退出，浏览器内完成手机号 / 验证码 / 两步验证登录（无需 CLI）
- **监控**：下载看门狗（5 分钟停滞检测）、Telegram 连接健康检查（2 分钟间隔）、Docker HEALTHCHECK

## 快速开始

### Docker Compose（推荐）

```bash
# 1. 配置环境变量
cp .env.example .env
vim .env          # 填写 TG_API_ID、TG_API_HASH、认证信息

# 2. 启动
docker compose up -d --build

# 3. 首次登录 Telegram（二选一）
#    A（推荐）：浏览器打开 http://localhost:5003，用页面「登录 Telegram」向导完成
#    B：宿主机执行 python3 login.py 生成 session 后重启容器

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

python3 app.py       # 启动（与 python3 app_new.py 等价，实际 serve Blueprint 应用）
# 首次登录 Telegram：浏览器打开后用「登录 Telegram」向导，或先运行 python3 login.py
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
WEB_AUTH_USERNAME=...      # 混合鉴权凭据：同时用于 Basic Auth 与网页登录页；非本地绑定必须配置（fail-closed）
WEB_AUTH_PASSWORD=...

# 会话（网页登录）
WEB_SESSION_SECRET=...        # Flask 会话签名密钥；留空则由 WEB_AUTH_* 经 sha256 派生（重启不失效）
WEB_SESSION_COOKIE_SECURE=    # 会话 cookie Secure 标志；留空按 PUBLIC_BASE_URL 是否 https 推断，HTTPS 部署建议 true

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
- 混合鉴权：非本地绑定强制认证（网页会话登录或 Basic Auth 二者其一）；无凭据的非本地访问 fail-closed（403）
- `X-Forwarded-For` 默认不被信任（防伪造绕过认证）；会话 cookie 为 HttpOnly + SameSite=Lax，HTTPS 部署建议 `WEB_SESSION_COOKIE_SECURE=true`
- relay URL 由 HMAC-SHA256 签名 token 保护，默认 TTL 30 分钟
