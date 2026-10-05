@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 1분봉 받기 (국내 첫 5분봉 FVG 백테스트용, 주문 없음)
echo 2023-09 부터 매일 "전날 거래대금 상위 20" 종목의 1분봉을 받습니다. 약 1만 5천 개, 1~2시간 걸려요.
echo 주문 없음. 끊기면 다시 실행하면 이어받습니다. 봇 매수 시간(15:10~15:20)에는 실행하지 마세요.
echo.
git pull
"%PY%" -m tossbot.backtest download-minute --days reports\first5_kr_days.csv
echo.
echo 끝나면 data 폴더의 "minute_first5_kr_days_1.zip", "_2.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
