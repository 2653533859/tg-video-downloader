# 项目优化任务清单

> 基于 2026-07-12 的代码结构核查、架构分析、实际测试运行和安全扫描整理。
> 本文只记录优化方向和验收标准，不代表任务已经完成。

## 当前基线（已核实的事实）

- **生产入口是单体 `app.py`（2651 行）**：Dockerfile:9 `CMD ["python", "app.py"]`，容器实际运行的是 app.py 自带的 33 个 `@app.route` 路由；`app_new.py` 的 Blueprint 装配层虽然路由覆盖已完整（31 个 Blueprint 路由 + 2 个 proxy settings 路由），但**未被容器使用**。
- app.py 已大量委托 `src/` 模块（DownloadScheduler、TelegramRuntime、TaskStatePersistence 等约 6400 行模块化代码），自身退化为"外观层 + 路由 + 全局状态容器"，但同一套路由在 app.py 和 src/routes/ 各维护一份。
- 测试基线：`PYTHONPATH=. pytest -q` 为 **112 passed, 8 failed**（0.54s，120 个测试收集无错误）；8 个失败全部在 `tests/test_aria2_client.py`，根因是测试针对一个"基于 requests 的另一版 Aria2Client"编写，与仓库实现（urllib）完全脱节。
- 任务终态实际为 `done / skipped / error / cancelled`（app.py:161 `TERMINAL_STATES`），CLAUDE.md / AGENTS.md 中描述的 `completed / failed` 状态名、"~4000 行 app.py"、以及大量行号均已过时。
- 本机 Python 3.14.5，Dockerfile 基础镜像 python:3.10-slim，存在版本差异。

## P0：先修复正确性与安全问题

### 1. 修复配置冲突导致的启动崩溃 ✅（2026-07-12 完成）

- [x] **代理类型冲突（最高优先级）**：docker-compose.yml、.env.example、实际 .env 的 `TG_PROXY_TYPE` 已统一为 `http`（config.py 唯一支持的类型）；`normalize_proxy_type` 报错信息补充了可选值提示。
- [x] **端口统一到 5003**（用户已确认方向）：config.py 默认 5003、Dockerfile `EXPOSE 5003`、healthcheck.sh 默认 5003、compose 保持 5003。
- [x] PUBLIC_BASE_URL：compose 中硬编码的内网 IP `192.168.66.23` 已移除（默认空）；.env / .env.example 对齐为 `http://localhost:5003`。
- [x] Dockerfile 增加 `HEALTHCHECK` 指令、compose 增加 `healthcheck` 块，均指向 `healthcheck.sh`（从环境变量读取凭据和端口）。
- 验收结果：`import config` 正常、compose YAML 校验通过、`pytest` 无新增失败（112 passed / 8 failed 与基线一致，失败项全部属任务 4）。

### 2. 清理凭据泄露和危险默认值 ✅（2026-07-12 完成，剩凭据轮换需人工）

- [x] 删除含硬编码明文凭据的 4 个孤立文件：`docker_healthcheck.sh`、`docker_healthcheck.py`、`healthcheck.py`、`patch.py`（均已核实无生产引用）；5 个历史文档中的真实凭据已替换为占位符。全仓库扫描无残留。
- [x] **修复 .dockerignore**：新增排除 `.env`、`*.session`、`*.session-journal`、`.task_state/`、`.sync-backups/` 等——镜像不再打包密钥和 Telegram 会话。
- [x] 移除 compose 中 `RELAY_TOKEN_SECRET:-change-me` 弱默认（改为空默认=禁用 relay）；实际 `.env` 中的占位密钥已替换为随机 64 位十六进制值。
- [x] **凭据轮换（2026-07-13 确认无需处理）**：用户确认现网使用的 Web 账户/密码是自行创建的，不在曾泄露的那组硬编码凭据内，无泄露风险，故不轮换。
- [x] **relay token secret 最小长度校验 ✅（2026-07-13 完成，方案 A）**：校验落点在 `src/system/startup.py` 的 `validate_runtime_config`（而非原写的 config.py，集中启动校验、不污染 import config 的测试）。新增 `RELAY_SECRET_MIN_LENGTH = 32`，参数 `relay_token_secret=""` 关键字默认形式加入（向后兼容既有调用）；空值放行=禁用 relay，非空且 <32 字符 `raise RuntimeError` fail-closed。`app_new.py` 唯一生效调用点补传 `RELAY_TOKEN_SECRET`，`tests/test_src_modules.py` 补 3 条断言。验收：pytest 97 passed 零回归、py_compile 通过、四分支独立冒烟通过；现网 `.env` 为 64 hex，零影响。
- 相关文件：`.dockerignore`、`docker-compose.yml`、`.env`、`docs/*.md`。

