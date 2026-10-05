@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 바이낸스 비트코인·이더리움 선물 1분봉 받기 (백테스트용, 주문 없음, 키 필요 없음)
echo 비트코인(BTCUSDT)·이더리움(ETHUSDT) 선물 1분봉과 펀딩비를 2026-01-01 부터 받습니다. 몇 분 걸려요.
echo 주문 없음, API 키 필요 없음.
echo.
git pull
"%PY%" -m binancebot download --add BTCUSDT ETHUSDT
echo.
echo 끝나면 data 폴더의 "binance_extra_1.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
