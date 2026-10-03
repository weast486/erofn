@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Kiwoom Reserve - 적립금 보기/고치기
"%PY%" -m kiwoombot reserve
echo.
echo 적립금을 직접 고치려면 새 금액(원)을 숫자로 입력하고 Enter. 그대로 두려면 그냥 Enter.
set "AMT="
set /p "AMT=새 적립금: "
if not "%AMT%"=="" "%PY%" -m kiwoombot reserve --set %AMT%
echo.
pause
