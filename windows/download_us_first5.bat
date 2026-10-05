@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 미국 주식 거래대금 상위 1분봉 받기 (첫 5분봉 돌파 검증용, 토스 API 조회만, 주문 없음)
echo 2023년부터 매일 "전날 거래대금 상위" 종목의 1분봉을 토스 API 로 받습니다. 약 1만 5천 건, 2~4시간 걸려요.
echo 주문 없음. 끊기면 다시 실행하면 이어받습니다.
echo.
git pull
"%PY%" scripts\download_us_surge.py --extra-days reports\first5_us_days.csv %*
echo.
echo 끝나면 data 폴더의 "us_first5_1.zip", "us_first5_2.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
