@echo off
chcp 65001 >nul
title INTENT Portable - Publish release
cd /d "%~dp0.."
set "PY=%~dp0..\runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
if "%1"=="" ("%PY%" "%~dp0portable\publish.py" --stage) else ("%PY%" "%~dp0portable\publish.py" %*)
echo.
pause
