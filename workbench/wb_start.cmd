@echo off
rem Device Workbench start: ensure backend running then open panel
rem 后端已改为纯标准库实现 (server\app.py)，不再依赖 uvicorn/fastapi/pyyaml
cd /d "%~dp0"
netstat -ano | findstr ":3000" | findstr "LISTENING" >nul
if %errorlevel%==0 (
  echo [OK] Workbench already running: http://localhost:3000
  start "" http://localhost:3000
  exit /b 0
)
set "PY=python"
if exist "%~dp0..\runtime\python\python.exe" set "PY=%~dp0..\runtime\python\python.exe"
start "workbench" cmd /k ""%PY%" server\app.py"
ping -n 3 127.0.0.1 >nul
start "" http://localhost:3000
echo [OK] Workbench started: http://localhost:3000
exit /b 0
