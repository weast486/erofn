@echo off
chcp 65001 >nul
title SOXL 10분할 스플릿 (익절 = 위 차수 +0.1%, 5배)
cd /d "%~dp0.."
.venv\Scripts\python.exe scripts\soxl_grid10.py run
pause
