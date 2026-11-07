#!/bin/bash
# 设备统一管理工作台 - 启动脚本 (Git Bash / Linux)
cd "$(dirname "$0")"
echo "[1/3] 启动 workbench 后端 (http://localhost:3000) ..."
python -m uvicorn server.app:app --host 0.0.0.0 --port 3000 &
WB_PID=$!
sleep 2
echo "[2/3] 状态检查:"
curl -s http://127.0.0.1:3000/api/devices | python -m json.tool | head -20
echo "[3/3] 浏览器打开 http://localhost:3000 ; Ctrl-C 停止 (PID $WB_PID)"
wait $WB_PID