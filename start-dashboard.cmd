@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing. Follow the installation steps in README.md first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" tools\launch_dashboard.py %*
if errorlevel 1 pause
