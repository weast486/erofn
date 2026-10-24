@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance SOXL 스플릿 봇 확인 (주문 없음)
if not exist ".env.binance_split" copy ".env.binance_split.example" ".env.binance_split" >nul
"%PY%" -m binancebot split check
echo.
"%PY%" -m binancebot split status
pause
