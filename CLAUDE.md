# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Telegram 视频下载器 (Telegram Video Downloader) with automated monitoring and recovery mechanisms. A Flask-based web application that downloads videos from Telegram channels/groups using the Telethon library, with three built-in monitoring systems for reliability.

**Key Features:**
- Web UI for browsing Telegram channels and downloading videos
- Download queue management with persistence across restarts
- Three monitoring systems: Download Watchdog, Telegram Health Checker, Docker Health Check
- Support for multiple download methods: direct download via Telethon, aria2, and tdl binary
- Video streaming and relay URL generation

**Architecture:**
- Single monolithic Flask application (`app.py`, ~4000 lines)
- Two async event loops: main Telegram client (tg_loop) and relay client (relay_loop)
- Threading model: separate threads for Telegram clients, queue processing, background monitors
- Persistence: SQLite database for task state, JSON files for resume information

## Core Components

### 1. Monitoring Systems (已优化功能)

**DownloadWatchdog** (app.py:127-261)
- Detects stalled downloads (default: 5 minutes without progress)
- Automatically restarts stuck tasks with resume support
- Check interval: 60s, stall timeout: 300s (configurable at app.py:249)

**TelegramHealthChecker** (app.py:263-416)
- Proactively monitors Telegram connection health every 2 minutes
- Auto-reconnects after 3 consecutive failures
- Prevents download failures by maintaining connection stability
- Initialized in `init_tg_health_checker()` after main client connects (app.py:3948)

**Docker Health Check** (healthcheck.sh)
- Container-level health monitoring via `/api/health` endpoint
- Uses Basic auth with credentials from environment variables
- Designed for Docker healthcheck configuration

### 2. Telegram Client Management

**Two Separate Clients:**
- `tg_client`: Main client for browsing, searching, downloading (app.py:95)
- `relay_tg_client`: Secondary client using StringSession for relay URLs to avoid DB lock conflicts (app.py:98)

**Connection Functions:**
- `ensure_tg_connection()` (app.py:1227): Validates main client connection with optional reconnect
- `ensure_relay_connection()` (app.py:1240): Validates relay client
- `run_async()` (app.py:1252): Execute coroutines on main tg_loop with timeout
- `relay_run_async()` (app.py:1273): Execute coroutines on relay_loop

Both clients run in separate threads with infinite retry logic on connection failure.

### 3. Download Queue System

**Queue Management:**
- In-memory queue (`download_queue`) + persistent state in SQLite
- `add_to_queue()` (app.py:927): Add new download task
- `process_queue()` (app.py:956): Main queue processor, runs continuously in background
- `get_next_from_queue()` (app.py:938): Fetch next pending task
- `move_queued_task()` (app.py:1054): Reorder queue (move up/down/top/bottom)

**Task State Persistence:**
- SQLite database at `downloads/task_state.db` (created via `_task_db_connect()` at app.py:496)
- `_persist_task_state()` (app.py:541): Save task to database
- `_load_persisted_task_states()` (app.py:571): Restore tasks on startup
- Legacy JSON file migration: `_migrate_legacy_task_state_files()` (app.py:524)

**Resume Mechanism:**
- Resume info stored in `downloads/resume/*.json` via `save_resume_info()` (app.py:868)
- `_restore_resume_tasks_into_memory()` (app.py:890): Load resume data on startup
- `_resume_task()` (app.py:964): Resume a single incomplete task
- `auto_resume_incomplete_tasks()` (app.py:4033): Auto-resume all incomplete tasks after Telegram connection

### 4. Download Methods

The application supports three download methods:

**Method 1: Direct Telethon Download** (default)
- `_download_with_telethon()`: Stream download via Telethon client
- Writes chunks directly to disk with progress tracking
- Supports resume via byte offset (`request_size`, `offset` params)
- Main download logic in `/api/download` endpoint (app.py:2826)

**Method 2: aria2 Integration**
- Generates relay URL via `build_relay_token()` and submits to aria2
- Relay endpoint: `/relay/<entity_id>/<msg_id>` (app.py:3802)
- Configured via `ARIA2_RPC_URL`, `ARIA2_SECRET` environment variables

