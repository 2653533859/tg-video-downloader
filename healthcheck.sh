#!/bin/sh
# Docker 健康检查脚本（使用 Python）

python3 << 'PYEOF'
import sys
import os
try:
    import urllib.request
    import base64

    # 从环境变量读取认证信息
    username = os.getenv("WEB_AUTH_USERNAME", "")
    password = os.getenv("WEB_AUTH_PASSWORD", "")
    port = os.getenv("WEB_BIND_PORT", "5003")

    # 创建认证请求
    url = f"http://localhost:{port}/api/health"

    # 如果配置了认证，添加 Basic Auth 头
    if username and password:
        auth_string = base64.b64encode(f"{username}:{password}".encode()).decode()
    
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Basic {auth_string}")
    else:
        req = urllib.request.Request(url)

    # 发送请求
    with urllib.request.urlopen(req, timeout=5) as response:
        if response.status == 200:
            print("健康检查通过")
            sys.exit(0)
        else:
            print(f"健康检查失败: HTTP {response.status}")
            sys.exit(1)
except Exception as e:
    print(f"健康检查失败: {e}")
    sys.exit(1)
PYEOF
