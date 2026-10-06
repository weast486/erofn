@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 봇 매매현황 (http://localhost:8765 - 이 창을 닫으면 페이지도 꺼집니다)
"%PY%" scripts\status_server.py %*
if errorlevel 1 pause
