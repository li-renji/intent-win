@echo off
chcp 65001 >nul
title INTENT Portable - Push update to USB
cd /d "%~dp0.."
set "PY=%~dp0..\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\push.py" %*
echo.
pause
