@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance Loop - 첫 5분봉 FVG 봇 (켜 두면 미국 거래일마다 동부 9:10 에 시작, 창을 닫지 마세요)
git pull
if not exist ".env.binance" copy ".env.binance.example" ".env.binance" >nul
"%PY%" -m binancebot loop
echo.
echo 기록: state_binance\bot.log, 매매 내역: state_binance\trades.csv
pause
