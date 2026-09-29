@echo off
rem 연구용: 신고가 매수 종목의 투자자별 순매수 분석 (토스 API 시세 조회만, 주문 없음)
rem 이 폴더(연구용 작업 폴더)에서 실행. 토스 키는 봇 폴더의 .env 를 읽는다.
chcp 65001 >nul
cd /d "%~dp0.."
set "BOT=E:\erofn"
set "PY=python"
if exist "%BOT%\.venv\Scripts\python.exe" set "PY=%BOT%\.venv\Scripts\python.exe"
title 투자자별 순매수 분석
echo [0/2] 필요한 부품(pandas) 확인...
"%PY%" -c "import pandas" 2>nul || "%PY%" -m pip install pandas
echo.
echo [1/2] 3건만 시험 조회...
"%PY%" scripts\investor_flow.py --env "%BOT%\.env" --limit 3
if errorlevel 1 goto end
echo.
echo [2/2] 전체 510건 조회 (몇 분 걸립니다)...
"%PY%" scripts\investor_flow.py --env "%BOT%\.env"
echo.
echo 완료: reports\investor_flow\summary.csv, trades_investor.csv
:end
pause
