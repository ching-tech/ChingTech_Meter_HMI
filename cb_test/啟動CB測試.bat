@echo off
chcp 65001 >nul
title CB Flow Test (single thermometer)
cd /d "%~dp0"
echo === Earpiece Thermometer CB(ACK) Flow Test ===
echo.
set /p MAC="Enter thermometer MAC (e.g. 00:18:E4:34:D2:1A): "
python test_cb_flow.py %MAC%
pause
