"""바이낸스 미국 주식 선물 단타.

    python -m binancebot download [--symbols TSLAUSDT NVDAUSDT]   # 1분봉·펀딩비 받기 (키 필요 없음, 주문 없음)
    python -m binancebot backtest --orb 15 --target-r 2 --risk 2 --leverage 5
    python -m binancebot backtest --strategy vwap --vwap-dev 2 --stop-pct 2      # VWAP 되돌림
    python -m binancebot backtest --strategy surge --surge-min-change 10 --buy-until 09:35 --stop-pct 7 --take-profit 7 --exit-time 12:00
"""
from __future__ import annotations

import argparse
from datetime import date, time
from pathlib import Path

from .sizing import SizeRule


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m binancebot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    dl = sub.add_parser("download", help="TradFi 무기한 선물 1분봉·펀딩비 받기")
    dl.add_argument("--out", default="data/binance")
    dl.add_argument("--symbols", nargs="*")
    bt = sub.add_parser("backtest", help="당일 단타 백테스트")
    bt.add_argument("--data", default="data/binance")
    bt.add_argument("--symbols", nargs="*")
    bt.add_argument("--capital", type=float, default=1000.0, help="시작 금액 (USDT)")
    bt.add_argument("--start", type=date.fromisoformat)
    bt.add_argument("--end", type=date.fromisoformat)
    bt.add_argument("--strategy", choices=["orb", "vwap", "surge"], default="orb")
    bt.add_argument("--vwap-dev", type=float, default=2.0, help="vwap: VWAP 에서 벗어난 %% 에 지정가 진입")
    bt.add_argument("--vwap-start", type=time.fromisoformat, default=time(10, 0), help="vwap: 이 시각부터 진입")
    bt.add_argument("--buy-until", type=time.fromisoformat, default=time(15, 0), help="vwap·surge: 진입 마감 (미국 동부)")
    bt.add_argument("--stop-pct", type=float, default=2.0, help="vwap·surge: 진입가 대비 손절 %%")
    bt.add_argument("--take-profit", type=float, default=0.0, help="vwap·surge: 익절 %% (0 = vwap 은 VWAP 복귀, surge 는 없음)")
    bt.add_argument("--surge-min-change", type=float, default=10.0, help="surge: 전날 상승률 하한 %%")
    bt.add_argument("--surge-entry", choices=["stop", "next"], default="stop", help="surge: 전날 종가 역지정가 / 다음 1분봉 시가")
    bt.add_argument("--maker-fee", type=float, default=0.02, help="지정가 수수료 %%")
    bt.add_argument("--orb", type=int, default=15, help="시가 범위 분 (장 시작 뒤 N분)")
    bt.add_argument("--direction", choices=["both", "long", "short"], default="both")
    bt.add_argument("--stop-mode", choices=["range", "half"], default="range")
    bt.add_argument("--target-r", type=float, default=2.0, help="익절 = 위험의 R배 (0 = 없음)")
    bt.add_argument("--exit-time", type=time.fromisoformat, default=time(15, 55), help="정리 시각 (미국 동부)")
    bt.add_argument("--min-range", type=float, default=0.0, help="시가 범위 폭 하한 %%")
    bt.add_argument("--max-range", type=float, default=100.0, help="시가 범위 폭 상한 %%")
    bt.add_argument("--min-open-qv", type=float, default=0.0, help="시가 범위 동안 거래대금 하한 (USDT)")
    bt.add_argument("--max-positions", type=int, default=10)
    bt.add_argument("--min-prev-qv", type=float, default=0.0, help="전날 정규장 거래대금 하한 (USDT)")
    bt.add_argument("--underlying", default="EQUITY", help="EQUITY(미국 주식·ETF) / ALL")
    bt.add_argument("--risk", type=float, default=2.0, help="거래당 손절 금액 = 평가금액의 %%")
    bt.add_argument("--leverage", type=float, default=5.0, help="포지션 합계 / 평가금액 상한")
    bt.add_argument("--fee", type=float, default=0.05, help="한쪽 수수료 %%")
    bt.add_argument("--slippage", type=float, default=0.05, help="돌파·손절·정리 한쪽 슬리피지 %%")
    a = ap.parse_args()
    if a.cmd == "download":
        from .data import download_all
        download_all(Path(a.out), a.symbols)
    else:
        from .backtest import Params, run
        p = Params(orb_minutes=a.orb, direction=a.direction, stop_mode=a.stop_mode, target_r=a.target_r,
                   exit_time=a.exit_time, min_range_pct=a.min_range, max_range_pct=a.max_range,
                   min_open_qv=a.min_open_qv, max_positions=a.max_positions,
                   min_prev_qv=a.min_prev_qv, underlying=a.underlying,
                   strategy=a.strategy, vwap_dev=a.vwap_dev, vwap_start=a.vwap_start, buy_until=a.buy_until,
                   stop_pct=a.stop_pct, take_profit_pct=a.take_profit, surge_min_change=a.surge_min_change,
                   maker_fee=a.maker_fee, surge_entry=a.surge_entry,
                   rule=SizeRule(a.risk, a.leverage, a.fee, a.slippage))
        run(Path(a.data), p, a.symbols, a.capital, a.start, a.end)


if __name__ == "__main__":
    main()
