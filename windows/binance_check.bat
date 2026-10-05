@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Binance Bot - 연결 확인 (주문 없음)
git pull
if not exist ".env.binance" (
  copy ".env.binance.example" ".env.binance" >nul
  echo .env.binance 파일을 만들었어요. 키 없이도 드라이런은 됩니다. 실제 주문을 하려면 메모장에서 BINANCE_API_KEY, BINANCE_API_SECRET 을 넣으세요.
  notepad ".env.binance"
)
"%PY%" -m binancebot check
pause
