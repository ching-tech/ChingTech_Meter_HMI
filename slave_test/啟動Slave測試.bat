@echo off
chcp 65001 >nul
title Slave (Port 8081)
cd /d "%~dp0\.."
python main.py --config slave_test\config.json
pause
