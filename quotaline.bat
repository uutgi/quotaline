@echo off
:: Opens the counter in a small window (refreshes every 60 s). Ctrl+C or close the window to quit.
chcp 65001 >nul
title quotaline
mode con cols=72 lines=14 >nul 2>&1
python "%~dp0quotaline.py" --watch 60
if errorlevel 1 pause
