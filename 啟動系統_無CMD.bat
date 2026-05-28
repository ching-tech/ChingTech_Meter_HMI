@echo off
set "APP_DIR=%~dp0"
if not exist "%APP_DIR%logs" mkdir "%APP_DIR%logs"

REM Kill any prior production instance (python.exe running main.py without --config).
REM Slave test instance is launched with --config so it will NOT be killed.
REM taskkill /T also kills child processes (pywebview/WebView2).
REM PowerShell command must contain NO double quotes - CMD does not understand \" escaping.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$found=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*--config*' }; if ($found) { $found | ForEach-Object { Write-Host ('Terminating PID ' + $_.ProcessId); & taskkill.exe /F /T /PID $_.ProcessId | Out-Null } }"

REM Wait 1 second for OS to release port/socket
timeout /t 1 /nobreak >nul

REM Launch python hidden, redirect stdout/stderr to log files
powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "Start-Process -FilePath 'python.exe' -ArgumentList 'main.py' -WorkingDirectory '%APP_DIR%' -WindowStyle Hidden -RedirectStandardOutput '%APP_DIR%logs\startup.out.log' -RedirectStandardError '%APP_DIR%logs\startup.err.log'"
exit
