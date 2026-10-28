#!/usr/bin/env bash
# 一键启动并自动在浏览器打开控制台
set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

PORT=8899
HOST=127.0.0.1

echo "========================================================="
echo "  ChatGPT Team 自动化控制台启动中..."
echo "  正在释放端口 ${PORT} (如有残留进程)..."
lsof -ti :${PORT} | xargs kill -9 2>/dev/null || true

echo "  正在启动服务: http://${HOST}:${PORT}"
echo "========================================================="

# 延迟 1.5 秒自动唤起浏览器打开页面
(sleep 1.5 && open "http://${HOST}:${PORT}") &

# 启动 uvicorn (不加 --reload 避免外置硬盘全盘文件扫描卡死)
exec ./venv/bin/python -m uvicorn server:app --host ${HOST} --port ${PORT}
