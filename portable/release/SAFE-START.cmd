@echo off
chcp 65001 >nul
title INTENT Portable - No key on disk
cd /d "%~dp0"
set "PY=%~dp0runtime\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0portable\boot.py" --safe
if errorlevel 1 pause
