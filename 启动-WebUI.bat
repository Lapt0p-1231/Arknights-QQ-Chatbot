@echo off
chcp 65001 >nul
title ARK-BOT WebUI
cd /d "%~dp0"
set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"
echo ========================================
echo   ARK-BOT WebUI
echo   http://127.0.0.1:8090
echo ========================================
"%PYTHON_EXE%" part_web.py
echo.
echo 程序已停止，按任意键关闭...
pause >nul
