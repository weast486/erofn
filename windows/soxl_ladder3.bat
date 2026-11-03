@echo off
chcp 65001 >nul
title SOXL 3분할 롱 71.0/70.8/70.6 손절 70.4 익절 73.5
cd /d "%~dp0.."
.venv\Scripts\python.exe scripts\soxl_ladder3.py run
pause
