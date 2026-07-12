#!/usr/bin/env python3
"""Telegram 视频下载器 - 支持频道、群组、私聊"""

import os
import sys
import asyncio
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.types import (
    MessageMediaDocument,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
)
from tqdm import tqdm
from config import API_ID, API_HASH, DOWNLOAD_DIR, SESSION_NAME


def get_video_info(message):
    """从消息中提取视频信息"""
    if not message.media or not isinstance(message.media, MessageMediaDocument):
        return None
    doc = message.media.document
    is_video = False
    filename = None
    duration = 0
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            is_video = True
            duration = attr.duration
        if isinstance(attr, DocumentAttributeFilename):
            filename = attr.file_name
    if not is_video:
        return None
    if not filename:
        filename = f"video_{message.id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    return {
        "id": message.id,
        "filename": filename,
        "size": doc.size,
        "duration": duration,
        "date": message.date,
    }


def format_size(size_bytes):
    """格式化文件大小"""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def format_duration(seconds):
    """格式化时长"""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


class TGDownloader:
    def __init__(self):
        self.client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    async def start(self):
        await self.client.start()
        me = await self.client.get_me()
        print(f"已登录: {me.first_name} (@{me.username})")

    async def list_dialogs(self):
        """列出所有对话"""
        print("\n--- 对话列表 ---")
        dialogs = await self.client.get_dialogs()
        for i, dialog in enumerate(dialogs[:30]):
            dtype = "频道" if dialog.is_channel else "群组" if dialog.is_group else "私聊"
            print(f"  [{i}] [{dtype}] {dialog.name} (ID: {dialog.id})")
        return dialogs

    async def scan_videos(self, entity, limit=100):
        """扫描对话中的视频"""
        videos = []
        print(f"\n正在扫描最近 {limit} 条消息中的视频...")
        async for message in self.client.iter_messages(entity, limit=limit):
            info = get_video_info(message)
            if info:
                videos.append((message, info))
        return videos

    async def download_video(self, message, info, save_dir):
        """下载单个视频"""
        filepath = os.path.join(save_dir, info["filename"])
        if os.path.exists(filepath) and os.path.getsize(filepath) == info["size"]:
            print(f"  跳过(已存在): {info['filename']}")
            return

        progress = tqdm(
            total=info["size"],
            unit="B",
            unit_scale=True,
            desc=f"  {info['filename'][:40]}",
        )

        def progress_cb(current, total):
            progress.update(current - progress.n)

        await self.client.download_media(
            message, file=filepath, progress_callback=progress_cb
        )
        progress.close()

    async def run(self):
        await self.start()

        while True:
            print("\n===== Telegram 视频下载器 =====")
            print("  1. 浏览对话列表并选择下载")
            print("  2. 通过链接/用户名下载")
            print("  3. 下载 Saved Messages 中的视频")
            print("  0. 退出")
            choice = input("\n请选择: ").strip()

            if choice == "0":
                break
            elif choice == "1":
                await self.mode_browse()
            elif choice == "2":
                await self.mode_link()
            elif choice == "3":
                await self.mode_saved()
            else:
                print("无效选择")

        await self.client.disconnect()

    async def mode_browse(self):
        """浏览模式"""
        dialogs = await self.list_dialogs()
        idx = input("\n输入对话编号: ").strip()
        if not idx.isdigit() or int(idx) >= len(dialogs):
            print("无效编号")
            return
        dialog = dialogs[int(idx)]
        await self._download_from_entity(dialog.entity, dialog.name)

    async def mode_link(self):
        """链接/用户名模式"""
        link = input("输入频道/群组链接或用户名 (如 @channel 或 https://t.me/channel): ").strip()
        if not link:
            return
        try:
            entity = await self.client.get_entity(link)
            name = getattr(entity, "title", None) or getattr(entity, "first_name", link)
            await self._download_from_entity(entity, name)
        except Exception as e:
            print(f"获取失败: {e}")

    async def mode_saved(self):
        """Saved Messages 模式"""
        me = await self.client.get_me()
        await self._download_from_entity(me, "Saved Messages")

    async def _download_from_entity(self, entity, name):
        """从指定实体下载视频"""
        limit_str = input("扫描最近多少条消息? (默认100): ").strip()
        limit = int(limit_str) if limit_str.isdigit() else 100

        videos = await self.scan_videos(entity, limit)
        if not videos:
            print("未找到视频")
            return

        print(f"\n找到 {len(videos)} 个视频:")
        for i, (msg, info) in enumerate(videos):
            print(
                f"  [{i}] {info['filename'][:50]}  "
                f"{format_size(info['size'])}  "
                f"{format_duration(info['duration'])}  "
                f"{info['date'].strftime('%Y-%m-%d')}"
            )

        sel = input("\n选择要下载的视频 (all=全部, 0,1,3=指定编号, 回车取消): ").strip()
        if not sel:
            return

        if sel.lower() == "all":
            selected = videos
        else:
            indices = [int(x.strip()) for x in sel.split(",") if x.strip().isdigit()]
            selected = [videos[i] for i in indices if i < len(videos)]

        if not selected:
            print("未选择任何视频")
            return

        # 创建下载目录
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in name)
        save_dir = os.path.join(DOWNLOAD_DIR, safe_name)
        os.makedirs(save_dir, exist_ok=True)

        total_size = sum(info["size"] for _, info in selected)
        print(f"\n即将下载 {len(selected)} 个视频, 总大小: {format_size(total_size)}")
        print(f"保存到: {save_dir}\n")

        for msg, info in selected:
            await self.download_video(msg, info, save_dir)

        print(f"\n下载完成! 文件保存在: {save_dir}")


async def main():
    if API_ID == 0 or API_HASH == "":
        print("请先在 config.py 中配置 API_ID 和 API_HASH")
        print("获取地址: https://my.telegram.org")
        sys.exit(1)

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    downloader = TGDownloader()
    await downloader.run()


if __name__ == "__main__":
    asyncio.run(main())

