@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance 조건 주문 감시 - 테스트 매매만 (실제 주문 없음, 창을 닫으면 감시가 멈춰요)
if not exist ".env.binance" copy ".env.binance.example" ".env.binance" >nul
"%PY%" -m binancebot manual list
echo.
"%PY%" -m binancebot manual run
echo.
echo 기록: state_binance\manual\manual.log, 테스트 매매 내역: state_binance\manual\trades.csv
pause
