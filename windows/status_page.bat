@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 봇 매매현황 만드는 중...
echo 토스 현재가 조회 중...
"%PY%" scripts\export_trades.py
if errorlevel 1 echo (현재가 조회 실패 - 마지막으로 조회한 가격으로 표시합니다)
"%PY%" scripts\make_status_page.py --local --out "state\매매현황_로컬.html"
if errorlevel 1 (
  echo 페이지를 만들지 못했어요. 위 오류 내용을 확인하세요.
  pause
  exit /b 1
)
start "" "state\매매현황_로컬.html"