### 3. 修复 X-Forwarded-For 认证绕过 ✅（2026-07-12 完成）

- [x] 新增配置项 `TRUST_FORWARDED_FOR`（默认 false）：`request_ip_is_local` / `web_auth_failure_kind` / `require_web_auth` 均增加 `trust_forwarded` 参数——默认忽略 `X-Forwarded-For`，只用 `remote_addr` 判定本地；部署在可信反向代理后可显式开启。
- [x] 两个入口的调用点均已接线：app.py（`current_request_is_local`、`_require_web_auth`）与 app_new.py（`before_request`）。
- [x] 更新测试：原先断言可伪造行为的用例改为断言安全行为，并新增"绑定 127.0.0.1 时伪造 XFF 返回 forbidden"回归用例（TestAccessControl 3 项全过）。
- 说明：目录遍历方面未发现问题——`/api/stream`、`/api/file` 均经 `resolve_file_path`（realpath + commonpath 校验），无需改动。
- 相关文件：`src/security/access.py`、`src/security/flask_access.py`、`config.py`、`app.py`、`app_new.py`、`tests/test_src_modules.py`。

### 4. 处理 aria2：废弃清理 ✅（2026-07-13 完成，用户已确认方案 2）

- 决策背景：核查确认 aria2 下载通道在模块化迁移时已丢失——当前生产入口 app.py / src / 前端中 aria2 引用为 0，完整流程只残留在 `app_legacy.py`。用户确认不恢复（tdl 多线程直连速度优于 aria2 经 relay 中转），走废弃分支。
- [x] 删除 `aria2_client.py`（生产零引用）和 `tests/test_aria2_client.py`（8 个失败测试全在此，测的是不存在的 requests 版实现）。
- [x] 从 `docker-compose.yml`、`.env`、`.env.example` 移除 `ARIA2_*` 配置；relay 段（PUBLIC_BASE_URL / RELAY_TOKEN_SECRET / RELAY_TOKEN_TTL）保留并改标题为"Relay 配置（在线播放）"——在线播放功能仍依赖 relay。
- [x] `config.py` 的 `ARIA2_*` 变量暂留（app_legacy.py 仍 import，删除会 ImportError），注释标注"待 P1-5 随旧入口移除"。
- 验收结果：**`pytest -q` 112 passed / 0 failed**（此前 8 个失败全部消除）；config 加载、两入口编译、compose YAML 均通过；生产代码与前端 aria2 零残留。
- 遗留：`database.py` / `metrics.py` 的去留与 aria2 无关，留到 P1-5 死代码清理统一决策；`requirements.txt` 中 `PySocks` 未固定版本，随任务 10（容器与 CI）一并处理；`pytest.ini` 固定 pythonpath 已在任务 11 覆盖。

## P1：收敛入口与清理死代码

### 5. 收敛唯一生产入口 ✅（2026-07-13 完成，遗留 P1-5b）

