@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build.ps1"
if errorlevel 1 (
    echo.
    echo Packaging failed. See the error above.
    pause
    exit /b 1
)
endlocal
