@echo off
rem Global command: type "intent" in any terminal to start the Agent here.
rem Keep this file ASCII + CRLF so cmd.exe reads it under any code page.
rem Single source of truth: chat.bat (double-click) calls this file too.
rem No cd / no chcp: running "intent" must not change the user's terminal.
setlocal
set "PY="
for /f "delims=" %%P in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_python.ps1"') do set "PY=%%P"
if not defined PY set "PY=python"
"%PY%" "%~dp0chat.py" %*
exit /b %ERRORLEVEL%
