@echo off
chcp 65001 >nul
title INTENT Portable - Check
cd /d "%~dp0"
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\boot.py" --doctor
echo.
pause
