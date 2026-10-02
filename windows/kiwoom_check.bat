@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Daytrade - 연결 확인 (주문 없음)
git pull
if not exist ".env.kiwoom" (
  copy ".env.kiwoom.example" ".env.kiwoom" >nul
  echo .env.kiwoom 파일을 만들었어요. 메모장으로 열어 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 를 넣고 다시 실행하세요.
  notepad ".env.kiwoom"
  pause
  exit /b
)
"%PY%" -m kiwoombot check
pause
