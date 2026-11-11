@echo off
chcp 65001 >nul
title SOXL 5분봉 50선 4분할 매수 2배 익절 +6% 손절 -2%
cd /d "%~dp0.."
.venv\Scripts\python.exe scripts\soxl_ma50_ladder.py run
pause
