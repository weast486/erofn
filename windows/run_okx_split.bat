@echo off
cd /d "%~dp0.."
title OKX KORU split
".venv\Scripts\python.exe" -m binancebot.okxsplit run --env .env.okx
pause
