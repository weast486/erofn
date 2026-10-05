@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance Bot - 설정을 최신으로 (API 키 유지)
git pull
"%PY%" -m binancebot update-settings
echo.
echo 이제 windows\binance_check.bat 로 확인한 뒤 windows\run_binance_loop.bat 를 켜 두세요.
pause
