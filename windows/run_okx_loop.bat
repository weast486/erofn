@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title OKX Loop - 첫 5분봉 FVG 봇 (켜 두면 미국 거래일마다 동부 9:10 에 시작, 창을 닫지 마세요)
git pull
if not exist ".env.okx" (
  copy ".env.okx.example" ".env.okx" >nul
  echo .env.okx 파일을 만들었어요. 키 없이도 드라이런은 됩니다. 실제 주문을 하려면 메모장에서 OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE 를 넣으세요.
  notepad ".env.okx"
)
"%PY%" -m binancebot loop --env .env.okx
echo.
echo 기록: state_okxot.log, 매매 내역: state_okx	rades.csv
pause
