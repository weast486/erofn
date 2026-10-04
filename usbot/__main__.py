"""토스증권 미국 주식 당일 단타 연구.

    python -m usbot download [--symbols TSLA NVDA] [--start 2025-10-01]   # 조회만, 주문 없음
    python -m usbot backtest --orb 15 --target-r 2
"""
from __future__ import annotations

import argparse
from datetime import date, time
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m usbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dl = sub.add_parser("download", help="토스 API 로 미국 주식 일봉·정규장 1분봉 받기")
    dl.add_argument("--out", default="data/tossus")
    dl.add_argument("--symbols", nargs="*", help="기본: 대형주 + 인기 ETF 20개")
    dl.add_argument("--start", type=date.fromisoformat, help="받기 시작 날짜 (기본 1년 전)")
    dl.add_argument("--env", default=".env")
    bt = sub.add_parser("backtest", help="시가 범위 돌파(매수만) 당일 단타 백테스트")
    bt.add_argument("--data", default="data/tossus")
    bt.add_argument("--symbols", nargs="*")
    bt.add_argument("--capital", type=float, default=10000.0, help="시작 금액 (달러)")
    bt.add_argument("--start", type=date.fromisoformat)
    bt.add_argument("--end", type=date.fromisoformat)
    bt.add_argument("--orb", type=int, default=15, help="시가 범위 분 (장 시작 뒤 N분)")
    bt.add_argument("--stop-mode", choices=["range", "half"], default="range")
    bt.add_argument("--target-r", type=float, default=2.0, help="익절 = 위험의 R배 (0 = 없음)")
    bt.add_argument("--exit-time", type=time.fromisoformat, default=time(15, 55), help="정리 시각 (미국 동부)")
    bt.add_argument("--min-range", type=float, default=0.0, help="시가 범위 폭 하한 %%")
    bt.add_argument("--max-range", type=float, default=100.0, help="시가 범위 폭 상한 %%")
    bt.add_argument("--min-open-qv", type=float, default=0.0, help="시가 범위 동안 거래대금 하한 (달러)")
    bt.add_argument("--max-positions", type=int, default=5)
    bt.add_argument("--position-pct", type=float, default=20.0, help="종목당 매수 금액 = 평가금액의 %%")
    bt.add_argument("--fee", type=float, default=0.1, help="한쪽 수수료 %%")
    bt.add_argument("--slippage", type=float, default=0.05, help="돌파·손절·정리 한쪽 슬리피지 %%")
    a = ap.parse_args()
    if a.cmd == "download":
        from .data import download_all
        download_all(Path(a.out), a.symbols, a.start, a.env)
    else:
        from binancebot.backtest import Params

        from .backtest import run
        p = Params(orb_minutes=a.orb, direction="long", stop_mode=a.stop_mode, target_r=a.target_r,
                   exit_time=a.exit_time, min_range_pct=a.min_range, max_range_pct=a.max_range,
                   min_open_qv=a.min_open_qv, max_positions=a.max_positions)
        run(Path(a.data), p, a.symbols, a.capital, a.start, a.end, a.position_pct, a.fee, a.slippage)


if __name__ == "__main__":
    main()
