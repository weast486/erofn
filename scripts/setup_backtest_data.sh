#!/usr/bin/env bash
# 백테스트 데이터 준비 (새 세션/새 PC에서 한 번 실행). data/ 는 git 에 올리지 않는다.
#  - FinanceData/marcap: KRX 전 종목 일별 (상장폐지 포함, 시가총액·시장 구분 포함)
#  - data/ohlcv_long: 2022-09 ~ 최신, 액면분할 보정한 종목별 일봉 CSV + 코스피/코스닥 지수
set -euo pipefail
cd "$(dirname "$0")/.."
pip install -q -r requirements-backtest.txt
if [ ! -d data/marcap ]; then
  git clone --depth 1 --filter=blob:none --no-checkout https://github.com/FinanceData/marcap data/marcap
fi
files=""
for y in $(seq 2022 "$(date +%Y)"); do files="$files data/marcap-$y.parquet"; done
git -C data/marcap checkout HEAD -- $files
python -m tossbot.backtest download --source marcap --marcap-dir data/marcap/data --start 2022-09-01 --cache data/ohlcv_long
echo "완료: data/ohlcv_long (일봉), data/marcap/data (시가총액)"
