"""Telegram startup loops."""

import asyncio
import sys
import time


def run_main_telegram_client(
    *,
    client,
    loop,
    runtime,
    format_user_display,
    init_health_checker,
    on_connecting,
    on_connected,
    on_error,
    log_info,
    print_func=print,
    sleep_func=time.sleep,
    exit_func=sys.exit,
):
    asyncio.set_event_loop(loop)

    retry_count = 0
    retry_delay = 5

    while True:
        try:
            connecting_message = "正在连接 Telegram..."
            runtime.mark_error(connecting_message)
            on_connecting(connecting_message)
            print_func(f"正在连接 Telegram... (第 {retry_count + 1} 次)")
            log_info(f"正在连接 Telegram... (第 {retry_count + 1} 次)")

            loop.run_until_complete(
                asyncio.wait_for(client.connect(), timeout=30)
            )

            if not loop.run_until_complete(client.is_user_authorized()):
                error_message = "Telegram 未登录！请先运行 downloader.py 完成登录。"
                runtime.mark_error(error_message)
                on_error(error_message)
                print_func(f"错误: {error_message}")
                exit_func(1)

            me = loop.run_until_complete(client.get_me())
            user_info = format_user_display(me)
            runtime.mark_connected(user_info)
            on_connected(user_info)
            retry_count = 0
            print_func(f"Telegram 已连接: {user_info}")
            log_info(f"Telegram 已连接: {user_info}")
            init_health_checker()
            loop.run_forever()
            break

        except asyncio.TimeoutError:
            retry_count += 1
            error_message = f"连接超时，{retry_delay}秒后重试... (已重试 {retry_count} 次)"
            runtime.mark_error(error_message)
            on_error(error_message)
            print_func(error_message)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            sleep_func(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)

        except Exception as exc:
            retry_count += 1
            error_message = f"连接失败: {exc}，{retry_delay}秒后重试..."
            runtime.mark_error(error_message)
            on_error(error_message)
            print_func(error_message)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            sleep_func(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)


def run_relay_telegram_client(
    *,
    loop,
    runtime,
    wait_for_main_ready,
    get_main_error,
    recreate_client,
    on_client_recreated,
    on_connecting,
    on_connected,
    on_error,
    log_info,
    log_warning,
    log_error,
    sleep_func=time.sleep,
):
    asyncio.set_event_loop(loop)

    retry_count = 0
    retry_delay = 5
    client = None

    while True:
        try:
            connecting_message = "正在连接 Relay Telegram..."
            runtime.mark_error(connecting_message)
            on_connecting(connecting_message)
            log_info(f"正在连接 Relay Telegram... (第 {retry_count + 1} 次)")

            if not wait_for_main_ready(timeout=60):
                raise Exception(get_main_error() or "主 Telegram 未就绪")

            client = recreate_client()
            runtime.client = client
            on_client_recreated(client)

            loop.run_until_complete(
                asyncio.wait_for(client.connect(), timeout=30)
            )

            if not loop.run_until_complete(client.is_user_authorized()):
                error_message = "Relay Telegram 未登录"
                runtime.mark_error(error_message)
                on_error(error_message)
                log_error(error_message)
                return

            runtime.mark_connected()
            on_connected()
            retry_count = 0
            log_info("Relay Telegram 已连接")
            loop.run_forever()
            break

        except asyncio.TimeoutError:
            retry_count += 1
            error_message = f"Relay 连接超时，{retry_delay}秒后重试..."
            runtime.mark_error(error_message)
            on_error(error_message)
            log_warning(error_message)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            sleep_func(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)

        except Exception as exc:
            retry_count += 1
            error_message = f"Relay 连接失败: {exc}，{retry_delay}秒后重试..."
            runtime.mark_error(error_message)
            on_error(error_message)
            log_warning(error_message)
            try:
                loop.run_until_complete(client.disconnect())
            except Exception:
                pass
            sleep_func(retry_delay)
            retry_delay = min(retry_delay * 1.5, 60)
