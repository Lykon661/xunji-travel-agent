@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test.ps1"
set code=%errorlevel%
endlocal & exit /b %code%