- 关键更正：核查 `app.py` 完整 `__main__` 块（app.py:2646-2654）后确认，`python app.py` 实际委托 `app_new` 装配 Blueprint 并 serve **`app_new.app`**——入口事实上早已收敛，与远程现网结构一致（.sync-backups 备份核对），**无需改 Dockerfile CMD，现网零影响**。app.py 自己的 Flask 实例（L125）及其 33 个 `@app.route` 从未被 serve，属死代码。
- [x] **git init 兜底**（用户选定方式）：加固 .gitignore（补 logs/.resume/.task_state/.sync-backups/.pytest_cache/.DS_Store）→ 快照提交 → 发现 `api.md` 含真实 Telegram API 凭据，已删除并 amend 出 git 历史（全历史扫描确认零残留）。
- [x] 删除死文件约 40 项（快照提交可恢复）：4 个历史入口、5 个补丁文件 + patches/ 目录、3 个孤立工具（quick_start.sh 实为无关的 1Panel 安装器）、database.py / metrics.py 及其测试、start_async.sh / requirements-async.txt、约 20 个历史总结文档（根目录 7 + docs/ 12 + DOWNLOAD.md）。
- [x] 随删除闭环的代码修改：config.py 移除 `ARIA2_*` 块（aria2 批次遗留项闭环）；3 处"请先运行 downloader.py 登录"报错指引改为 login.py（app.py:1680、src/telegram/startup.py:41、src/telegram/runtime.py:107）及对应测试断言。
- [x] 重写 CLAUDE.md（按当前真实结构：入口委托关系、src/ 模块地图、真实状态名、5003 端口、无 aria2）；AGENTS.md 同步为镜像；README.md 全量重写（原文约七成引用已删文件或失实——Prometheus 端点不存在、"100% 覆盖"等，超出原定"仅修段落"范围）；tests/README.md、docs/WEBSOCKET_GUIDE.md 修正残留引用。
- 验收结果：pytest **87 passed / 0 failed**（较 112 减少 25 个 = 被删死模块的测试数，属预期）；app_new 隔离环境冒烟导入成功、32 条路由、关键路由抽查通过；全仓库无已删模块的 import 残留。
- [x] **P1-5b-1 ✅（2026-07-13 完成）**：用 ast 脚本精确摘除 app.py 死路由——32 个整函数（含 `enforce_access_control`/`index`）、2 个仅摘装饰器（`api_get_proxy_settings`/`api_set_proxy_settings`，函数体被 app_new 以 runtime 前缀调用故保留）、`app = Flask(__name__)` 实例、`url_map` 注册、孤立的 `SignedIntConverter` 类与未使用 flask 导入。app.py 从 2654 行减至 **1940 行**。验收：pytest 87 全绿、冒烟导入路由数 32 不变、关键路由抽查通过、无 `@app` 残留。逐函数引用分析确认 src/routes/ 的同名函数是独立 Blueprint 实现而非引用。
- [ ] **P1-5b-2（待做）**：精简 app_new.py 每个 Blueprint 20-30 个参数的手工注入，改为传 runtime/服务对象（接口设计变更，单独成批）。

### 6. 重构下载调度和线程生命周期（P1-6a ✅ / P1-6b/c 待做）

- [ ] **（P1-6c）** 用固定 worker pool 替代每任务 `threading.Thread(daemon=True)`（app.py:647）；当前 `MAX_CONCURRENT_DOWNLOADS = 1`（app.py:488）靠 DownloadScheduler 计数槽位控制。
- [x] **P1-6a ✅（2026-07-13）** 简化 DownloadScheduler 计数补偿逻辑：重写 `src/download/scheduler.py`，用单调递增 generation 令牌跟踪槽位归属（`get_next_task` 发当代令牌并占槽，`release_tasks` 持匹配令牌才还槽，`release_scheduled_task` 直接作废当代令牌）——消除"正常释放 vs 停滞释放"的计数对冲，杜绝槽位泄漏/超发；原 `released_stalled_task_ids` 计数已移除。
- [x] **P1-6a ✅（2026-07-13）** 明确任务状态机迁移表：新建 `src/download/transitions.py`（`can_transition`/`is_terminal` + 显式邻接表），四终态 `done/skipped/error/cancelled` 默认不可迁出；`set_task_state`/`update_task_state` 接入校验（自动流程 `allow_revive=False` 禁覆盖终态，仅 resume/retry `allow_revive=True` 可复活）；`_restart_stalled_download`/`_recover_stalled_tasks` 改走状态机，watchdog 不再重启已完成任务。
- [x] **P1-6b ✅（2026-07-13 完成）** 增加优雅退出：新建 `src/system/shutdown.py` 的 `GracefulShutdown` 编排器（set stop_event → 各 `stop()` → 断开 TG 客户端 → join → 关闭持久化，防御式 + 幂等）。watchdog/health_checker 的 `stop()` 由 `time.sleep` 改为 `Event.wait` **可中断等待**（立即生效）；缩略图清理/DB 备份两个周期循环接入 `shutdown_event`；app.py 新增 `shutdown_runtime`/`_disconnect_tg_clients`/`_install_shutdown_signal_handlers`，两入口 `__main__` 注册 SIGTERM/SIGINT。`TaskStatePersistence.close()` 在停机时释放连接（WAL 收尾），避免下载中状态不一致。新增 4 条测试（watchdog/health 可中断停止 + 编排顺序/幂等 + 防御式）。
- [ ] **（P1-6c）** tdl 单实例 Bolt DB 约束做成调度器级资源锁，而非依赖 max_concurrent=1 的隐含行为。
- P1-6a 验收结果：新增 `tests/test_transitions.py`（6 项）+ scheduler 令牌测试（4 项）；全量 **97 passed**（原 87 零回归）；`py_compile` 通过；隔离冒烟装配成功（32 路由）、终态保护生效（watchdog 覆盖 `done→error` 被拒、resume 复活 `done→queued` 放行）。
- 验收（P1-6 整体）：并发提交、取消、重试、watchdog 恢复、进程重启恢复五类场景有集成测试。
- 相关文件：`app.py`、`src/download/scheduler.py`、`src/download/transitions.py`、`src/download/worker.py`、`src/download/watchdog.py`。

