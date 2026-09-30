@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 미국 주식 일봉 받기 (백테스트용, 주문 없음)
echo 토스 API 로 미국 S^&P 500 종목 일봉을 받습니다. 10~30분 걸릴 수 있어요. (주문은 하지 않습니다)
"%PY%" -m tossbot.backtest download --source toss-us --start 2022-06-01 --cache data\ohlcv_us
echo.
echo 끝나면 data 폴더의 "ohlcv_us.zip" 파일을 구글 드라이브에 올려 주세요.
explorer data
pause
