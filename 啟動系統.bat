@echo off
cd /d "%~dp0"
echo Starting ChingTech Meter HMI...

REM Kill any prior production instance (python.exe running main.py without --config).
REM Slave test instance is launched with --config so it will NOT be killed.
REM taskkill /T also kills child processes (pywebview/WebView2).
REM PowerShell command must contain NO double quotes - CMD does not understand \" escaping.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$found=Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*main.py*' -and $_.CommandLine -notlike '*--config*' }; if ($found) { $found | ForEach-Object { Write-Host ('Terminating PID ' + $_.ProcessId); & taskkill.exe /F /T /PID $_.ProcessId | Out-Null } } else { Write-Host 'No existing instances found' }"

REM Wait 1 second for OS to release port/socket
timeout /t 1 /nobreak >nul

REM Launch python in a new window. Change "python" to "pythonw" to hide the console window.
start "" python main.py
exit
