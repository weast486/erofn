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
    fee_entry: float | None = None  # 진입 수수료 % (None = 테이커 rule.fee_pct, 지정가면 메이커)
    fee_exit: float | None = None
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
    min_prev_qv: float = 0.0  # 전날 정규장 거래대금 하한 (USDT, 미래 정보 없음)
    underlying: str = "EQUITY"  # symbols.json underlyingType (EQUITY = 미국 주식·ETF, ALL = 전부)
    strategy: str = "orb"     # orb / vwap / surge
    maker_fee: float = 0.02   # 지정가(메이커) 수수료 %
    # vwap: 당일 VWAP 에서 dev% 벗어나면 반대로 지정가 진입, VWAP 닿으면 청산
    vwap_dev: float = 2.0
    vwap_start: time = time(10, 0)
    buy_until: time = time(15, 0)
    stop_pct: float = 2.0     # 진입가 대비 손절 % (vwap·surge)
    take_profit_pct: float = 0.0  # 진입가 대비 익절 % (0 = vwap 은 VWAP 복귀, surge 는 없음)
    # surge: 전날 +N%↑ 종목이 전날 종가 아래에서 시작해 전날 종가를 넘으면 매수
    surge_min_change: float = 10.0
    # vwma: N분봉 거래량가중이동평균(VWMA) 위에 min_above 봉 이상 + VWMA 상승 → VWMA 부근 지정가 롱 (반대면 숏)
    vwma_tf: int = 15          # 분봉 단위
    vwma_len: int = 100
    vwma_slope_bars: int = 1   # VWMA 가 이 봉 수 전보다 높으면 상승
    vwma_min_above: int = 20   # 직전 연속 봉 수 (종가가 VWMA 위)
    vwma_band: float = 0.0     # 지정가 = VWMA x (1 + band%) (롱), 숏은 x (1 - band%)
    vwma_source: str = "all"   # all = 받아 둔 봉 전부(UTC 12~22시) / rth = 정규장 봉만으로 VWMA 계산
    vwma_stop_basis: str = "entry"  # entry = 진입가 대비 stop_pct / vwma = VWMA 대비 stop_pct
    vwma_target_r: float = 0.0  # 익절 = 위험의 R배 (0 이면 take_profit_pct, 둘 다 0 이면 정리 시각까지)
    surge_entry: str = "stop"  # stop = 전날 종가에 역지정가 / next = 넘은 1분봉 다음 봉 시가 (보수적)
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


def run_exit(symbol: str, d: date, side: int, entry_bar: Bar, entry: float, stop: float, rest: list[Bar],
             close_t: datetime, slip: float, target: float | None = None, target_fn=None,
             fee_entry: float | None = None, maker_fee: float = 0.02) -> Trade:
    """진입 뒤 손절(시장가)·익절(지정가, 메이커)·정리 시각. target_fn(i) 는 i번째 봉 직전까지 아는 목표가."""
    s = slip / 100
    for i, b in enumerate(rest):
        if b.t >= close_t:
            return Trade(symbol, d, side, entry_bar.t, entry, stop, b.t, b.o * (1 - side * s), "time", fee_entry=fee_entry)
        if (b.l <= stop) if side == 1 else (b.h >= stop):
            px = min(b.o, stop) if side == 1 else max(b.o, stop)
            return Trade(symbol, d, side, entry_bar.t, entry, stop, b.t, px * (1 - side * s), "stop", fee_entry=fee_entry)
        tg = target_fn(i) if target_fn else target
        if tg is not None and ((b.h >= tg) if side == 1 else (b.l <= tg)):
            px = max(b.o, tg) if side == 1 else min(b.o, tg)
            return Trade(symbol, d, side, entry_bar.t, entry, stop, b.t, px, "target", fee_entry=fee_entry, fee_exit=maker_fee)
    last = rest[-1] if rest else entry_bar
    return Trade(symbol, d, side, entry_bar.t, entry, stop, last.t, last.c * (1 - side * s), "time", fee_entry=fee_entry)


