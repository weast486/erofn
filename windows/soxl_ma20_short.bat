@echo off
chcp 65001 >nul
title SOXL 1시간봉 20선 이탈 숏 2배 손절 +5% 익절 -10%
cd /d "%~dp0.."
.venv\Scripts\python.exe scripts\soxl_ma20_short.py run
pause
