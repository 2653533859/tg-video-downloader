#!/usr/bin/env python3
"""
自动应用下载监控看门狗补丁到 app.py
"""
import sys

def apply_watchdog_patch(app_py_path):
    # 读取原文件
    with open(app_py_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    # 读取补丁
    with open('watchdog_patch.py', 'r', encoding='utf-8') as f:
        watchdog_code = f.read()
    
    # 查找插入位置（在全局变量定义后，第一个函数定义前）
    insert_line = None
    for i, line in enumerate(lines):
        # 找到 "TDL_RESTART_RESET_MIN_BYTES" 这一行后插入
        if 'TDL_RESTART_RESET_MIN_BYTES' in line:
            insert_line = i + 1
            break
    
    if insert_line is None:
        print("错误：未找到合适的插入位置")
        return False
    
    # 插入看门狗代码
    watchdog_lines = ['\n\n'] + watchdog_code.split('\n')
    watchdog_lines = [line + '\n' for line in watchdog_lines]
    
    # 组装新文件
    new_lines = lines[:insert_line] + watchdog_lines + lines[insert_line:]
    
    # 查找 if __name__ == "__main__": 块，添加启动代码
    main_block_line = None
    for i, line in enumerate(new_lines):
        if 'if __name__ == "__main__":' in line:
            main_block_line = i
            break
    
    if main_block_line is None:
        print("错误：未找到主函数入口")
        return False
    
    # 找到启动线程的位置（在 threading.Thread(target=auto_resume_incomplete_tasks 之后）
    watchdog_start_line = None
    for i in range(main_block_line, len(new_lines)):
        if 'threading.Thread(target=auto_resume_incomplete_tasks' in new_lines[i]:
            watchdog_start_line = i + 1
            break
    
    if watchdog_start_line:
        # 插入看门狗启动代码
        indent = '    '
        startup_code = f'{indent}download_watchdog.start()\n'
        new_lines.insert(watchdog_start_line, startup_code)
    
    # 写入备份
    backup_path = app_py_path + '.bak.' + __import__('datetime').datetime.now().strftime('%Y%m%d%H%M%S')
    with open(backup_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)
    print(f"✓ 原文件已备份到: {backup_path}")
    
    # 写入新文件
    with open(app_py_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    
    print(f"✓ 补丁已应用到: {app_py_path}")
    print(f"✓ 插入位置: 第 {insert_line} 行")
    print(f"✓ 启动代码: 第 {watchdog_start_line} 行")
    return True

if __name__ == '__main__':
    if apply_watchdog_patch('app.py'):
        print("\n成功！监控看门狗已添加到项目中")
        print("重启容器后生效")
    else:
        print("\n失败！请检查错误信息")
        sys.exit(1)
