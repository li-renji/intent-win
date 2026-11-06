@echo off
title Intent-Win
cd /d "%~dp0"
rem Use WorkBuddy bundled python (pywebview preinstalled). Path resolved by
rem find_python.ps1 to avoid non-ASCII path issues inside this batch file.
set "PY="
for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_python.ps1"') do set "PY=%%P"
if not defined PY set "PY=python"
rem Kill stale agent processes before starting (avoid running an old instance)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0kill_old.ps1" >nul 2>&1
"%PY%" -m agent.app
if errorlevel 1 pause
