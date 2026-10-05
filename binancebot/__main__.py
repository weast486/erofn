"""바이낸스 미국 주식 선물 단타.

    python -m binancebot download [--symbols TSLAUSDT NVDAUSDT]   # 1분봉·펀딩비 받기 (키 필요 없음, 주문 없음)
    python -m binancebot backtest --orb 15 --target-r 2 --risk 2 --leverage 5
    python -m binancebot backtest --strategy vwap --vwap-dev 2 --stop-pct 2      # VWAP 되돌림
    python -m binancebot backtest --strategy flag --symbols SOXLUSDT --flag-pole-pct 3   # 15분봉 상승 깃발형
    python -m binancebot backtest --strategy surge --surge-min-change 10 --buy-until 09:35 --stop-pct 7 --take-profit 7 --exit-time 12:00

자동매매 봇 (첫 5분봉 + 1분봉 FVG, 설정 .env.binance, 기본 드라이런):
    python -m binancebot check        # 키·잔고·오늘 대상 종목 확인 (주문 없음)
    python -m binancebot run          # 오늘 하루 매매
    python -m binancebot loop         # 켜 둔 채 미국 거래일마다 반복
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
    dl.add_argument("--add", nargs="*", help="TradFi 목록은 두고 이 종목만 추가로 받기 (예 BTCUSDT ETHUSDT)")
    dl.add_argument("--since", default="2026-01-01", help="--add 종목을 받을 시작 날짜")
    bt = sub.add_parser("backtest", help="당일 단타 백테스트")
    bt.add_argument("--data", default="data/binance")
    bt.add_argument("--symbols", nargs="*")
    bt.add_argument("--capital", type=float, default=1000.0, help="시작 금액 (USDT)")
    bt.add_argument("--start", type=date.fromisoformat)
    bt.add_argument("--end", type=date.fromisoformat)
    bt.add_argument("--strategy", choices=["orb", "vwap", "surge", "vwma", "swing", "flag", "first5", "vbreak", "rsi", "boll", "fvg"], default="orb")
    bt.add_argument("--vwap-dev", type=float, default=2.0, help="vwap: VWAP 에서 벗어난 %% 에 지정가 진입")
    bt.add_argument("--vwap-start", type=time.fromisoformat, default=time(10, 0), help="vwap·vwma: 이 시각부터 진입")
    bt.add_argument("--buy-until", type=time.fromisoformat, default=time(15, 0), help="vwap·surge: 진입 마감 (미국 동부)")
    bt.add_argument("--stop-pct", type=float, default=2.0, help="vwap·surge: 진입가 대비 손절 %%")
    bt.add_argument("--take-profit", type=float, default=0.0, help="vwap·surge: 익절 %% (0 = vwap 은 VWAP 복귀, surge 는 없음)")
    bt.add_argument("--surge-min-change", type=float, default=10.0, help="surge: 전날 상승률 하한 %%")
    bt.add_argument("--surge-entry", choices=["stop", "next"], default="stop", help="surge: 전날 종가 역지정가 / 다음 1분봉 시가")
    bt.add_argument("--vwma-tf", type=int, default=15, help="vwma: 분봉 단위")
    bt.add_argument("--vwma-len", type=int, default=100)
    bt.add_argument("--vwma-slope-bars", type=int, default=1, help="vwma: VWMA 가 N봉 전보다 높으면 상승 (0 = 기울기 조건 없음)")
    bt.add_argument("--vwma-min-above", type=int, default=20, help="vwma: 종가가 VWMA 위(아래)에 있던 연속 봉 수")
    bt.add_argument("--vwma-band", type=float, default=0.0, help="vwma: 지정가 = VWMA x (1+band%%), 숏은 반대")
    bt.add_argument("--vwma-source", choices=["all", "rth"], default="all", help="vwma: 받아 둔 봉 전부 / 정규장 봉만")
    bt.add_argument("--vwma-stop-basis", choices=["entry", "vwma"], default="entry")
    bt.add_argument("--vwma-rsi-period", type=int, default=14)
    bt.add_argument("--vwma-rsi-long-min", type=float, default=0.0, help="vwma: 롱은 직전 봉 RSI 이 값 이상 (숏은 100-값 이하)")
    bt.add_argument("--vwma-rsi-long-max", type=float, default=100.0, help="vwma: 롱은 직전 봉 RSI 이 값 이하 (숏은 100-값 이상)")
    bt.add_argument("--vwma-max-touch", type=int, default=0, help="vwma: VWMA 한쪽에 자리 잡은 뒤 N번째 닿음까지만 (0 = 제한 없음)")
    bt.add_argument("--vwma-target-r", type=float, default=0.0, help="vwma: 익절 = 위험의 R배")
    bt.add_argument("--swing-tf", type=int, default=15, help="swing: 분봉 단위")
    bt.add_argument("--swing-n", type=int, default=3, help="swing: 고점·저점 = 좌우 N봉 중 최고·최저")
    bt.add_argument("--swing-buy1", type=float, default=0.5, help="swing: 1차 매수 = 저점2 + (고점2-저점2) x 값")
    bt.add_argument("--swing-buy2", type=float, default=0.25, help="swing: 2차 매수")
    bt.add_argument("--swing-tp2", type=float, default=0.5, help="swing: 전량 매도 = 고점2 + (고점2-저점2) x 값")
    bt.add_argument("--swing-single", action="store_true", help="swing: 1차 매수가 한 번 매수, 전량 매도가 한 번 매도")
    bt.add_argument("--swing-source", choices=["rth", "all"], default="rth")
    bt.add_argument("--swing-min-d", type=float, default=0.0, help="swing: 고점2-저점2 가 고점2 의 %% 이상")
    bt.add_argument("--rsi-tf", type=int, default=5, help="rsi: 분봉 단위")
    bt.add_argument("--rsi-period", type=int, default=14)
    bt.add_argument("--rsi-buy", type=float, default=30.0, help="rsi: 이 값 아래면 매수 (숏은 100-값 위)")
    bt.add_argument("--rsi-sell", type=float, default=50.0, help="rsi: 이 값 이상이면 매도 (숏은 100-값 이하)")
    bt.add_argument("--rsi-source", choices=["rth", "all"], default="rth")
    bt.add_argument("--rsi-side", choices=["long", "short", "both"], default="long")
    bt.add_argument("--boll-tf", type=int, default=5, help="boll: 분봉 단위")
    bt.add_argument("--boll-len", type=int, default=20)
    bt.add_argument("--boll-k", type=float, default=2.0, help="boll: 표준편차 배수")
    bt.add_argument("--boll-mode", choices=["reversion", "breakout"], default="reversion")
    bt.add_argument("--boll-exit", choices=["mid", "upper"], default="mid", help="boll reversion: 중심선 / 반대편 밴드에서 청산")
    bt.add_argument("--boll-side", choices=["long", "short", "both"], default="long")
    bt.add_argument("--boll-source", choices=["rth", "all"], default="rth")
    bt.add_argument("--boll-min-width", type=float, default=0.0, help="boll: 밴드 폭 %% 하한")
    bt.add_argument("--fvg-loc", choices=["zone", "mid"], default="zone", help="fvg: 갭 전체가 첫 봉 고가 위 / 가운데 봉 종가가 위")
    bt.add_argument("--fvg-entry", choices=["edge", "mid", "full"], default="edge", help="fvg: 갭 첫 닿음 / 가운데 / 다 메움")
    bt.add_argument("--fvg-pick", choices=["first", "latest"], default="latest")
    bt.add_argument("--fvg-target-r", type=float, default=0.0, help="fvg: 익절 = 위험 x R (0 = 없음)")
    bt.add_argument("--fvg-min-gap", type=float, default=0.0, help="fvg: 갭 크기 %% 하한")
    bt.add_argument("--fvg-side", choices=["long", "short", "both"], default="both")
    bt.add_argument("--fvg-minutes", type=int, default=350, help="fvg: 9:30 부터 N분 뒤 모두 정리")
    bt.add_argument("--fvg-stop-mode", choices=["touch", "close"], default="touch")
    bt.add_argument("--fvg-all-symbols", action="store_true", help="fvg: 하루 한 종목 제한 없이 모두")
    bt.add_argument("--vb-tf", type=int, default=15, help="vbreak: 분봉 단위")
    bt.add_argument("--vb-len", type=int, default=100, help="vbreak: VWMA 길이")
    bt.add_argument("--vb-fast", type=int, default=20, help="vbreak: 정배열 빠른선")
    bt.add_argument("--vb-mid", type=int, default=50, help="vbreak: 정배열 중간선")
    bt.add_argument("--vb-ma", choices=["sma", "vwma"], default="sma", help="vbreak: 빠른선·중간선 종류")
    bt.add_argument("--vb-wait", type=int, default=3, help="vbreak: 돌파 뒤 회복 못 한 봉 수")
    bt.add_argument("--vb-target-r", type=float, default=2.0, help="vbreak: 익절 = 손절 거리 x 값")
    bt.add_argument("--vb-source", choices=["rth", "all"], default="rth")
    bt.add_argument("--vb-side", choices=["short", "long", "both"], default="short")
    bt.add_argument("--vb-expire", type=int, default=0, help="vbreak: 주문 유효 봉 수 (0 = 회복할 때까지)")
    bt.add_argument("--flag-tf", type=int, default=15, help="flag: 분봉 단위")
    bt.add_argument("--flag-pole-bars", type=int, default=4, help="flag: 깃대 = 고점 포함 직전 N봉 최저가→고점")
    bt.add_argument("--flag-pole-pct", type=float, default=3.0, help="flag: 깃대 상승률 하한 %%")
    bt.add_argument("--flag-min", type=int, default=2, help="flag: 깃발 봉 수 하한")
    bt.add_argument("--flag-max", type=int, default=8, help="flag: 깃발 봉 수 상한")
    bt.add_argument("--flag-retrace", type=float, default=0.5, help="flag: 깃발 눌림 한도 (깃대 길이 배수)")
    bt.add_argument("--flag-entry", choices=["flag", "pole"], default="flag", help="flag: 깃발 고점 / 깃대 고점 돌파 매수")
    bt.add_argument("--flag-stop", choices=["flag", "mid", "pct"], default="flag", help="flag: 깃발 저점 / 가운데 / 진입가 -stop-pct%%")
    bt.add_argument("--flag-target", type=float, default=1.0, help="flag: 익절 = 진입가 + 깃대 길이 x 값 (0 = 없음)")
    bt.add_argument("--flag-target-r", type=float, default=0.0, help="flag: 익절 = 위험의 R배 (우선)")
    bt.add_argument("--flag-vol", action="store_true", help="flag: 깃발 거래량 < 깃대 거래량")
    bt.add_argument("--flag-source", choices=["rth", "all"], default="rth")
    bt.add_argument("--flag-entry-bar", choices=["close", "low"], default="close", help="flag: 돌파 1분봉 안 손절 = 종가 / 저가 기준(보수적)")
    bt.add_argument("--f5-pattern", choices=["A", "B"], default="A", help="first5: A 고가 돌파 / B 눌림 반등")
    bt.add_argument("--f5-entry", choices=["touch", "close"], default="touch")
    bt.add_argument("--f5-breakout-until", type=time.fromisoformat, default=time(9, 45))
    bt.add_argument("--f5-b-support", choices=["open", "mid"], default="open")
    bt.add_argument("--f5-b-until", type=time.fromisoformat, default=time(9, 55))
    bt.add_argument("--f5-min-body", type=float, default=0.5)
    bt.add_argument("--f5-vol-mult", type=float, default=0.0)
    bt.add_argument("--f5-gap-min", type=float, default=-100.0)
    bt.add_argument("--f5-gap-max", type=float, default=100.0)
    bt.add_argument("--f5-stop-pct", type=float, default=2.0)
    bt.add_argument("--f5-tp1", type=float, default=2.0)
    bt.add_argument("--f5-side", choices=["long", "short", "both"], default="long", help="first5: 숏 = 첫 봉 음봉·저가 하향 돌파")
    bt.add_argument("--f5-tp-short", type=float, default=0.0, help="first5: 숏 익절 %% (0 = f5-tp1)")
    bt.add_argument("--f5-stop-short", type=float, default=0.0, help="first5: 숏 손절 %% (0 = f5-stop-pct)")
    bt.add_argument("--f5-single", action="store_true", help="first5: f5-tp1 에서 전량 매도 (한 번 매도)")
    bt.add_argument("--f5-tp2", type=float, default=5.0)
    bt.add_argument("--size-mode", choices=["risk", "lev"], default="risk", help="risk = 손절 금액 기준 / lev = 고정 배율")
    bt.add_argument("--lev-etf", type=float, default=2.0, help="size-mode lev: 레버리지 ETF 배율")
    bt.add_argument("--lev-stock", type=float, default=5.0, help="size-mode lev: 일반 주식 배율")
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
    for name, hlp in [("check", "봇: 키·잔고·오늘 대상 종목 확인 (주문 없음)"), ("run", "봇: 오늘 하루 매매"),
                      ("loop", "봇: 켜 둔 채 미국 거래일마다 반복")]:
        sp = sub.add_parser(name, help=hlp)
        sp.add_argument("--env", default=".env.binance")
    a = ap.parse_args()
    if a.cmd in ("check", "run", "loop"):
        from .botmain import bot_main
        bot_main(a.cmd, a.env)
        return
    if a.cmd == "download":
        from .data import download_all
        download_all(Path(a.out), a.symbols, a.add, a.since)
    else:
        from .backtest import Params, run
        p = Params(orb_minutes=a.orb, direction=a.direction, stop_mode=a.stop_mode, target_r=a.target_r,
                   exit_time=a.exit_time, min_range_pct=a.min_range, max_range_pct=a.max_range,
                   min_open_qv=a.min_open_qv, max_positions=a.max_positions,
                   min_prev_qv=a.min_prev_qv, underlying=a.underlying,
                   strategy=a.strategy, vwap_dev=a.vwap_dev, vwap_start=a.vwap_start, buy_until=a.buy_until,
                   stop_pct=a.stop_pct, take_profit_pct=a.take_profit, surge_min_change=a.surge_min_change,
                   maker_fee=a.maker_fee, surge_entry=a.surge_entry,
                   size_mode=a.size_mode, lev_etf=a.lev_etf, lev_stock=a.lev_stock,
                   f5_pattern=a.f5_pattern, f5_entry=a.f5_entry, f5_breakout_until=a.f5_breakout_until,
                   f5_b_support=a.f5_b_support, f5_b_until=a.f5_b_until, f5_min_body=a.f5_min_body,
                   f5_vol_mult=a.f5_vol_mult, f5_gap_min=a.f5_gap_min, f5_gap_max=a.f5_gap_max,
                   f5_stop_pct=a.f5_stop_pct, f5_tp1=a.f5_tp1, f5_tp2=a.f5_tp2, f5_split=not a.f5_single, f5_side=a.f5_side, f5_tp_short=a.f5_tp_short, f5_stop_short=a.f5_stop_short,
                   swing_tf=a.swing_tf, swing_n=a.swing_n, swing_buy1=a.swing_buy1, swing_buy2=a.swing_buy2,
                   swing_tp2=a.swing_tp2, swing_split=not a.swing_single, swing_source=a.swing_source, swing_min_d=a.swing_min_d,
                   vwma_tf=a.vwma_tf, vwma_len=a.vwma_len, vwma_slope_bars=a.vwma_slope_bars,
                   vwma_min_above=a.vwma_min_above, vwma_band=a.vwma_band, vwma_source=a.vwma_source,
                   vwma_stop_basis=a.vwma_stop_basis, vwma_target_r=a.vwma_target_r,
                   vwma_rsi_period=a.vwma_rsi_period, vwma_rsi_long_min=a.vwma_rsi_long_min,
                   vwma_rsi_long_max=a.vwma_rsi_long_max, vwma_max_touch=a.vwma_max_touch,
                   flag_tf=a.flag_tf, flag_pole_bars=a.flag_pole_bars, flag_pole_pct=a.flag_pole_pct,
                   flag_min=a.flag_min, flag_max=a.flag_max, flag_retrace=a.flag_retrace, flag_entry=a.flag_entry,
                   flag_stop=a.flag_stop, flag_target=a.flag_target, flag_target_r=a.flag_target_r,
                   flag_vol=a.flag_vol, flag_source=a.flag_source, flag_entry_bar=a.flag_entry_bar,
                   rsi_tf=a.rsi_tf, rsi_period=a.rsi_period, rsi_buy=a.rsi_buy, rsi_sell=a.rsi_sell,
                   rsi_source=a.rsi_source, rsi_side=a.rsi_side,
                   boll_tf=a.boll_tf, boll_len=a.boll_len, boll_k=a.boll_k, boll_mode=a.boll_mode,
                   boll_exit=a.boll_exit, boll_side=a.boll_side, boll_source=a.boll_source, boll_min_width=a.boll_min_width,
                   fvg_loc=a.fvg_loc, fvg_entry=a.fvg_entry, fvg_pick=a.fvg_pick, fvg_target_r=a.fvg_target_r,
                   fvg_min_gap=a.fvg_min_gap, fvg_side=a.fvg_side, fvg_minutes=a.fvg_minutes,
                   fvg_stop_mode=a.fvg_stop_mode, fvg_one_per_day=not a.fvg_all_symbols,
                   vb_tf=a.vb_tf, vb_len=a.vb_len, vb_fast=a.vb_fast, vb_mid=a.vb_mid, vb_ma=a.vb_ma,
                   vb_wait=a.vb_wait, vb_target_r=a.vb_target_r, vb_source=a.vb_source, vb_side=a.vb_side,
                   vb_expire=a.vb_expire,
                   rule=SizeRule(a.risk, a.leverage, a.fee, a.slippage))
        run(Path(a.data), p, a.symbols, a.capital, a.start, a.end)


if __name__ == "__main__":
    main()
