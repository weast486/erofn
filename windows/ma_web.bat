@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 이평선 매매 화면 (주문 없음)
"%PY%" -m binancebot.maweb --lan
pause
