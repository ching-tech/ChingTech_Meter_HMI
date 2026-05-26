@echo off
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%logs" mkdir "%APP_DIR%logs"
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -FilePath 'python.exe' -ArgumentList 'main.py' -WorkingDirectory '%APP_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%APP_DIR%logs\startup.out.log' -RedirectStandardError '%APP_DIR%logs\startup.err.log'"
exit
