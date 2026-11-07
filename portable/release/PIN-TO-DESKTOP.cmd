@echo off
setlocal
rem Create / update / remove the INTENT desktop shortcut.
rem No driver, no admin, no registry, no background process.
rem The shortcut is drive-letter independent: it calls launch.cmd, which
rem scans A..Z for INTENT-PORTABLE.marker before running START.cmd.
set "SRCDIR=%~dp0portable\desktop"
set "HOSTDIR=%LOCALAPPDATA%\INTENTPortable"
if not defined LOCALAPPDATA set "HOSTDIR=%~dp0data\host"
if /i "%~1"=="/U" goto :uninstall
if /i "%~1"=="/REMOVE" goto :uninstall
if not exist "%SRCDIR%\desktop.vbs" goto :missing
if not exist "%HOSTDIR%" mkdir "%HOSTDIR%" 2>nul
copy /y "%SRCDIR%\launch.cmd" "%HOSTDIR%\launch.cmd" >nul
copy /y "%SRCDIR%\desktop.vbs" "%HOSTDIR%\desktop.vbs" >nul
copy /y "%~dp0intent.ico" "%HOSTDIR%\intent.ico" >nul
rem INTENT_DESKTOP_DIR only exists for the self-test; normal runs let VBS
rem resolve the real Desktop through WScript.Shell.SpecialFolders.
wscript.exe //B "%HOSTDIR%\desktop.vbs" "%HOSTDIR%" "once" "%INTENT_DESKTOP_DIR%"
if errorlevel 1 goto :failed
echo [OK] INTENT Portable shortcut is on the Desktop.
echo      Host files: %HOSTDIR%
echo      Uninstall: PIN-TO-DESKTOP.cmd /U
exit /b 0
:uninstall
if exist "%SRCDIR%\desktop.vbs" wscript.exe //B "%SRCDIR%\desktop.vbs" "%HOSTDIR%" "remove" "%INTENT_DESKTOP_DIR%"
rem Only remove the files we created; never rmdir /s on a user directory.
if exist "%HOSTDIR%\launch.cmd" del /q "%HOSTDIR%\launch.cmd"
if exist "%HOSTDIR%\desktop.vbs" del /q "%HOSTDIR%\desktop.vbs"
if exist "%HOSTDIR%\intent.ico" del /q "%HOSTDIR%\intent.ico"
if exist "%HOSTDIR%\helper.cmd" del /q "%HOSTDIR%\helper.cmd"
if exist "%HOSTDIR%" rmdir "%HOSTDIR%" 2>nul
echo [OK] INTENT desktop shortcut removed.
exit /b 0
:missing
echo [FAILED] portable\desktop\desktop.vbs is missing.
exit /b 1
:failed
echo [FAILED] Could not create the shortcut.
exit /b 1
