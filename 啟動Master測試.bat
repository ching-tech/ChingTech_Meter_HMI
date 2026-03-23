@echo off
chcp 65001 >nul
title Master (Port 8080)
cd /d "%~dp0"
python main.py
pause
