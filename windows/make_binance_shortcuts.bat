@echo off
chcp 65001 >nul
cd /d "%~dp0.."
git pull
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_binance_shortcuts.ps1"
pause
