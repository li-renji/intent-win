@echo off
chcp 65001 >nul
title INTENT Portable - Window
cd /d "%~dp0"
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\boot.py" --gui
if errorlevel 1 pause
