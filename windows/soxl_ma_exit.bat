@echo off
chcp 65001 >nul
title SOXL 5분봉 11선 이탈 매도 감시
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\soxl_ma_exit.py watch
pause
