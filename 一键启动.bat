@echo off
chcp 65001 >nul
title ARK-BOT Launcher
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo ========================================
echo   ARK-BOT
echo   请先启动 NapCat，并连接反向 WebSocket：
echo   ws://127.0.0.1:8080
echo ========================================

echo [1/2] WebUI  http://127.0.0.1:8090
start "ARK-BOT WebUI" /min "%PYTHON_EXE%" part_web.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8090

echo [2/2] QQ-BOT
"%PYTHON_EXE%" QQ-BOT.py

echo.
echo 程序已停止，按任意键关闭...
pause >nul
