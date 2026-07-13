# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> 本文档描述 2026-07-13 P1-5 清理后的真实结构。历史文档中的行号和描述均已过时，以本文为准。

## Project Overview

Telegram 视频下载器：基于 Flask + Telethon 的 Web 应用，浏览 Telegram 频道/群组并下载视频，带下载队列持久化、断点续传、看门狗和连接健康监控。

**支持两种下载方式**（aria2 通道已于 2026-07-13 废弃移除）：
1. **Telethon 直连**（默认）：`src/download/telegram_downloader.py`
2. **tdl 外部二进制**：`src/download/tdl*.py`，对特定频道自动回退

## 入口结构（重要）

```
python3 app.py        # 生产入口（Dockerfile CMD）
python3 app_new.py    # 等价入口
```

两者**最终 serve 的都是 `app_new.app`（Blueprint 装配版）**：`app.py` 的 `__main__`（app.py:1932）会 `import app_new` 并调用 `app_new.app.run()`。

- `app.py`（约 1940 行）：**运行时模块**——持有全局状态、Telegram 客户端、下载调度等，大量委托 `src/` 模块。**不含任何路由定义**（P1-5b 已摘除历史死路由），全部路由在 `src/routes/` 的 Blueprint 中。
- `app_new.py`：Blueprint 装配层——把 `src/routes/` 的 6 个 Blueprint 用 `app.py` 的运行时函数注入初始化。
- **新增/修改路由一律在 `src/routes/` 中进行**，并在 `app_new.py` 的对应 `init_blueprint` 中注入依赖。

## src/ 模块地图

```
src/
├── download/    # 调度器(scheduler)、worker、看门狗(watchdog)、tdl 执行器、
│                # telethon 下载器、断点续传(resume)、路径(paths)、状态(status)
├── telegram/    # TelegramRuntime(runtime.py: run_async/重连/消息缓存)、
│                # health_checker、startup、video_service、debug_service
├── state/       # TaskStatePersistence(persistence.py: SQLite WAL) + manager
├── files/       # 文件服务(service.py: 路径校验/列表)、缩略图(thumbnails)
├── relay/       # Range 请求解析（在线播放用）
├── routes/      # 6 个 Blueprint: system/files/telegram/download/misc/relay
├── security/    # access.py(纯逻辑) + flask_access.py(Flask 适配)
├── system/      # startup(启动编排) + status(状态服务)
└── utils/       # formatting、validators
```

## 并发模型

- Flask `threaded=True` + 两个独立 asyncio 事件循环线程：主 Telegram 客户端（tg_loop）与 relay 客户端（relay_loop，StringSession 避免 session 文件锁冲突）
- **禁止**在 Flask 请求处理器中 `loop.run_until_complete()`；必须用 `run_async()` / `relay_run_async()`（app.py:872/879，底层是 `TelegramRuntime.run_async`，asyncio.run_coroutine_threadsafe + 超时）
- 下载并发：`MAX_CONCURRENT_DOWNLOADS = 1`（app.py:473，受 tdl 单实例 Bolt DB 约束）；relay 并发：`MAX_CONCURRENT_RELAYS = 2`
- 后台线程（全部 daemon）：队列 worker、DownloadWatchdog、TelegramHealthChecker（app.py:213 在主客户端连接后初始化）、缩略图清理、任务库备份。**优雅退出已接线（P1-6b）**：`GracefulShutdown`（src/system/shutdown.py）经 SIGTERM/SIGINT 有序停止——set stop_event → 各 `stop()` → 断开 TG 客户端 → join → 关闭持久化连接；watchdog/health 用 `Event.wait` 可中断等待。仍待做：worker pool 化（P1-6c）

## 任务状态机

- 过渡态：`submitting` → `queued` → `downloading`（可 `paused`）
- 终态：`TERMINAL_STATES = {"done", "skipped", "error", "cancelled"}`（app.py:147）
- 写入经 `set_task_state` / `update_task_state`（app.py:354/414）

