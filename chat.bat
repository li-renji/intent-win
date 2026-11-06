@echo off
title Intent Chat (one window)
call "%~dp0intent.cmd" %*
if errorlevel 1 pause
