@echo off
chcp 65001 >nul
title 이평선 매매 켜기
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_ma_all.ps1"
timeout /t 5 >nul
