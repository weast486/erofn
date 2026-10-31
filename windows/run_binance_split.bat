@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance SOXL 스플릿 봇 (켜 두면 계속 매매, 창을 닫지 마세요)
rem 봇이 끝나 멈춰 있는 예전 스플릿 봇 창은 닫음 (봇이 돌고 있는 창은 그대로)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0close_old_windows.ps1" run_binance_split.bat
git pull
if not exist ".env.binance_split" copy ".env.binance_split.example" ".env.binance_split" >nul
"%PY%" -m binancebot split status
echo.
"%PY%" -m binancebot split run
echo.
echo 기록: state_binance_split\bot.log, 매매 내역: state_binance_split\trades.csv
pause
