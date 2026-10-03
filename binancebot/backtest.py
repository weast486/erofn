"""바이낸스 미국 주식 선물 당일 단타 백테스트 (1분봉, 미국 정규장 09:30~16:00 ET, 평일만).

전략 orb (시가 범위 돌파): 장 시작 N분 고가·저가를 정하고, 처음 고가를 넘으면 매수(롱) / 저가를 깨면 매도(숏).
손절 = 반대편 끝(range) 또는 범위 가운데(half), 익절 = 위험의 R배(0 이면 없음), exit_time 에 시장가 정리.
수량 = 평가금액 x 위험% / 손절 폭 (sizing.position_size), 포지션 합계 <= 평가금액 x 최대 레버리지.

가정: 돌파·손절은 그 가격에 슬리피지만큼 불리하게 체결(갭이면 시가), 익절 지정가는 그 가격,
같은 1분봉에서 손절·익절 모두 닿으면 손절, 진입 봉이 범위 위아래를 모두 건드리면 거래 안 함,
수수료는 진입·청산 모두 테이커, 들고 있는 동안 펀딩 시각(보통 16:00 UTC)이 지나면 펀딩비 반영.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .sizing import SizeRule, position_size

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
# 미국 증시 휴장일 (선물은 열려도 기초 주식이 안 움직이므로 거래 안 함)
US_HOLIDAYS = {date(2026, m, d) for m, d in [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25)]}
US_EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24)}  # 13:00 마감


@dataclass
class Bar:
    t: datetime  # ET, 봉 시작 시각
    o: float
    h: float
    l: float
    c: float
    qv: float  # 거래대금(USDT)


@dataclass
class Trade:
    symbol: str
    day: date
    side: int  # 1 롱, -1 숏
    entry_t: datetime
    entry: float
    stop: float
    exit_t: datetime
    exit: float
    reason: str
    funding: float = 0.0  # 1개당 펀딩 비용(가격 단위, +면 냄)
    qty: float = 0.0
    pnl: float = 0.0
    risk: float = 0.0


@dataclass
class Params:
    orb_minutes: int = 15
    direction: str = "both"   # both / long / short
    stop_mode: str = "range"  # range / half
    target_r: float = 2.0     # 0 = 익절 없음
    exit_time: time = time(15, 55)
    min_range_pct: float = 0.0
    max_range_pct: float = 100.0
    min_open_qv: float = 0.0  # 시가 범위 동안 거래대금 하한 (USDT)
    max_positions: int = 10   # 동시에 들고 있을 최대 종목 수
    rule: SizeRule = field(default_factory=SizeRule)


def session_close(d: date) -> time:
    return time(13, 0) if d in US_EARLY_CLOSE else time(16, 0)


def load_bars(path: Path) -> dict[date, list[Bar]]:
    """정규장 봉만 날짜별로."""
    days: dict[date, list[Bar]] = defaultdict(list)
    with gzip.open(path, "rt", newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.fromtimestamp(int(row["open_ms"]) / 1000, tz=UTC).astimezone(ET)
            d = t.date()
            if t.weekday() >= 5 or d in US_HOLIDAYS:
                continue
            if not (time(9, 30) <= t.time() < session_close(d)):
                continue
            days[d].append(Bar(t, float(row["open"]), float(row["high"]), float(row["low"]),
                               float(row["close"]), float(row["quote_volume"])))
    for bars in days.values():
        bars.sort(key=lambda b: b.t)
    return days


def load_funding(path: Path) -> list[tuple[datetime, float]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [(datetime.fromtimestamp(int(r["funding_ms"]) / 1000, tz=UTC), float(r["rate"])) for r in csv.DictReader(f)]


def orb_trade(symbol: str, d: date, bars: list[Bar], p: Params, slip: float) -> Trade | None:
    if not bars or bars[0].t.time() > time(9, 35):
        return None
    start = bars[0].t.replace(hour=9, minute=30)
    rend = start + timedelta(minutes=p.orb_minutes)
    rng = [b for b in bars if b.t < rend]
    rest = [b for b in bars if b.t >= rend]
    if len(rng) < p.orb_minutes * 0.8 or not rest:
        return None
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    if sum(b.qv for b in rng) < p.min_open_qv:
        return None
    width = (hi - lo) / lo * 100
    if not (p.min_range_pct <= width <= p.max_range_pct) or hi <= lo:
        return None
    close_t = datetime.combine(d, min(p.exit_time, (datetime.combine(d, session_close(d)) - timedelta(minutes=5)).time()), ET)
    s = slip / 100
    for i, b in enumerate(rest):
        if b.t >= close_t:
            return None
        up = b.h >= hi and p.direction in ("both", "long")
        dn = b.l <= lo and p.direction in ("both", "short")
        if up and dn:
            return None  # 한 봉에서 위아래 모두 → 순서 모름, 거래 안 함
        if not (up or dn):
            continue
        side = 1 if up else -1
        lvl = hi if up else lo
        entry = (max(b.o, lvl) if up else min(b.o, lvl)) * (1 + side * s)
        stop = (lo if up else hi) if p.stop_mode == "range" else (hi + lo) / 2
        risk = (entry - stop) * side
        if risk <= 0:
            return None
        target = entry + side * risk * p.target_r if p.target_r > 0 else None
        # 진입 봉: half 손절이면 같은 봉에서 손절가까지 왔는지(보수적으로 손절)
        if p.stop_mode == "half" and ((side == 1 and b.l <= stop) or (side == -1 and b.h >= stop)):
            return Trade(symbol, d, side, b.t, entry, stop, b.t, stop * (1 - side * s), "stop")
        for b2 in rest[i + 1:]:
            if b2.t >= close_t:
                return Trade(symbol, d, side, b.t, entry, stop, b2.t, b2.o * (1 - side * s), "time")
            hit_stop = b2.l <= stop if side == 1 else b2.h >= stop
            if hit_stop:
                px = min(b2.o, stop) if side == 1 else max(b2.o, stop)
                return Trade(symbol, d, side, b.t, entry, stop, b2.t, px * (1 - side * s), "stop")
            if target is not None and (b2.h >= target if side == 1 else b2.l <= target):
                px = max(b2.o, target) if side == 1 else min(b2.o, target)
                return Trade(symbol, d, side, b.t, entry, stop, b2.t, px, "target")
        last = rest[-1]
        return Trade(symbol, d, side, b.t, entry, stop, last.t, last.c * (1 - side * s), "time")
    return None


def apply_funding(t: Trade, funding: list[tuple[datetime, float]]) -> None:
    a, z = t.entry_t.astimezone(UTC), t.exit_t.astimezone(UTC)
    t.funding = sum(rate * t.entry * t.side for ft, rate in funding if a < ft <= z)


def lot_filters(meta: dict) -> tuple[float, float]:
    step = min_notional = 0.0
    for f in meta.get("filters", []):
        if f.get("filterType") == "MARKET_LOT_SIZE":
            step = max(step, float(f.get("stepSize", 0)))
        elif f.get("filterType") == "LOT_SIZE" and not step:
            step = float(f.get("stepSize", 0))
        elif f.get("filterType") == "MIN_NOTIONAL":
            min_notional = float(f.get("notional", 0))
    return step, min_notional


def simulate(trades: list[Trade], p: Params, capital: float, filters: dict[str, tuple[float, float]]) -> tuple[list[Trade], dict[date, float]]:
    """시간 순서대로 계좌에 반영. 반환: 실제 체결된 거래, 날짜별 마감 평가금액."""
    rule = p.rule
    equity = capital
    done: list[Trade] = []
    open_: list[Trade] = []
    eod: dict[date, float] = {}

    def close_until(ts: datetime | None):
        nonlocal equity
        for t in sorted([t for t in open_ if ts is None or t.exit_t <= ts], key=lambda t: t.exit_t):
            fee = (t.entry + t.exit) * t.qty * rule.fee_pct / 100
            t.pnl = (t.exit - t.entry) * t.side * t.qty - fee - t.funding * t.qty
            equity += t.pnl
            open_.remove(t)
            done.append(t)

    for t in sorted(trades, key=lambda t: (t.entry_t, t.symbol)):
        close_until(t.entry_t)
        if len(open_) >= p.max_positions:
            continue
        step, mn = filters.get(t.symbol, (0.0, 0.0))
        qty = position_size(equity, t.entry, t.stop, rule, sum(x.entry * x.qty for x in open_), step, mn)
        if qty <= 0:
            continue
        t.qty = qty
        t.risk = equity * rule.risk_pct / 100
        open_.append(t)
    close_until(None)
    eq = capital
    for d in sorted({t.day for t in done}):
        eq += sum(t.pnl for t in done if t.day == d)
        eod[d] = eq
    return done, eod


def max_drawdown(values: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def run(data_dir: Path, p: Params, symbols: list[str] | None = None, capital: float = 1000.0,
        start: date | None = None, end: date | None = None, verbose: bool = True) -> dict:
    meta = {}
    sj = data_dir / "symbols.json"
    if sj.exists():
        meta = {s["symbol"]: s for s in json.loads(sj.read_text(encoding="utf-8"))["symbols"]}
    files = sorted(data_dir.glob("*_1m.csv.gz"))
    if symbols:
        want = {s.upper() for s in symbols}
        files = [f for f in files if f.name.split("_")[0] in want]
    trades: list[Trade] = []
    filters = {}
    for f in files:
        sym = f.name.split("_")[0]
        filters[sym] = lot_filters(meta.get(sym, {}))
        funding = load_funding(data_dir / f"{sym}_funding.csv")
        for d, bars in load_bars(f).items():
            if (start and d < start) or (end and d > end):
                continue
            t = orb_trade(sym, d, bars, p, p.rule.slippage_pct)
            if t:
                apply_funding(t, funding)
                trades.append(t)
    done, eod = simulate(trades, p, capital, filters)
    return summarize(done, eod, capital, verbose)


def summarize(done: list[Trade], eod: dict[date, float], capital: float, verbose: bool) -> dict:
    days = sorted(eod)
    curve = [capital] + [eod[d] for d in days]
    n = len(done)
    wins = [t for t in done if t.pnl > 0]
    rs = [t.pnl / t.risk for t in done if t.risk]
    res = {
        "trades": n,
        "return_pct": (curve[-1] / capital - 1) * 100,
        "mdd_pct": max_drawdown(curve),
        "win_pct": len(wins) / n * 100 if n else 0.0,
        "avg_r": sum(rs) / len(rs) if rs else 0.0,
        "reasons": {k: sum(1 for t in done if t.reason == k) for k in ("stop", "target", "time")},
    }
    months: dict[str, list[float]] = defaultdict(list)
    prev = capital
    for d in days:
        months[d.strftime("%Y-%m")].append((prev, eod[d]))
        prev = eod[d]
    res["monthly"] = {m: (v[-1][1] / v[0][0] - 1) * 100 for m, v in months.items()}
    by_sym = defaultdict(list)
    for t in done:
        by_sym[t.symbol].append(t.pnl / t.risk if t.risk else 0)
    res["by_symbol"] = {s: (len(v), sum(v) / len(v)) for s, v in sorted(by_sym.items())}
    if verbose:
        print(f"거래 {n}건, 수익률 {res['return_pct']:+.1f}%, MDD {res['mdd_pct']:.1f}%, 승률 {res['win_pct']:.0f}%, "
              f"거래당 평균 {res['avg_r']:+.2f}R, 청산 {res['reasons']}")
        print("월별: " + " / ".join(f"{m} {v:+.1f}%" for m, v in res["monthly"].items()))
        print("종목별(건수, 평균R): " + ", ".join(f"{s} {c} {r:+.2f}" for s, (c, r) in res["by_symbol"].items()))
    return res
