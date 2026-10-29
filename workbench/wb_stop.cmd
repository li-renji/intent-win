@echo off
rem Device Workbench stop
cd /d "%~dp0"
set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":3000" ^| findstr "LISTENING"') do (
  taskkill /f /pid %%a >nul 2>&1
  set FOUND=1
)
if %FOUND%==1 (
  echo [OK] Workbench stopped
) else (
  echo [OK] Workbench not running
)
exit /b 0
