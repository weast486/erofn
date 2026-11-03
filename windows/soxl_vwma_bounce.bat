@echo off
chcp 65001 >nul
title SOXL 5분봉 거래량가중 100선 양봉 2배 롱
cd /d "%~dp0.."
".venv\Scripts\python.exe" scripts\soxl_vwma_bounce.py run
pause
