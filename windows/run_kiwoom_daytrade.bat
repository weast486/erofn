@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Daytrade - 단타 봇 (08:45 ETF 매도, 09:00~09:05 매수, 12:00 정리, 15:21 ETF 매수 - 15:30 까지 켜 두기)
git pull
"%PY%" -m kiwoombot run
echo.
echo 오늘 기록: state_kiwoom\daytrade.log
pause
