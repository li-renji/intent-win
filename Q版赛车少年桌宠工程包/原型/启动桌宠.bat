@echo off
cd /d "%~dp0" || goto BADCD
echo Starting Racer Boy Desktop Pet ...
echo Click=Wave   DoubleClick=Jump   Drag=Move   RightClick=Menu
echo Close: right-click the pet and pick Quit, or press Ctrl+C here.
echo You can minimize this window while the pet is running.
echo.
if not exist "C:\Python314\python.exe" goto NOPY
"C:\Python314\python.exe" "desktop_pet.py"
if errorlevel 1 goto FAILED
exit /b 0

:NOPY
echo [ERROR] Python not found: C:\Python314\python.exe
echo Please edit this .bat and point it to a Python with tkinter + Pillow.
echo.
pause
exit /b 1

:BADCD
echo [ERROR] Cannot enter folder: %~dp0
pause
exit /b 1

:FAILED
echo [ERROR] Pet exited with an error. See the message above.
pause
exit /b 1