## 持久化

- 任务状态：`.task_state/tasks.sqlite3`（WAL 模式；表：task_states / task_history / tdl_fallback_channels；自动备份到 `.task_state/backups/`）
- 断点续传信息：ResumeStore（JSON 文件）
- Telethon 会话：`tg_downloader.session`（**敏感文件，已被 .gitignore/.dockerignore 排除**）

## 配置（config.py，全部环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `TG_API_ID` / `TG_API_HASH` | 必填 | Telegram API 凭据 |
| `WEB_BIND_HOST` / `WEB_BIND_PORT` | 127.0.0.1 / **5003** | 非本地绑定必须配认证 |
| `WEB_AUTH_USERNAME` / `WEB_AUTH_PASSWORD` | 空 | Basic Auth（非本地无凭据返回 403，fail-closed） |
| `TG_PROXY_ENABLED` / `TG_PROXY_TYPE` | true / http | **仅支持 http**（ALLOWED_PROXY_TYPES，导入时校验，socks5 会直接抛错） |
| `TRUST_FORWARDED_FOR` | false | 仅在可信反向代理后设 true，否则 X-Forwarded-For 不参与本地判定（防伪造绕过认证） |
| `RELAY_TOKEN_SECRET` | 空 | 空=relay/在线播放禁用（503）；用于 HMAC 签名 |
| `PUBLIC_BASE_URL` | 空 | 生成外部播放器可访问的签名 URL |
| `TDL_BINARY` 等 | /usr/local/bin/tdl | tdl 下载配置 |
| `DEBUG_API_ENABLED` | false | 调试端点开关 |

## Commands

```bash
# 首次登录（生成 session 文件）
python3 login.py

# 启动
python3 app.py                      # 或 python3 app_new.py，等价

# 测试（pytest.ini 尚未配置，需要 PYTHONPATH）
PYTHONPATH=. python3 -m pytest -q

# Docker
docker compose up -d --build        # HEALTHCHECK 已接线 healthcheck.sh

# 任务库检查
sqlite3 .task_state/tasks.sqlite3 "SELECT task_id, json_extract(state_json,'$.status') FROM task_states;"
```

## 主要 API 端点

- 浏览：`/api/dialogs`、`/api/videos`、`/api/search`、`/api/video_search`、`/api/replies`、`/api/thumb/<msg_id>`
- 下载：`POST /api/download|cancel|retry|retry_all|queue_action`、`/api/download_status`、`/api/progress`、`/api/history`、`/api/recovery_candidates`
- 文件：`/api/files`、`/api/file/<path>`、`/api/stream/<path>`（Range 支持）、`POST /api/rename-file|delete-file|open-folder`
- 在线播放：`/api/online-play-url` → `/relay/<entity_id>/<msg_id>?token=<HMAC签名>`（豁免 Basic Auth，靠 token 保护）
- 系统：`/api/status`、`/api/health`、`/api/settings/proxy`
- 调试（需 DEBUG_API_ENABLED）：`/api/debug*`

## Development Notes

- **加路由**：写在 `src/routes/` 对应 Blueprint + 在 `app_new.py` 注入依赖；不要在 app.py 加 `@app.route`
- **调 Telegram API**：包在 `run_async()` 里；连接问题依赖 `TelegramRuntime.ensure_connection` 自动重连（8s 冷却窗口）
- **改下载逻辑**：注意 DownloadWatchdog 会重启无进度任务（默认 5 分钟阈值）；确保进度回调正常更新
- **文件路径处理**：一律经 `src/files/service.py` 的 `resolve_file_path`（realpath + commonpath 防目录遍历）
- **已知待办**：见 `docs/Task.md`（P1-5b 摘除 app.py 死路由、P1-6 调度器重构、P1-7 运行时阻塞、P1-8 持久化优化等）
- 修改后运行：`PYTHONPATH=. python3 -m pytest -q`（当前基线：全绿）