### 7. 收紧 Telegram 运行时的阻塞与缓存边界

- [x] **✅（2026-07-13 完成）** `ensure_connection` 重连原在 `reconnect_lock` 内 `.result(timeout=45)` 同步阻塞触发它的 Flask 请求线程——已改为**后台重连 + 快速失败**：断开时立即返回 False 并排布后台重连线程（`_maybe_start_reconnect`/`_run_reconnect`），请求线程不再阻塞。新增 `test_ensure_connection_reconnects_in_background`。
- [x] **✅（2026-07-13 完成）** `run_async` 超时后 `future.cancel()`：`run_coroutine_threadsafe` 返回的 concurrent future 其 `.cancel()` 经内部 `_chain_future` 把取消传播到 loop 线程上的 asyncio task（CPython 语义正确，无需改动）；重连退避已由固定 8s 窗口改为**指数退避**（8/16/32…上限 120s，成功即清零）。新增 `test_reconnect_cooldown_exponential_backoff`。
- [x] **✅（2026-07-13 完成）** `get_cached_message` 的兜底逻辑会全表扫描并可能返回**其他频道**同 msg_id 的消息（runtime.py:260-273）——已将跨频道兜底循环收敛到仅 `entity_id is None` 时执行；调用方指定 entity 却未命中时返回 `None`，由 worker 兜底走 `resolve_message` 精确重取。新增 `test_get_cached_message_does_not_cross_entity`，pytest 98 passed。
- [x] **✅（2026-07-13 完成）** 缓存边界：messages_cache 有 2000 条上限、dialogs 缓存有 300s TTL（已实现）；`videos_cache`/`replies_cache` 此前只有容量上限（30/500）无 TTL，已在读取路径补 TTL 校验（`_cache_fresh`，默认 300s，复用写入时已存的 `time` 字段）；`current_entity_cache` 经核查是固定 ~5 键的当前实体暂存 dict、不会无界增长，无需 TTL/容量。新增 `test_list_videos_ttl_expiry_triggers_rescan`。
- 验收：网络断开、代理不可用、API 超时、重连期间请求四类场景下不阻塞 Flask 线程、不泄漏线程、不串消息。
- 相关文件：`src/telegram/runtime.py`、`src/telegram/health_checker.py`。

### 8. 优化任务持久化层 ✅（2026-07-13 完成）

- [x] `TaskStatePersistence.connect()` 原每次操作新建 SQLite 连接并重复执行 3 个 `CREATE TABLE` + PRAGMA（旧连接还因 sqlite3 上下文管理器只提交不关闭而堆积）——改为**复用单连接**（`check_same_thread=False` + `self.lock` 串行化），schema 经 `_init_schema` **一次性初始化**；新增 `close()` 供优雅退出释放。
- [x] 移除 `enabled()` 中 `"unittest" not in sys.modules` 的测试感知逻辑——改为构造器 `enabled=True` **配置开关**（依赖注入），`import sys` 一并删除；测试去掉 `monkeypatch.delitem(sys.modules,'unittest')` hack。
- [x] 状态写入**节流**：同一 task 同状态的高频进度更新在 `persist_throttle_seconds`（默认 2s）窗口内跳过落库；终态与状态切换始终写。`task_history` 增加 `idx_task_history_completed(completed_at DESC, updated_at DESC)` 分页索引。
- [x] 增加 schema 版本号（`PRAGMA user_version = SCHEMA_VERSION`，仅新库写入）；备份增加 `PRAGMA integrity_check` **一致性校验**（失败即丢弃该备份），并修复原 target 连接泄漏。
- 验收结果：新增 3 条测试（enabled 开关 / 节流跳过同状态 / schema 版本 + 备份校验），pytest **104 passed** 零回归。
- 相关文件：`src/state/persistence.py`、`src/state/manager.py`。

