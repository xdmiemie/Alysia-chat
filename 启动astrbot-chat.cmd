@echo off
setlocal
chcp 65001 >nul
title astrbot-chat
cd /d "%~dp0"
if not exist "%~dp0runtime_env\python.exe" (
    echo Missing runtime_env\python.exe. Restore the project runtime first.
    pause
    exit /b 1
)
"%~dp0runtime_env\python.exe" -X utf8 "%~dp0scripts\qq_companion\open_app.py"
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
exit /b 0
