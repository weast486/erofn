@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 이평선 실거래 봇 (켜 두면 화면 요청대로 주문, 창을 닫지 마세요)
if not exist ".env.ma" copy ".env.ma.example" ".env.ma" >nul
"%PY%" -m binancebot.mabot check --env .env.ma
echo.
echo 이평선 매매 화면(http://localhost:8790)의 '실거래' 칸에서 시작하세요. 화면은 windows\ma_web.bat
"%PY%" -m binancebot.mabot run --env .env.ma
echo.
echo 기록: state_ma\bot.log, 매매 내역: state_ma\trades.csv
pause
