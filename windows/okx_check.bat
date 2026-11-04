@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title OKX Bot - 연결 확인 (주문 없음)
git pull
if not exist ".env.okx" (
  copy ".env.okx.example" ".env.okx" >nul
  echo .env.okx 파일을 만들었어요. 키 없이도 드라이런은 됩니다. 실제 주문을 하려면 메모장에서 OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE 를 넣으세요.
  notepad ".env.okx"
)
"%PY%" -m binancebot check --env .env.okx
pause
