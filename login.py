#!/usr/bin/env python3
"""
Telegram 登录脚本
用于首次登录并生成 session 文件
"""
import asyncio
from telethon import TelegramClient
from config import API_ID, API_HASH, SESSION_NAME, PROXY_CONFIG

async def login():
    """登录 Telegram"""
    print("开始登录 Telegram...")
    print(f"API ID: {API_ID}")
    print(f"Session: {SESSION_NAME}")

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH, proxy=PROXY_CONFIG)

    await client.start()

    me = await client.get_me()
    print(f"\n✅ 登录成功！")
    print(f"用户名: {me.first_name} {me.last_name or ''}")
    if me.username:
        print(f"@{me.username}")
    print(f"ID: {me.id}")
    print(f"\nSession 文件已保存: {SESSION_NAME}.session")

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(login())