**Method 3: tdl Binary Fallback**
- External CLI tool for direct Telegram download
- Used when Telethon fails for certain channels
- Configured via `TDL_BINARY`, `TDL_NAMESPACE`, `TDL_STORAGE_PATH` env vars
- Fallback tracking: `_remember_tdl_fallback_channel()` (app.py:680)

## Commands

### Running the Application

**Start the web server:**
```bash
python3 app.py
```

**With Docker:**
```bash
# Copy files to container
docker cp app.py <container_id>:/app/app.py
docker cp healthcheck.sh <container_id>:/app/healthcheck.sh

# Restart container
docker restart <container_id>
```

### Testing and Debugging

**Check monitoring systems are running:**
```bash
docker logs <container_id> | grep -E "watchdog|tg-health"
```

**Test health check:**
```bash
docker exec <container_id> /app/healthcheck.sh
```

**Test Telegram connection:**
```bash
docker exec <container_id> python3 -c "from telethon.sync import TelegramClient; print('OK')"
```

**Check task database:**
```bash
sqlite3 downloads/task_state.db "SELECT task_id, state, file_name FROM task_states;"
```

**Monitor logs:**
```bash
tail -f logs/app.log
```

### Troubleshooting

**Monitoring not starting:**
```bash
docker exec <container_id> grep -n "download_watchdog\|TelegramHealthChecker" /app/app.py
```

**View task recovery candidates:**
```bash
curl -u username:password http://localhost:5000/api/recovery_candidates
```

**Clear stalled tasks:**
```bash
curl -X POST -u username:password http://localhost:5000/api/clear_tasks
```

## Configuration

All configuration is via environment variables (config.py:1-48):

**Required:**
- `TG_API_ID`: Telegram API ID
- `TG_API_HASH`: Telegram API Hash

**Web Server:**
- `WEB_BIND_HOST`: Default "127.0.0.1"
- `WEB_BIND_PORT`: Default 5000
- `WEB_AUTH_USERNAME`, `WEB_AUTH_PASSWORD`: Required for non-local binding

**Download:**
- `DOWNLOAD_DIR`: Default "downloads"
- `SESSION_NAME`: Default "tg_downloader" (for Telethon session file)

**Proxy:**
- `TG_PROXY_ENABLED`: Default true
- `TG_PROXY_TYPE`: Default "socks5"
- `TG_PROXY_HOST`: Default "127.0.0.1"
- `TG_PROXY_PORT`: Default 7890

**aria2:**
- `ARIA2_RPC_URL`: Default "http://127.0.0.1:6800/jsonrpc"
- `ARIA2_SECRET`: aria2 RPC secret
- `PUBLIC_BASE_URL`: Public URL for relay endpoint generation
- `RELAY_TOKEN_SECRET`: Secret for signing relay tokens
- `RELAY_TOKEN_TTL`: Token expiration in seconds (default 1800)

**tdl:**
- `TDL_BINARY`: Path to tdl binary (default "/usr/local/bin/tdl")
- `TDL_NAMESPACE`: Default "default"
- `TDL_STORAGE_PATH`: Default "/root/.tdl/data"
- `TDL_THREADS`: Default 8
- `TDL_LIMIT`: Default 4

**Tuning Monitoring (in code):**
- Download watchdog: `check_interval` and `stall_timeout` at app.py:249
- Telegram health check: `check_interval` and `max_retry` at app.py:402

## Key API Endpoints

**Main UI:**
- `GET /`: Web interface

**Telegram Browsing:**
- `GET /api/dialogs`: List user's channels/groups
- `GET /api/videos?dialog_id=<id>&page=<n>`: List videos in a channel
- `GET /api/search?dialog_id=<id>&query=<q>`: Search videos in channel
- `GET /api/replies?dialog_id=<id>&msg_id=<id>`: Get replies to a message
- `GET /api/thumb/<msg_id>`: Get video thumbnail

