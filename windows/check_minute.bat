@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 분봉 확인 (저장·주문 없음)
echo 토스 API 로 국내 분봉을 며칠치까지 받을 수 있는지 확인합니다. 1~3분 걸려요. (주문 없음)
echo 봇 매수 시간(15:10~15:20)에는 실행하지 마세요.
echo.
git pull
"%PY%" -m tossbot.backtest probe-minute
echo.
echo 위 결과를 복사해서 보내 주세요. (data\_minute_probe.txt 에도 저장됨)
pause
