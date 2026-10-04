"""토스증권 미국 주식 당일 단타 백테스트 (1분봉, 미국 정규장, 매수만·레버리지 없음·1주 단위).

전략은 binancebot 의 시가 범위 돌파(orb_trade)를 매수 방향만 사용: 장 시작 N분 고가를 처음 넘으면 매수,
손절 = 범위 저가(range) 또는 가운데(half), 익절 = 위험의 R배(0 이면 없음), exit_time 에 시장가 정리.
수량 = 평가금액 x position_pct% / 매수가 (정수 주, 남은 현금 안에서), 동시에 최대 max_positions 종목.

가정: 수수료 한쪽 fee%(기본 0.1), 돌파·손절·정리는 slippage% 만큼 불리하게, 익절 지정가는 그 가격,
같은 1분봉에서 손절·익절 모두 닿으면 손절. 달러 기준(환전 비용·환율 변동·양도세 미반영).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

from binancebot.backtest import Params, Trade, load_bars, max_drawdown, orb_trade


def collect(data_dir: Path, p: Params, symbols: list[str] | None, start: date | None, end: date | None,
            slippage: float) -> list[Trade]:
    files = sorted(data_dir.glob("*_1m.csv.gz"))
    if symbols:
        want = {s.upper() for s in symbols}
        files = [f for f in files if f.name.split("_")[0] in want]
    trades = []
    for f in files:
        sym = f.name.split("_")[0]
        for d, bars in load_bars(f).items():
            if (start and d < start) or (end and d > end):
                continue
            t = orb_trade(sym, d, bars, p, slippage)
            if t:
                trades.append(t)
    return trades


def net_pct(t: Trade, fee: float) -> float:
    """거래 1건 순수익률 % (수수료 양쪽 포함)."""
    return (t.exit / t.entry - 1) * 100 - fee * (1 + t.exit / t.entry)


def simulate(trades: list[Trade], capital: float, position_pct: float, max_positions: int,
             fee: float) -> tuple[list[Trade], dict[date, float]]:
    """시간 순서대로 현금 계좌에 반영. 반환: 체결된 거래, 날짜별 마감 평가금액."""
    cash = capital
    open_: list[Trade] = []
    done: list[Trade] = []

    def close_until(ts) -> None:
        nonlocal cash
        for t in sorted([t for t in open_ if ts is None or t.exit_t <= ts], key=lambda t: t.exit_t):
            cash += t.exit * t.qty * (1 - fee / 100)
            t.pnl = (t.exit - t.entry) * t.qty - (t.entry + t.exit) * t.qty * fee / 100
            open_.remove(t)
            done.append(t)

    for t in sorted(trades, key=lambda t: (t.entry_t, t.symbol)):
        close_until(t.entry_t)
        if len(open_) >= max_positions:
            continue
        equity = cash + sum(x.entry * x.qty for x in open_)
        budget = min(equity * position_pct / 100, cash / (1 + fee / 100))
        qty = int(budget // t.entry)
        if qty <= 0:
            continue
        t.qty = qty
        cash -= t.entry * qty * (1 + fee / 100)
        open_.append(t)
    close_until(None)
    eod: dict[date, float] = {}
    eq = capital
    for d in sorted({t.day for t in done}):
        eq += sum(t.pnl for t in done if t.day == d)
        eod[d] = eq
    return done, eod


def run(data_dir: Path, p: Params, symbols: list[str] | None = None, capital: float = 10000.0,
        start: date | None = None, end: date | None = None, position_pct: float = 20.0,
        fee: float = 0.1, slippage: float = 0.05, verbose: bool = True) -> dict:
    trades = collect(data_dir, p, symbols, start, end, slippage)
    rets = [net_pct(t, fee) for t in trades]
    done, eod = simulate(trades, capital, position_pct, p.max_positions, fee)
    days = sorted(eod)
    curve = [capital] + [eod[d] for d in days]
    n = len(trades)
    quarters: dict[str, list[float]] = defaultdict(list)
    by_sym: dict[str, list[float]] = defaultdict(list)
    for t, r in zip(trades, rets):
        quarters[f"{t.day.year}Q{(t.day.month - 1) // 3 + 1}"].append(r)
        by_sym[t.symbol].append(r)
    res = {
        "signals": n,
        "avg_pct": sum(rets) / n if n else 0.0,
        "win_pct": sum(r > 0 for r in rets) / n * 100 if n else 0.0,
        "reasons": {k: sum(1 for t in trades if t.reason == k) for k in ("stop", "target", "time")},
        "quarters": {q: (len(v), sum(v) / len(v)) for q, v in sorted(quarters.items())},
        "by_symbol": {s: (len(v), sum(v) / len(v)) for s, v in sorted(by_sym.items())},
        "trades": len(done),
        "return_pct": (curve[-1] / capital - 1) * 100,
        "mdd_pct": max_drawdown(curve),
    }
    if verbose:
        print(f"신호 {n}건, 거래당 평균 {res['avg_pct']:+.3f}%, 승률 {res['win_pct']:.0f}%, 청산 {res['reasons']}")
        print("분기별(건수, 거래당%): " + " / ".join(f"{q} {c} {r:+.2f}" for q, (c, r) in res["quarters"].items()))
        print("종목별(건수, 거래당%): " + ", ".join(f"{s} {c} {r:+.2f}" for s, (c, r) in res["by_symbol"].items()))
        print(f"계좌: 체결 {res['trades']}건, 수익률 {res['return_pct']:+.1f}%, MDD {res['mdd_pct']:.1f}%")
    return res