**Download Management:**
- `POST /api/download`: Start a download task
- `POST /api/cancel`: Cancel a running task
- `POST /api/retry`: Retry a failed task
- `POST /api/retry_all`: Retry all failed tasks
- `POST /api/queue_action`: Reorder queue (move up/down/top/bottom)

**Status & Monitoring:**
- `GET /api/status`: Overall status (tasks, queue, Telegram connection)
- `GET /api/health`: Health check endpoint (used by Docker)
- `GET /api/download_status`: List all active/queued/failed downloads
- `GET /api/history?status=<s>&query=<q>&page=<n>`: Task history from database
- `GET /api/recovery_candidates`: List stalled tasks that can be recovered

**File Operations:**
- `GET /api/file/<filepath>`: Download file from DOWNLOAD_DIR
- `GET /api/stream/<filepath>`: Stream video with range request support
- `POST /api/open-folder`: Open file explorer (requires OPEN_FOLDER_ENABLED=true)
- `POST /api/rename-file`: Rename downloaded file
- `POST /api/delete-file`: Delete downloaded file

**Relay (for aria2):**
- `GET /relay/<entity_id>/<msg_id>?token=<signed_token>`: Stream video via signed token

**Debug (requires DEBUG_API_ENABLED=true):**
- `GET /api/debug`: List recent messages in a channel
- `GET /api/debug_replies`: Get replies with full message object
- `GET /api/debug_full`: Get complete message data

## Important Implementation Details

### Task State Machine

Tasks progress through these states (app.py:707):
- `pending`: Queued, waiting to start
- `downloading`: Active download in progress
- `completed`: Successfully finished
- `failed`: Error occurred
- `cancelled`: User cancelled

State transitions managed via `_set_task_state()` and `_update_task_state()`.

### Concurrency Model

- Flask app runs with `threaded=True` for concurrent request handling
- Main Telegram client: `start_tg_client()` in dedicated thread (app.py:3916)
- Relay client: `start_relay_tg_client()` in separate thread (app.py:3976)
- Queue processor: `process_queue()` runs continuously (app.py:956)
- Background jobs: thumbnail cleanup (app.py:1145), task DB backup (app.py:631)
- Monitors: DownloadWatchdog and TelegramHealthChecker run in their own threads

**Critical:** Never call `loop.run_until_complete()` from Flask request handlers. Use `run_async()` or `relay_run_async()` which properly schedule coroutines on the respective event loops.

### Missing Dependencies

The app imports `aria2_client` and `relay_tokens` modules (app.py:74-75) which are **not present** in this repository. These are likely:
- `aria2_client.py`: Wrapper for aria2 JSON-RPC calls
- `relay_tokens.py`: Token generation/verification for relay URLs

If developing features that require these, either locate the original modules or implement minimal stubs.

### Patches Directory

Contains standalone optimization code that was integrated into main app.py:
- `watchdog_patch.py`: Original DownloadWatchdog implementation
- `telegram_health_check.py`: Original TelegramHealthChecker implementation
- `apply_watchdog.py`: Script to inject watchdog into app.py
- `apply_health_checks.py`: Script to inject health checker into app.py

These files are **reference only** — the optimizations are already applied in app.py.

## Development Notes

**Before making changes:**
1. Check if monitoring systems are affected (watchdog/health checker)
2. Consider impact on both Telegram clients (main and relay)
3. Test with queue processing (tasks may be in various states)
4. Verify task persistence survives app restart

**When adding new download methods:**
- Update `_download_with_telethon()` logic or add parallel method
- Ensure progress tracking works with DownloadWatchdog
- Add appropriate error handling and retry logic
- Update task state persistence if needed

**When modifying Telegram operations:**
- Use `ensure_tg_connection()` before Telegram API calls
- Wrap coroutines with `run_async()` or `relay_run_async()`
- Handle `ConnectionError`, `TimeoutError` for auto-recovery
- Test with TelegramHealthChecker monitoring

**Testing considerations:**
- Watchdog activates after 5 minutes of no progress — use smaller timeout for testing
- Health checker runs every 2 minutes — can be reduced for faster iteration
- Restart app to test task persistence and auto-resume
- Test with various file sizes (small/large) and network conditions (slow/fast)