def _close_t(d: date, p: Params) -> datetime:
    return datetime.combine(d, min(p.exit_time, (datetime.combine(d, session_close(d)) - timedelta(minutes=5)).time()), ET)


def vwap_trade(symbol: str, d: date, bars: list[Bar], p: Params, slip: float) -> Trade | None:
    """VWAP 되돌림: 직전 봉까지의 VWAP 에서 vwap_dev% 아래(위)에 지정가 매수(매도) → VWAP 복귀 지정가 청산."""
    if not bars or bars[0].t.time() > time(9, 35):
        return None
    close_t = _close_t(d, p)
    pv = vol = 0.0
    vwaps = []  # i번째 봉 직전까지의 VWAP
    for b in bars:
        vwaps.append(pv / vol if vol else None)
        typ = (b.h + b.l + b.c) / 3
        q = b.qv / typ if typ else 0.0  # 수량
        pv += typ * q
        vol += q
    k = p.vwap_dev / 100
    for i, b in enumerate(bars):
        v = vwaps[i]
        if v is None or b.t.time() < p.vwap_start:
            continue
        if b.t.time() >= p.buy_until or b.t >= close_t:
            return None
        lo_lvl, hi_lvl = v * (1 - k), v * (1 + k)
        long_ok = p.direction in ("both", "long") and b.l <= lo_lvl
        short_ok = p.direction in ("both", "short") and b.h >= hi_lvl
        if long_ok and short_ok:
            return None
        if not (long_ok or short_ok):
            continue
        side = 1 if long_ok else -1
        lvl = lo_lvl if long_ok else hi_lvl
        entry = min(b.o, lvl) if side == 1 else max(b.o, lvl)
        stop = entry * (1 - side * p.stop_pct / 100)
        # 진입 봉 안에서 손절가까지 갔으면 손절 (순서 모름 → 보수적)
        if (b.l <= stop) if side == 1 else (b.h >= stop):
            return Trade(symbol, d, side, b.t, entry, stop, b.t, stop * (1 - side * slip / 100), "stop", fee_entry=p.maker_fee)
        rest = bars[i + 1:]
        if p.take_profit_pct > 0:
            return run_exit(symbol, d, side, b, entry, stop, rest, close_t, slip,
                            target=entry * (1 + side * p.take_profit_pct / 100), fee_entry=p.maker_fee, maker_fee=p.maker_fee)
        return run_exit(symbol, d, side, b, entry, stop, rest, close_t, slip,
                        target_fn=lambda j: vwaps[i + 1 + j], fee_entry=p.maker_fee, maker_fee=p.maker_fee)
    return None


def surge_trade(symbol: str, d: date, bars: list[Bar], p: Params, slip: float, prev_close: float | None,
                prev_change: float | None) -> Trade | None:
    """전날 +N%↑ 종목이 전날 종가 아래에서 시작해 buy_until 까지 전날 종가를 넘으면 매수 (역지정가, 테이커)."""
    if prev_close is None or prev_change is None or prev_change < p.surge_min_change:
        return None
    if not bars or bars[0].t.time() > time(9, 35) or bars[0].o >= prev_close:
        return None
    close_t = _close_t(d, p)
    s = slip / 100
    for i, b in enumerate(bars):
        if b.t.time() >= p.buy_until or b.t >= close_t:
            return None
        if b.h < prev_close:
            continue
        if p.surge_entry == "next":
            if i + 1 >= len(bars):
                return None
            b, i = bars[i + 1], i + 1
            entry = b.o * (1 + s)
        else:
            entry = max(b.o, prev_close) * (1 + s)
        stop = entry * (1 - p.stop_pct / 100)
        target = entry * (1 + p.take_profit_pct / 100) if p.take_profit_pct > 0 else None
        if p.surge_entry == "next" and (b.l <= stop or (target is not None and b.h >= target)):
            # 진입 봉 안에서 손절·익절 순서를 모름 → 손절가에 닿았으면 손절, 아니면 다음 봉부터
            if b.l <= stop:
                return Trade(symbol, d, 1, b.t, entry, stop, b.t, stop * (1 - s), "stop")
        return run_exit(symbol, d, 1, b, entry, stop, bars[i + 1:], close_t, slip, target=target, maker_fee=p.maker_fee)
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
            fe = rule.fee_pct if t.fee_entry is None else t.fee_entry
            fx = rule.fee_pct if t.fee_exit is None else t.fee_exit
            fee = (t.entry * fe + t.exit * fx) * t.qty / 100
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



