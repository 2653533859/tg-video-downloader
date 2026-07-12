#!/usr/bin/env python3
"""修复 Telegram 健康检查初始化"""

with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

# 找到 "log_info(f\"Telegram 已连接:" 这一行，在其后添加初始化
for i, line in enumerate(lines):
    if 'log_info(f"Telegram 已连接:' in line:
        # 获取缩进
        indent = len(line) - len(line.lstrip())
        spaces = ' ' * indent
        # 在下一行插入初始化代码
        init_line = f'{spaces}init_tg_health_checker()\n'
        lines.insert(i + 1, init_line)
        print(f"✓ 已在第 {i+2} 行添加 init_tg_health_checker() 调用")
        break

with open('app.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("✓ 修复完成")
