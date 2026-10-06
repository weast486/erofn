@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title Toss Bot - 한 종목 즉시 매도
echo 봇이 보유한 종목:
echo.
"%PY%" -m tossbot status
echo.
set "CODE="
set /p CODE=즉시 팔 종목코드 (여러 개는 띄어쓰기, 그냥 Enter = 취소): 
if "%CODE%"=="" goto :end
echo.
"%PY%" -m tossbot sell %CODE%
:end
echo.
pause
