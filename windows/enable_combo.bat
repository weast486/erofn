@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Toss Bot - 신고가+RSI 전략 켜기
if not exist ".env" (
  echo .env 파일이 없습니다.
  pause
  exit /b 1
)
copy /y .env .env.bak >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = (Resolve-Path '.env').Path; $l = [IO.File]::ReadAllLines($p); if ($l -match '^STRATEGY=') { $l = $l -replace '^STRATEGY=.*', 'STRATEGY=combo' } else { $l += 'STRATEGY=combo' }; [IO.File]::WriteAllLines($p, $l, (New-Object Text.UTF8Encoding $false))"
echo.
echo 전략을 "신고가 + RSI 한 계좌(combo)" 로 바꿨습니다. (바꾸기 전 파일: .env.bak)
echo 켜져 있는 봇 창에서 Ctrl + C 로 끈 뒤, 토스봇 실행(run_bot.bat)을 다시 실행하세요.
echo.
pause
