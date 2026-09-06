# Google 云盘转存功能开发任务看板 (task.md)

> 创建时间：2026-09-06  
> 目标：为 Telegram 视频下载器新增将视频自动/手动保存到 Google 云盘的功能（基于 rclone 引擎）。

## 架构决策与选型
- **传输引擎**：基于 `rclone` 外部工具，提供极高的超大文件断点续传稳定性与低内存占用。
- **解耦模式**：新建 `src/uploader/` 独立子系统，维护独立的 `UploadManager` 与 `UploadWorker` 队列，完全不污染现有下载状态机。
- **磁盘策略**：提供可选的 `GDRIVE_DELETE_LOCAL_AFTER_UPLOAD` 选项（适合小硬盘 VPS 自动清理本地文件）。
- **触发机制**：
  1. 下载完成自动触发（`GDRIVE_AUTO_UPLOAD=true` 经由 `on_download_complete` 钩子）；
  2. Web 界面“已下载文件”列表手动单点触发上传。

---

## 任务执行进度
### Phase 1: 基础设施与 Rclone 执行引擎
- [x] **1.1 配置层扩展**：在 `config.py` 和 `.env.example` 中增加 Google Drive / rclone 配置变量。
- [x] **1.2 核心模块创建**：创建 `src/uploader/` 目录，实现 `base.py` 抽象基类、`config.py` 与 `rclone.py` 执行器（支持 JSON 流式进度、取消与错误解析）。
- [x] **1.3 单元测试**：编写 `tests/test_uploader_rclone.py`，验证命令构建、进度解析和异常处理（13 passed）。
### Phase 2: 上传任务管理与后台 Worker
- [x] **2.1 上传任务状态与持久化**：在 `src/uploader/status.py` 与 `persistence.py` 中定义上传任务状态机与 SQLite WAL 存储。
- [x] **2.2 UploadManager 与 UploadWorker**：实现独立队列调度、单并发上传控制、重试与取消机制，并接入优雅停机。
- [x] **2.3 下载完成事件挂钩**：在 `DownloadWorker` 中增加 `on_download_complete` 回调，在 `app.py` 中连接自动上传逻辑。
- [x] **2.4 单元测试**：编写 `tests/test_uploader_manager.py`，验证队列调度、回调触发与自动清理（6 passed）。
### Phase 3: API 路由与 Blueprint 装配
- [x] **3.1 路由定义**：在 `src/routes/uploader.py` 中实现 `/api/gdrive/upload`、`/api/gdrive/status`、`/api/gdrive/cancel`、`/api/gdrive/retry`、`/api/gdrive/test`。
- [x] **3.2 Blueprint 依赖注入**：在 `app_new.py` 中注册 `uploader_bp` 并由 `app.py` 注入依赖。
- [x] **3.3 接口测试**：编写 `tests/test_routes_uploader.py` 验证 API 权限控制与参数校验（7 passed）。
### Phase 4: 前端交互与端到端验证
- [x] **4.1 页面模板更新**：在 `templates/index.html` 的已下载文件操作栏中新增【传至云盘】按钮及上传状态展示条。
- [x] **4.2 前端脚本更新**：在 `static/js/app.js` 中增加上传状态轮询与按钮状态实时联动。
- [x] **4.3 回归测试与验证**：运行完整测试套件，新建 26 项单元测试全过，既有核心测试无回归（总计 168 项测试通过）。

### Phase 5: Google Drive 网页授权向导 (方案 A)
- [x] **5.1 授权管理模块**：在 `src/uploader/auth.py` 中实现 rclone 配置生成、Token 解析与校验、配置文件读写与解绑。
- [x] **5.2 API 路由扩展**：在 `src/routes/uploader.py` 中新增 `/api/gdrive/auth/info`、`/api/gdrive/auth/save_token`、`/api/gdrive/auth/save_config`、`/api/gdrive/auth/unbind` 端点。
- [x] **5.3 单元测试**：编写 `tests/test_uploader_auth.py` 验证 Token 写入、格式校验与配置解析（6 passed）。
- [x] **5.4 网页弹窗与交互**：在 `index.html` 实现 `gdriveLoginModal`，并在 `app.js` 中接线 Token 提交、直接粘贴配置、状态反馈与一键连通测试。
- [x] **5.5 端到端验证**：进行全量测试与网页端授权验证（32 项上传专属测试全过，总计 174 项核心测试全绿）。
