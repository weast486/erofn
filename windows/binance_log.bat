@echo off
chcp 65001 >nul
cd /d "%~dp0.."
title Binance Bot - 기록 보기
if not exist "state_binance" mkdir "state_binance"
echo 바이낸스 봇 기록 폴더를 엽니다.
echo   bot.log    : 진행 기록 (오류도 여기에)
echo   trades.csv : 매매 내역 (엑셀로 열림)
echo.
if exist "state_binance\bot.log" (
  echo ---- bot.log 마지막 30줄 ----
  powershell -NoProfile -Command "Get-Content -Encoding UTF8 -Tail 30 'state_binance\bot.log'"
) else (
  echo 아직 기록이 없어요. 봇을 한 번도 실행하지 않았거나 장이 아직 안 열렸어요.
)
explorer "state_binance"
echo.
pause
