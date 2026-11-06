@echo off
chcp 65001 >nul
title 明日方舟QQ角色聊天机器人 Launcher
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo ========================================
echo   明日方舟QQ角色聊天机器人
echo   请先启动 Docker Desktop
echo   SnowLuma 反向 WebSocket：ws://127.0.0.1:8080
echo ========================================

echo [1/3] SnowLuma
docker inspect snowluma >nul 2>&1
if errorlevel 1 (
    echo 未检测到 snowluma 容器，请先按环境配置教程.txt 创建
) else (
    docker start snowluma
    timeout /t 5 /nobreak >nul
)

echo [2/3] WebUI  http://127.0.0.1:8090
start "明日方舟QQ角色聊天机器人 WebUI" /min "%PYTHON_EXE%" part_web.py
timeout /t 3 /nobreak >nul
start "" http://127.0.0.1:8090

echo [3/3] QQ-BOT
"%PYTHON_EXE%" QQ-BOT.py

echo.
echo 程序已停止，按任意键关闭...
pause >nul
