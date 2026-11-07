# 设备统一管理工作台 - 启动脚本 (Windows CMD)
# 用法: start.cmd
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 启动工作台后端 (http://localhost:3000) ...
set "PY=python"
if exist "%~dp0..\runtime\python\python.exe" set "PY=%~dp0..\runtime\python\python.exe"
start "workbench" cmd /k ""%PY%" server\app.py"
echo [2/2] 工作台首页: http://localhost:3000
echo       其它设备免安装加入: 同一局域网打开 http://本机IP:3000/node
echo       左侧导航: 总览 / 设备 / 用量 / 额度雷达
pause