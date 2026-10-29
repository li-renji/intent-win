@echo off
title QD Agent
cd /d "%~dp0"
set PY=C:\Users\renji\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python
rem Kill stale agent.app processes (logic lives in kill_old.ps1, avoids cmd quote-escaping traps)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill_old.ps1" >nul 2>&1
"%PY%" -m agent.app
if errorlevel 1 pause
