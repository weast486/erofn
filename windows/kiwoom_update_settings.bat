@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Daytrade - 설정을 최신 추천값으로 바꾸기
git pull
if not exist ".env.kiwoom" (
  copy ".env.kiwoom.example" ".env.kiwoom" >nul
  echo .env.kiwoom 파일을 새로 만들었어요. 메모장으로 열어 KIWOOM_APP_KEY, KIWOOM_SECRET_KEY 를 넣어 주세요.
  notepad ".env.kiwoom"
  pause
  exit /b
)
"%PY%" -m kiwoombot update-settings
echo.
echo 키와 모의투자/드라이런 설정은 그대로 두었어요. 이전 파일은 .env.kiwoom.bak 에 있어요.
pause
