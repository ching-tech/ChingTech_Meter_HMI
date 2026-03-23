@echo off
cd /d "%~dp0"
echo Starting ChingTech Meter HMI...
:: 使用 start 啟動 python，這會開啟一個新的 python 視窗，但 batch 視窗會關閉
:: 如果不想看到 python 黑色視窗，請將 python 改回 pythonw
start "" python main.py
exit