## P2：可观测性、部署与测试

### 9. 让监控和健康检查真正可用 ✅（2026-07-13 完成）

- [x] `/metrics`/metrics.py **已于 P1-5 随死代码删除**（生产零引用），本项消解，无需再接 Prometheus。
- [x] health 分层：新增 `/api/health/live`（liveness，进程存活即 200，不触外部依赖）与 `/api/health/ready`（readiness，主 Telegram 未就绪返回 503）；完整 `/api/health` 增加 `degraded` 列表（telegram / tdl 降级标记，tdl 降级不阻断就绪）。
- [x] 日志加固：轮转此前已有（RotatingFileHandler 10MB×30）；新增**敏感字段脱敏** `RedactionFilter`（src/utils/log_filters.py，脱敏 password/api_hash/token/secret/Basic 头等，挂在 file + stream handler）。结构化字段（task_id/entity_id…）散落各 log 调用点，属大范围文案改写，本轮未逐点改写（价值低、churn 大），保留现状。
- 验收：新增 3 条测试（liveness/readiness、脱敏正则、Filter 改写记录），pytest 111 passed。
- 相关文件：`src/system/status.py`、`src/routes/system.py`、`src/utils/log_filters.py`、`app.py`。

### 10. 完善容器和 CI ✅（2026-07-13 完成，非 root 需宿主配套 chown）

- [x] Dockerfile 使用**非 root 用户**（uid 10001 appuser，chown /app）；显式建 downloads/logs/.task_state/.resume 可写目录；digest 固定给出解析命令注释（离线无法解析真实 sha256，保留为 FROM tag + 文档）。⚠️ 现网 compose 用 `.:/app`、`/root/downloads`、`/root/.tdl` 等 **root 属主 bind-mount**，非 root 无法写入——compose 内已加明确说明：二选一「宿主 `chown -R 10001:10001 <目录>`」或「取消 `user: "0:0"` 注释临时以 root 运行」。
- [x] docker-compose 安全加固（不影响挂载读写权限）：`security_opt: [no-new-privileges:true]` + `cap_drop: [ALL]`。
- [x] Python 支持矩阵：CI 固定 `python 3.10`（与容器基础镜像一致）。
- [x] CI 增加 `test` job（compileall 语法检查 + pytest + `docker compose config` 校验 + 敏感文件扫描 `git ls-files` 防 .env/session 提交），镜像 `build` job `needs: test` 且仅 push 触发。
- 相关文件：`Dockerfile`、`docker-compose.yml`、`.github/workflows/docker-publish.yml`。

### 11. 建立分层测试体系

- [ ] 为 Flask 路由增加 API 测试（鉴权、参数校验、错误码），当前 src/routes/ 只有少量覆盖。
- [ ] 为下载 worker 增加不依赖真实 Telegram/tdl 的合同测试（resume、取消、重试、终态）。
- [ ] 增加 SQLite 并发写入与崩溃恢复测试。
- [ ] 安全回归测试：伪造 XFF、无 token relay、过期 token、路径遍历。
- 验收：本地和 CI 同一条命令跑测试，单元/集成/冒烟可分层执行。

## 建议实施顺序

1. **P0-1/P0-2/P0-3**（半天级）：配置冲突、凭据清理、XFF 绕过——都是小改动但影响安全和"能不能启动"。
2. **P0-4**（半天级）：决定 aria2_client/database/metrics 三个孤立模块去留，测试恢复全绿。
3. **P1-5**（一两天）：定唯一入口 + 删除已核实的死文件（约 15 个文件/目录），这一步会让后续所有工作只改一处。
4. **P1-6/7/8**（按周计）：调度器、Telegram 运行时、持久化层的架构改进。
5. **P2**：监控、容器加固、CI、测试补强。

## 暂不建议立即做的事项

- 暂不切换 Quart/全异步架构：先稳定现有 Flask + 双 event loop 边界，迁移会放大故障面。
- 暂不新增下载后端：Telethon/tdl/aria2 的职责和降级路径需要先统一（aria2 路径目前生产代码已不引用，属于任务 4 的去留决策）。
- 删除旧文件前先确认生产容器和部署脚本已切换到唯一入口（任务 5 的前置条件），再批量归档。
