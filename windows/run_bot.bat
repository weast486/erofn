@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - RUN
echo [1/2] 최신 코드 받는 중 (git pull)...
git pull
echo.
echo [2/2] 봇 실행 - 끄려면 이 창에서 Ctrl + C
echo.
"%PY%" -m tossbot run
echo.
echo 봇이 종료되었습니다.
pause
