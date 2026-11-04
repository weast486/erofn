@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title OKX Bot - 실주문 시험 (최소 수량)
"%PY%" -m binancebot okx-test --env .env.okx
pause
