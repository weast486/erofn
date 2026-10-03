@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 바이낸스 미국주식 선물 1분봉 받기 (백테스트용, 주문 없음, 키 필요 없음)
echo 바이낸스 선물 미국 주식 종목의 1분봉과 펀딩비를 받습니다. 20~40분 걸려요.
echo 주문 없음, API 키 필요 없음. 끊기면 다시 실행하면 이어받습니다.
echo.
git pull
"%PY%" -m binancebot download
echo.
echo 끝나면 data 폴더의 "binance_1.zip", "binance_2.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
