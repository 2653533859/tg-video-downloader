# TG Video Downloader Optimization Notes

Date: 2026-02-19
Scope: app.py, downloader.py, config.py, docker-compose.yml, templates/index.html, Dockerfile, requirements.txt

## Findings (ordered by severity)

1. Critical - Telegram API credentials committed in repo defaults
Evidence: config.py:5-6, docker-compose.yml:13-14
Impact: anyone with repo access can reuse your API ID/hash; it also increases the blast radius if a session file leaks.
Recommendation: remove defaults, use env-only values, provide a .env.example, and rotate API credentials.

2. Critical - No access control and binds to all interfaces
Evidence: app.py:1080-1081
Impact: any host that can reach port 5000 can browse dialogs, download files, and stream videos.
Recommendation: add authentication (basic auth or token), and bind to 127.0.0.1 by default or place behind a reverse proxy with auth.

3. High - Path traversal / arbitrary write via Telegram filename
Evidence: app.py:807, downloader.py:93
Impact: a crafted filename containing path separators can write outside the download directory.
Recommendation: sanitize filenames to a safe basename and replace path separators; consider a strict allowlist.

4. High - Task state race + potential queue stall
Evidence: app.py:50-55, 693-702, 742-944, 940-943
Impact: clearing an active task can raise in worker threads, and `active_downloads` may never decrement, stalling the queue.
Recommendation: guard `download_status` with a lock, prevent clearing active tasks, and wrap `_do_download` in try/finally.

5. Medium - Thumbnail cache key collision across dialogs
Evidence: app.py:507-520
Impact: message IDs collide across chats, so thumbnails can be incorrect.
Recommendation: include `entity_id` in the thumbnail cache filename and lookup key.

6. Medium - Unbounded in-memory caches
Evidence: app.py:276-279
Impact: memory growth over time with long-running usage.
Recommendation: add TTL/LRU caps, or clear caches on dialog switch and at intervals.

7. Medium - Hard-coded proxy and fixed concurrency
Evidence: app.py:43, 58
Impact: breaks in environments without a proxy and cannot be tuned at runtime.
Recommendation: read proxy and concurrency settings from environment or config.

8. Medium - Range header parsing is brittle
Evidence: app.py:959-962
Impact: invalid Range headers may raise 500 and suffix ranges are not supported.
Recommendation: implement robust parsing and bounds checking (or use Werkzeug utilities).

9. Low - Unused resume helpers
Evidence: app.py:77-102
Impact: dead code adds complexity and confusion.
Recommendation: remove or wire up resume support.

10. Low - Unused dependencies
Evidence: requirements.txt
Impact: larger image size and slower build.
Recommendation: prune unused packages (e.g., Flask-SocketIO stack if not used).

## Optimization ideas

1. Reliability: move downloads to a dedicated worker queue (queue.Queue or ThreadPoolExecutor) and update task state under a single lock.
2. Persistence: store task metadata and status in SQLite to survive restarts.
3. Performance: add server-side pagination for scans and cap `limit` values; batch reply scans with a concurrency limit.
4. UX: add pause/resume and a per-dialog task summary; surface errors inline with retry reasons.
5. Security: require an auth token for all API endpoints and add basic rate limits.

## Test gaps

1. No automated tests for queue behavior, cancel/retry, or path safety.
2. No API endpoint tests for error handling and Range requests.

