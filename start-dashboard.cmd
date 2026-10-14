@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup_windows.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup did not finish. Read the message above and the README troubleshooting section.
  pause
  exit /b 1
)
