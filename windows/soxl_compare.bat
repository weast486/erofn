@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title SOXL 편입 종목 비교
python -m usbot.soxlweb
pause
