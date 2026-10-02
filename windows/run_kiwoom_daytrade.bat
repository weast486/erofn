@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Daytrade - 단타 봇 (08:50 대상 고르기, 09:00~09:05 매수, 12:00 정리)
git pull
"%PY%" -m kiwoombot run
echo.
echo 오늘 기록: state_kiwoom\daytrade.log
pause
