@echo off
title QD Agent
cd /d "%~dp0"
"C:\Users\renji\.workbuddy\binaries\python\versions\3.13.12\python.exe" -m agent.app
if errorlevel 1 pause
