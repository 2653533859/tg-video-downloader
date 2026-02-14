# Telegram Video Downloader 🎥

一个带有 Web 界面的 Telegram 视频下载工具。支持解析 Telegram 消息链接并直接下载视频到本地。

## ✨ 功能

- **Web 界面**：通过浏览器直接输入链接即可下载。
- **异步下载**：基于 Telethon 实现高效下载。
- **Docker 支持**：一键部署，环境隔离。
- **自动镜像构建**：通过 GitHub Actions 自动构建 Docker 镜像。

## 🚀 快速开始

### 1. 获取 Telegram API 凭据
请访问 [my.telegram.org](https://my.telegram.org) 获取您的 `API_ID` 和 `API_HASH`。

### 2. 使用 Docker Compose 部署 (推荐)

```bash
git clone https://github.com/2653533859/tg-video-downloader.git
cd tg-video-downloader
docker-compose up -d
```

**注意**：首次运行可能需要在容器终端内进行登录验证（生成 session 文件）。由于 session 文件被 git 忽略，它将保存在您的宿主机目录中以实现持久化。

### 3. 环境变量配置

在 `docker-compose.yml` 中可以配置以下变量：

| 变量名 | 说明 |
| :--- | :--- |
| `TG_API_ID` | Telegram API ID |
| `TG_API_HASH` | Telegram API Hash |
| `DOWNLOAD_DIR` | 视频下载存储目录 |
| `SESSION_NAME` | Session 文件名前缀 |

## 📦 项目结构

- `app.py`: Web 服务入口。
- `downloader.py`: Telegram 下载核心逻辑。
- `config.py`: 配置管理。
- `templates/`: 前端页面模板。
- `downloads/`: 视频保存目录。

## ⚠️ 隐私提醒
您的 `*.session` 文件包含登录凭据，**请勿泄露**。本项目已在 `.gitignore` 中将其排除。

## 📄 协议
[MIT License](LICENSE)
