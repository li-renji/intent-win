# 设备统一管理工作台 - 启动脚本 (Windows CMD)
# 用法: start.cmd
@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [1/2] 启动工作台后端 (http://localhost:3000) ...
set PY=python
if exist "C:\Users\renji\.workbuddy\binaries\python\versions\3.13.12\python.exe" set PY=C:\Users\renji\.workbuddy\binaries\python\versions\3.13.12\python.exe
start "workbench" cmd /k ""%PY%" server\app.py"
echo [2/2] 请在浏览器打开 http://localhost:3000
echo 管理 MeshCentral: cd deploy ^&^& node node_modules/meshcentral --configfile mc_config.json
pause