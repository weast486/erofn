@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 바이낸스 한국 관련 종목 1분봉 받기 (한국 장 시간, 백테스트용, 주문 없음, 키 필요 없음)
echo EWY, KORU, SKHY, SKUU 의 한국 장 시간(UTC 0~7시 = 한국 9~16시) 1분봉을 받습니다. 몇 분 걸려요.
echo 주문 없음, API 키 필요 없음. 실행 중인 바이낸스 봇에는 영향 없어요.
echo.
git pull
"%PY%" -m binancebot download --out data/binance_kr --utc-hours 0 7 --symbols EWYUSDT KORUUSDT SKHYUSDT SKUUUSDT
echo.
echo 끝나면 data 폴더의 "binance_kr_1.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
