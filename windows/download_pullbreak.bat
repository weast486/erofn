@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 1분봉 받기 (눌림 돌파 백테스트용, 주문 없음)
echo 백테스트용 1분봉을 받습니다. 약 1시간 걸려요. (주문 없음, 끊기면 다시 실행하면 이어받음)
echo 봇 매수 시간(15:10~15:20)에는 실행하지 마세요. 저녁에 실행을 추천합니다.
echo.
git pull
"%PY%" -m tossbot.backtest download-minute --days reports\pullbreak\minute_days.csv
echo.
echo 끝나면 data 폴더의 "minute_..._1.zip", "_2.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
