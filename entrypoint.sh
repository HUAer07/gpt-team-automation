#!/bin/bash
set -e

# 清理旧的锁文件，防止异常退出残留
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99

# 启动后台虚拟显卡 Xvfb
echo "=== 启动 Xvfb 虚拟显卡环境 (DISPLAY=:99) ==="
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset > /dev/null 2>&1 &
export DISPLAY=:99
sleep 1

# 启动 FastAPI 控制台服务
echo "=== 启动 Web 控制台服务 (8899) ==="
exec python -m uvicorn server:app --host 0.0.0.0 --port 8899