@dataclass
class MinBar:
    t: datetime  # ET
    o: float
    h: float
    l: float
    c: float
    v: float


def load_minutes(path: Path) -> list[MinBar]:
    out = []
    with gzip.open(path, "rt", newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.fromtimestamp(int(row["open_ms"]) / 1000, tz=UTC).astimezone(ET)
            out.append(MinBar(t, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"])))
    out.sort(key=lambda b: b.t)
    return out


def is_rth(t: datetime) -> bool:
    d = t.date()
    return t.weekday() < 5 and d not in US_HOLIDAYS and time(9, 30) <= t.time() < session_close(d)


def vwma_trades(symbol: str, mins: list[MinBar], p: Params) -> list[Trade]:
    """VWMA 눌림: 직전 완성된 N분봉 기준 조건이 맞으면 다음 N분 동안 1분봉으로 지정가 체결·손절·익절 확인.
    진입은 정규장 vwma_start~buy_until 만, 정리는 exit_time (당일 단타), 한 번에 한 포지션."""
    slip = p.rule.slippage_pct / 100
    tf = p.vwma_tf
    # N분봉 만들기 (ET 기준 tf 분 단위)
    groups: dict[datetime, list[MinBar]] = {}
    for b in mins:
        if p.vwma_source == "rth" and not is_rth(b.t):
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        groups.setdefault(k, []).append(b)
    keys = sorted(groups)
    closes, vwma = [], []
    pv = vv = 0.0
    window: list[tuple[float, float]] = []
    for k in keys:
        g = groups[k]
        c, v = g[-1].c, sum(b.v for b in g)
        closes.append(c)
        window.append((c * v, v))
        pv += c * v
        vv += v
        if len(window) > p.vwma_len:
            a, b2 = window.pop(0)
            pv -= a
            vv -= b2
        vwma.append(pv / vv if len(window) == p.vwma_len and vv > 0 else None)
    # i번째 봉이 끝난 뒤의 신호
    def signal(i: int) -> int:
        n, s = p.vwma_min_above, p.vwma_slope_bars
        if i < max(n, s) or vwma[i] is None or vwma[i - s] is None:
            return 0
        recent = range(i - n + 1, i + 1)
        if any(vwma[j] is None for j in recent):
            return 0
        if vwma[i] > vwma[i - s] and all(closes[j] > vwma[j] for j in recent):
            return 1
        if vwma[i] < vwma[i - s] and all(closes[j] < vwma[j] for j in recent):
            return -1
        return 0
    sig_at = {}  # 1분봉 시각 → (방향, VWMA) : 그 1분봉이 속한 N분봉 직전 봉의 신호
    for i in range(len(keys) - 1):
        sg = signal(i)
        if sg and (p.direction == "both" or (sg == 1) == (p.direction == "long")):
            sig_at[keys[i + 1]] = (sg, vwma[i])
    trades: list[Trade] = []
    pos = None  # (side, entry_bar, entry, stop, target, fee_entry)
    for b in mins:
        if not is_rth(b.t):
            continue
        d = b.t.date()
        close_t = _close_t(d, p)
        if pos:
            side, eb, entry, stop, target = pos
            reason = px = None
            if b.t >= close_t or eb.t.date() != d:
                reason, px = "time", b.o * (1 - side * slip)
            elif (b.l <= stop) if side == 1 else (b.h >= stop):
                reason = "stop"
                px = (min(b.o, stop) if side == 1 else max(b.o, stop)) * (1 - side * slip)
            elif target is not None and ((b.h >= target) if side == 1 else (b.l <= target)):
                reason, px = "target", (max(b.o, target) if side == 1 else min(b.o, target))
            if reason:
                trades.append(Trade(symbol, eb.t.date(), side, eb.t, entry, stop, b.t, px, reason, fee_entry=p.maker_fee,
                                    fee_exit=p.maker_fee if reason == "target" else None))
                pos = None
            continue
        if b.t.time() < p.vwap_start or b.t.time() >= p.buy_until or b.t >= close_t:
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        if k not in sig_at:
            continue
        side, v = sig_at[k]
        lvl = v * (1 + side * p.vwma_band / 100)
        if not ((b.l <= lvl) if side == 1 else (b.h >= lvl)):
            continue
        entry = min(b.o, lvl) if side == 1 else max(b.o, lvl)
        base = entry if p.vwma_stop_basis == "entry" else v
        stop = base * (1 - side * p.stop_pct / 100)
        if (stop >= entry) if side == 1 else (stop <= entry):
            continue
        risk = abs(entry - stop)
        target = (entry + side * risk * p.vwma_target_r if p.vwma_target_r > 0
                  else entry * (1 + side * p.take_profit_pct / 100) if p.take_profit_pct > 0 else None)
        if (b.l <= stop) if side == 1 else (b.h >= stop):  # 진입 1분봉 안에서 손절가 → 보수적으로 손절
            trades.append(Trade(symbol, d, side, b.t, entry, stop, b.t, stop * (1 - side * slip), "stop", fee_entry=p.maker_fee))
            continue
        pos = (side, b, entry, stop, target)
    return trades


def make_trade(sym: str, d: date, bars: list[Bar], p: Params, prev_close: float | None, prev_change: float | None) -> Trade | None:
    slip = p.rule.slippage_pct
    if p.strategy == "vwap":
        return vwap_trade(sym, d, bars, p, slip)
    if p.strategy == "surge":
        return surge_trade(sym, d, bars, p, slip, prev_close, prev_change)
    return orb_trade(sym, d, bars, p, slip)


def run(data_dir: Path, p: Params, symbols: list[str] | None = None, capital: float = 1000.0,
        start: date | None = None, end: date | None = None, verbose: bool = True) -> dict:
    meta = {}
    sj = data_dir / "symbols.json"
    if sj.exists():
        meta = {s["symbol"]: s for s in json.loads(sj.read_text(encoding="utf-8"))["symbols"]}
    files = sorted(data_dir.glob("*_1m.csv.gz"))
    if meta and p.underlying != "ALL":
        files = [f for f in files if meta.get(f.name.split("_")[0], {}).get("underlyingType") == p.underlying]
    if symbols:
        want = {s.upper() for s in symbols}
        files = [f for f in files if f.name.split("_")[0] in want]
    trades: list[Trade] = []
    filters = {}
    for f in files:
        sym = f.name.split("_")[0]
        filters[sym] = lot_filters(meta.get(sym, {}))
        funding = load_funding(data_dir / f"{sym}_funding.csv")
        if p.strategy == "vwma":
            for t in vwma_trades(sym, load_minutes(f), p):
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        days = load_bars(f)
        prev_qv = prev_close = prev_change = None
        for d in sorted(days):
            bars = days[d]
            pq, pc, pch = prev_qv, prev_close, prev_change
            prev_qv = sum(b.qv for b in bars)
            prev_change = (bars[-1].c / prev_close - 1) * 100 if prev_close else None
            prev_close = bars[-1].c
            if (start and d < start) or (end and d > end):
                continue
            if p.min_prev_qv > 0 and (pq is None or pq < p.min_prev_qv):
                continue
            t = make_trade(sym, d, bars, p, pc, pch)
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
