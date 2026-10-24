@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance SOXL 스플릿 봇 (켜 두면 계속 매매, 창을 닫지 마세요)
git pull
if not exist ".env.binance_split" copy ".env.binance_split.example" ".env.binance_split" >nul
"%PY%" -m binancebot split status
echo.
"%PY%" -m binancebot split run
echo.
echo 기록: state_binance_split\bot.log, 매매 내역: state_binance_split\trades.csv
pause
