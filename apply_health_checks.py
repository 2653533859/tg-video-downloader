#!/usr/bin/env python3
"""
应用方案 2（Telegram 健康检查）和方案 4（Docker 健康检查）
"""
import sys
import datetime

def apply_telegram_health_check(app_py_path):
    """应用 Telegram 健康检查补丁"""
    print("\n=== 应用方案 2: Telegram 健康检查 ===")
    
    # 读取原文件
    with open(app_py_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 读取补丁
    with open('telegram_health_check.py', 'r', encoding='utf-8') as f:
        health_check_code = f.read()
    
    # 查找插入位置（在 download_watchdog 定义之后）
    insert_line = None
    for i, line in enumerate(lines):
        if 'download_watchdog = DownloadWatchdog(' in line:
            # 找到 download_watchdog 定义块的结束
            for j in range(i, min(i+10, len(lines))):
                if ')' in lines[j] and 'DownloadWatchdog' not in lines[j]:
                    insert_line = j + 1
                    break
            break
    
    if insert_line is None:
        print("❌ 错误：未找到 download_watchdog 定义")
        return False
    
    # 插入健康检查代码
    health_lines = ['\n\n'] + health_check_code.split('\n')
    health_lines = [line + '\n' for line in health_lines]
    
    new_lines = lines[:insert_line] + health_lines + lines[insert_line:]
    
    # 查找 start_tg_client 函数，在 Telegram 连接成功后初始化健康检查
    tg_connect_line = None
    for i, line in enumerate(new_lines):
        if 'def start_tg_client():' in line:
            # 找到函数内 Telegram 连接成功的位置
            for j in range(i, min(i+200, len(new_lines))):
                if 'tg_user_info =' in new_lines[j] and 'me.username' in new_lines[j]:
                    tg_connect_line = j + 1
                    break
            break
    
    if tg_connect_line:
        # 插入健康检查初始化代码
        indent = '        '
        init_code = f'{indent}init_tg_health_checker()\n'
        new_lines.insert(tg_connect_line, init_code)
        print(f"✓ Telegram 健康检查代码已插入到第 {insert_line} 行")
        print(f"✓ 初始化代码已插入到第 {tg_connect_line} 行")
    else:
        print("⚠️  警告：未找到 Telegram 连接位置，需要手动初始化")
    
    # 写回文件
    with open(app_py_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    return True

def copy_healthcheck_to_container(container_id):
    """复制健康检查脚本到容器"""
    import subprocess
    
    print("\n=== 应用方案 4: Docker 健康检查 ===")
    
    # 复制健康检查脚本到容器
    try:
        subprocess.run([
            'docker', 'cp',
            'docker_healthcheck.py',
            f'{container_id}:/app/healthcheck.py'
        ], check=True, capture_output=True)
        print(f"✓ 健康检查脚本已复制到容器")
        return True
    except subprocess.CalledProcessError as e:
        print(f"❌ 复制失败: {e}")
        return False

def main():
    container_id = '65e79462bf76'
    
    print("=" * 60)
    print("开始应用优化方案 2 和 4")
    print("=" * 60)
    
    # 备份当前文件
    backup_time = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    import shutil
    shutil.copy('app.py', f'app.py.bak.{backup_time}')
    print(f"\n✓ 已备份当前 app.py 到 app.py.bak.{backup_time}")
    
    # 应用方案 2
    if not apply_telegram_health_check('app.py'):
        print("\n❌ 方案 2 应用失败")
        return False
    
    # 应用方案 4
    if not copy_healthcheck_to_container(container_id):
        print("\n❌ 方案 4 应用失败")
        return False
    
    print("\n" + "=" * 60)
    print("✅ 所有优化已应用！")
    print("=" * 60)
    print("\n下一步:")
    print("1. 部署更新后的 app.py:")
    print(f"   docker cp app.py {container_id}:/app/app.py")
    print(f"2. 重启容器:")
    print(f"   docker restart {container_id}")
    print("3. 验证健康检查:")
    print(f"   docker inspect --format='{{{{.State.Health.Status}}}}' {container_id}")
    
    return True

if __name__ == '__main__':
    if main():
        sys.exit(0)
    else:
        sys.exit(1)
