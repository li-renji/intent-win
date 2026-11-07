@echo off
rem Desktop shortcut target. Finds the INTENT U disk by its marker file,
rem so the shortcut keeps working when the drive letter changes.
setlocal
for %%d in (C D E F G H I J K L M N O P Q R S T U V W X Y Z) do (
  if exist "%%d:\INTENT-PORTABLE.marker" (
    if exist "%%d:\START.cmd" (
      if defined INTENT_LAUNCH_DRYRUN (
        echo FOUND %%d:
        exit /b 0
      )
      call "%%d:\START.cmd" %*
      exit /b %ERRORLEVEL%
    )
  )
)
echo [INTENT] No INTENT U disk found. Plug it in and try again.
echo.
pause
exit /b 1
