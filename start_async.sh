#!/bin/bash
# 异步版本快速启动脚本

echo "================================"
echo "Telegram 下载器 - 异步版本启动"
echo "================================"
echo ""

# 检查 Python 版本
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "✓ Python 版本: $python_version"

# 检查依赖
echo ""
echo "检查依赖..."

if ! python3 -c "import quart" 2>/dev/null; then
    echo "✗ Quart 未安装"
    echo "正在安装异步依赖..."
    pip3 install -r requirements-async.txt
else
    echo "✓ Quart 已安装"
fi

# 检查配置
echo ""
echo "检查配置..."

if [ -z "$TG_API_ID" ]; then
    echo "⚠️  警告: TG_API_ID 未设置"
    echo "   请设置环境变量或在 .env 文件中配置"
fi

if [ -z "$TG_API_HASH" ]; then
    echo "⚠️  警告: TG_API_HASH 未设置"
    echo "   请设置环境变量或在 .env 文件中配置"
fi

# 检查 Session 文件
if [ ! -f "tg_downloader.session" ]; then
    echo "⚠️  警告: 未找到 Session 文件"
    echo "   请先运行: python3 login.py"
fi

echo ""
echo "================================"
echo "启动异步版本..."
echo "================================"
echo ""
echo "访问地址:"
echo "  主页:     http://localhost:5000/"
echo "  实时进度: http://localhost:5000/progress.html"
echo "  WebSocket: ws://localhost:5000/ws/progress"
echo ""
echo "按 Ctrl+C 停止服务器"
echo ""

# 启动应用
python3 app_async.py
