@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
if errorlevel 1 (
    echo.
    echo Stop failed. See the error above.
    pause
    exit /b 1
)
endlocal
