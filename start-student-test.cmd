@echo off
cd /d "%~dp0"
if /I "%~1"=="--no-monitor" goto iponly
call "%~dp0start-dashboard.cmd" -StudentTest %*
if errorlevel 1 exit /b 1
echo.
echo The IP-sharing launcher has finished or sharing is already running.
echo Open http://127.0.0.1:8765 and check IP sharing settings.
pause
exit /b
:iponly
".venv\Scripts\python.exe" dashboard.py --student-test %*
if errorlevel 1 (
  echo.
  echo IP sharing did not start. Read the error above, then try again.
  pause
  exit /b 1
)
echo.
echo IP sharing has stopped.
pause
