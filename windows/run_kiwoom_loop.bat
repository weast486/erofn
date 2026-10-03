@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Loop - 단타 봇 (켜 두면 거래일마다 08:40 에 시작, 창을 닫지 마세요)
git pull
"%PY%" -m kiwoombot loop
echo.
echo 기록: state_kiwoom\daytrade.log
pause
