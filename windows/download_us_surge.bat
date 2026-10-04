@echo off
chcp 65001 >nul
cd /d "%~dp0.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
title 미국 주식 급등 다음 날 1분봉 받기 (백테스트용, 토스 API 조회만, 주문 없음)
echo 미국 주식 일봉과 "전날 +5%% 이상 오른 다음 날" 1분봉을 토스 API 로 받습니다. 처음에는 1~3시간 걸려요.
echo 주문 없음. 끊기면 다시 실행하면 이어받습니다.
echo.
git pull
"%PY%" scripts\download_us_surge.py %*
echo.
echo 끝나면 data 폴더의 "us_surge_1.zip", "us_surge_2.zip" ... 파일을 모두 구글 드라이브에 올려 주세요.
explorer data
pause
