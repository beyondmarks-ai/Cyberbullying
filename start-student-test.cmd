@echo off
cd /d "%~dp0"
if /I "%~1"=="--no-monitor" goto iponly
call "%~dp0start-dashboard.cmd" -StudentTest %*
exit /b
:iponly
".venv\Scripts\python.exe" dashboard.py --student-test %*
