import os

# Telegram API 配置
# 从 https://my.telegram.org 获取
API_ID = int(os.getenv("TG_API_ID", "33900011"))
API_HASH = os.getenv("TG_API_HASH", "7168171ca268ace6d8da5e2362e103d0")

# 下载配置
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
SESSION_NAME = os.getenv("SESSION_NAME", "tg_downloader")
