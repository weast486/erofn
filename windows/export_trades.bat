@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 매매현황 내보내기
"%PY%" scripts\export_trades.py
echo.
echo state 폴더의 "매매현황.json" 을 매매 결과 화면에 끌어다 놓으세요.
explorer state
pause
