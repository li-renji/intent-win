@echo off
chcp 65001 >nul
title INTENT Portable - Build factory package
cd /d "%~dp0.."
set "PY=%~dp0..\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\factory.py"
echo.
pause
