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
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .sizing import SizeRule, position_size

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
# 미국 증시 휴장일 (선물은 열려도 기초 주식이 안 움직이므로 거래 안 함)
US_HOLIDAYS = ({date(2026, m, d) for m, d in [(1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3), (9, 7), (11, 26), (12, 25)]}
               | {date(2027, m, d) for m, d in [(1, 1), (1, 18), (2, 15), (3, 26), (5, 31), (6, 18), (7, 5), (9, 6), (11, 25), (12, 24)]})
# 2·3배(인버스 포함) 레버리지 ETF (고정 배율 진입 때 구분). SKDD·STXX 는 확인 못 해 보수적으로 포함
LEVERAGED_ETFS = {"SOXL", "SOXS", "TQQQ", "SQQQ", "NVDL", "TSLL", "MUU", "MVLL", "SNXX", "SKUU", "SKDD", "STXX",
                  "KORU", "TMF", "TZA", "TBT", "UVXY"}
US_EARLY_CLOSE = {date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
                  date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)}  # 13:00 마감


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
    size_risk: float | None = None  # 수량 계산용 1단위당 위험(가격). None = |entry-stop|
    size_frac: float = 1.0          # 그 수량의 몇 배 (분할 매수·매도 조각)
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
    vwma_rsi_period: int = 14
    vwma_max_touch: int = 0    # 종가가 VWMA 를 넘어 한쪽에 자리 잡은 뒤 N번째 닿음까지만 진입 (0 = 제한 없음)
    vwma_rsi_long_min: float = 0.0    # 롱: 직전 봉 RSI 가 이 값 이상 (숏은 100-값 이하)
    vwma_rsi_long_max: float = 100.0  # 롱: 직전 봉 RSI 가 이 값 이하 (숏은 100-값 이상)
    # swing: 저점1<저점2·고점1<고점2 (저1→고1→저2→고2) 뒤 되돌림 분할 매수
    swing_tf: int = 15
    swing_n: int = 3              # 고점·저점 = 좌우 N봉 중 최고·최저
    swing_buy1: float = 0.5       # 1차 매수 = 저점2 + D x 값 (D = 고점2 - 저점2)
    swing_buy2: float = 0.25      # 2차 매수 (3/4 지점 = 고점2 에서 3/4 내려온 곳)
    swing_tp2: float = 0.5        # 전량 매도 = 고점2 + D x 값 (1차 매도는 고점2 에서 절반)
    swing_split: bool = True      # False = 1차 매수가에 한 번 매수(2차 없음), 고점2 절반 매도 없이 전량 매도가에 한 번 매도
    swing_source: str = "rth"     # 패턴을 찾는 봉: rth = 정규장 / all = 받아 둔 봉 전부
    swing_min_d: float = 0.0      # D 가 고점2 의 몇 % 이상일 때만
    # flag: 상승 깃발형 — 깃대(pole_bars 봉 안에 +pole_pct%↑) 뒤 flag_min~flag_max 봉 동안 깃대 고점 아래에서 눌림
    #       (깃대 상승폭의 max_retrace 이하), 깃발 고점(또는 깃대 고점)을 넘으면 역지정가 매수
    flag_tf: int = 15
    flag_pole_bars: int = 4       # 깃대: 고점 포함 직전 N봉 안의 최저가 → 고점
    flag_pole_pct: float = 3.0    # 깃대 상승률 하한 %
    flag_min: int = 2             # 깃발 봉 수 하한
    flag_max: int = 8             # 깃발 봉 수 상한
    flag_retrace: float = 0.5     # 깃발 저점이 깃대 상승폭의 몇 배 이상 되돌리면 무효
    flag_entry: str = "flag"      # flag = 깃발 고점 돌파 / pole = 깃대 고점 돌파
    flag_stop: str = "flag"       # flag = 깃발 저점 / mid = 깃발 저점과 진입가 가운데 / pct = 진입가 -stop_pct%
    flag_target: float = 1.0      # 익절 = 진입가 + 깃대 길이 x 값 (0 = 없음, target_r 우선)
    flag_target_r: float = 0.0    # 익절 = 위험의 R배 (0 이면 flag_target)
    flag_vol: bool = False        # 깃발 평균 거래량 < 깃대 평균 거래량일 때만
    flag_source: str = "rth"      # 패턴을 찾는 봉: rth / all
    flag_entry_bar: str = "close"  # 돌파한 1분봉 안 손절 판단: close = 종가가 손절가 이하면 손절 / low = 저가만 닿아도 손절 (보수적)
    # first5: 첫 5분봉(9:30~9:35) 기준 돌파(A)·눌림(B) 매수, 분할 익절
    f5_pattern: str = "A"         # A = 첫 5분봉 고가 돌파 / B = 시가·중간값 지지 뒤 반등 양봉
    f5_entry: str = "touch"       # A: touch = 고가에 역지정가 / close = 5분봉 종가가 고가 위면 그 종가
    f5_breakout_until: time = time(9, 45)  # A: 2·3번째 5분봉까지
    f5_b_support: str = "open"    # B: 종가로 깨면 안 되는 선 open(첫 봉 시가) / mid(중간값)
    f5_b_until: time = time(9, 55)
    f5_min_body: float = 0.5      # 첫 봉 몸통 / (고가-저가) 하한, 양봉만
    f5_vol_mult: float = 0.0      # 첫 봉 거래량 >= 최근 10일 첫 봉 평균 x 값 (0 = 조건 없음)
    f5_gap_min: float = -100.0    # 시가 갭(전날 정규장 종가 대비 %) 범위
    f5_gap_max: float = 100.0
    f5_stop_pct: float = 2.0      # 매수가 대비 손절 % (닿으면)
    f5_tp1: float = 2.0           # 절반 익절 %
    f5_tp2: float = 5.0           # 나머지 익절 %
    f5_tp_short: float = 0.0      # 숏 익절 % (0 = f5_tp1 과 같음)
    f5_stop_short: float = 0.0    # 숏 손절 % (0 = f5_stop_pct 와 같음)
    f5_side: str = "long"         # long / short / both (숏 = 첫 봉 음봉, 저가 하향 돌파)
    f5_split: bool = True         # False = tp1 에서 전량 매도 (한 번 매도)
    # vbreak: VWMA 상승 추세(빠른선 > 중간선 > VWMA, VWMA 상승) 중 종가가 VWMA 를 하향 돌파하고 N봉 동안 회복 못 하면
    #         VWMA 에 지정가 숏 (손절 = 돌파 봉 고가, 익절 = 손절 거리 x R). 롱은 반대로
    vb_tf: int = 15
    vb_len: int = 100           # VWMA 길이
    vb_fast: int = 20           # 정배열 확인용 빠른선
    vb_mid: int = 50            # 중간선
    vb_ma: str = "sma"          # 빠른선·중간선 종류: sma / vwma
    vb_wait: int = 3            # 돌파 봉 뒤 이 봉 수 동안 종가가 VWMA 아래면 지정가 주문
    vb_target_r: float = 2.0    # 익절 = 손절 거리 x 값 (0 = 정리 시각까지)
    vb_source: str = "rth"      # 이평선 계산 봉: rth = 정규장 / all = 받아 둔 봉 전부
    vb_side: str = "short"      # short / long (반대: 하락 역배열 중 상향 돌파 → VWMA 지정가 롱) / both
    vb_expire: int = 0          # 주문을 낸 뒤 이 봉 수 안에 체결 안 되면 취소 (0 = 회복·당일 끝까지)
    # rsi: 장중 RSI 평균회귀 — 완성된 tf 분봉 RSI 가 buy 아래면 다음 봉 시가 매수, sell 이상이면 다음 봉 시가 매도
    #      (숏은 100-buy 위에서 매도 → 100-sell 이하에서 환매), stop_pct 손절·take_profit_pct 익절(0 = 없음), exit_time 정리
    rsi_tf: int = 5
    rsi_period: int = 14
    rsi_buy: float = 30.0
    rsi_sell: float = 50.0
    rsi_source: str = "rth"     # RSI 계산 봉: rth = 정규장 / all = 받아 둔 봉 전부
    rsi_side: str = "long"      # long / short / both
    # boll: 장중 볼린저밴드 (boll_trades 참고), 손절·익절은 stop_pct·take_profit_pct
    boll_tf: int = 5
    boll_len: int = 20
    boll_k: float = 2.0
    boll_mode: str = "reversion"   # reversion = 밴드 밖에서 되돌림 / breakout = 밴드 돌파 추종
    boll_exit: str = "mid"         # reversion 청산: mid = 중심선 / upper = 반대편 밴드
    boll_side: str = "long"        # long / short / both
    boll_source: str = "rth"
    boll_min_width: float = 0.0    # 밴드 폭(상단-하단)/중심선 % 하한 (0 = 조건 없음)
    # fvg: 첫 5분봉(9:30~9:35) 고가·저가 → 1분봉 FVG(3봉 갭)가 고가 위면 롱, 저가 아래면 숏, 갭 되돌림 지정가 진입
    fvg_loc: str = "zone"          # zone = 갭 전체가 고가 위(저가 아래) / mid = 가운데 봉 종가가 고가 위(저가 아래)
    fvg_entry: str = "edge"        # edge = 갭 첫 닿음(3번째 봉 저가) / mid = 갭 가운데 / full = 갭 다 메움(1번째 봉 고가)
    fvg_pick: str = "latest"       # first = 그날 첫 FVG 만 / latest = 체결 전 새 FVG 가 나오면 그것으로 바꿈
    fvg_target_r: float = 0.0      # 익절 = 위험(진입가-첫 봉 반대편) x R (0 = 없음, 정리 시각까지)
    fvg_min_gap: float = 0.0       # 갭 크기 / 가격 % 하한
    fvg_side: str = "both"         # long / short / both
    fvg_minutes: int = 350         # 9:30 부터 N분 뒤 모두 정리 (350 = 15:20)
    fvg_stop_mode: str = "touch"   # touch = 첫 봉 반대편 닿으면 / close = 1분봉 종가가 넘으면
    fvg_one_per_day: bool = True   # 하루 한 종목 (모든 종목 중 가장 먼저 체결된 것)
    surge_entry: str = "stop"  # stop = 전날 종가에 역지정가 / next = 넘은 1분봉 다음 봉 시가 (보수적)
    size_mode: str = "risk"  # risk = 손절 금액 기준(위험 %) / lev = 평가금액 x 고정 배율
    lev_etf: float = 2.0     # size_mode=lev: 레버리지 ETF 배율
    lev_stock: float = 5.0   # size_mode=lev: 일반 주식·1배 ETF 배율
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
        if p.size_mode == "lev":
            base = t.symbol[:-4] if t.symbol.endswith("USDT") else t.symbol
            lev = p.lev_etf if base in LEVERAGED_ETFS else p.lev_stock
            room = equity * rule.max_leverage - sum(x.entry * x.qty for x in open_)
            q = min(equity * lev * t.size_frac, max(room, 0.0)) / t.entry
            qty = math.floor(q / step + 1e-9) * step if step > 0 else q
            if qty * t.entry < mn:
                qty = 0.0
        elif t.size_risk:  # 분할 조각: 같은 셋업 전체가 손절되면 평가금액 x 위험% 를 잃도록
            per = t.size_risk + t.entry * (2 * rule.fee_pct + rule.slippage_pct) / 100
            q = equity * rule.risk_pct / 100 / per * t.size_frac
            room = equity * rule.max_leverage - sum(x.entry * x.qty for x in open_)
            q = min(q, max(room, 0.0) / t.entry)
            qty = math.floor(q / step + 1e-9) * step if step > 0 else q
            if qty * t.entry < mn:
                qty = 0.0
        else:
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


def rsi_values(closes: list[float], period: int) -> list[float | None]:
    """와일더 RSI. 앞 period 개는 None."""
    out: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0)
        losses += max(-ch, 0)
    ag, al = gains / period, losses / period
    out[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(ch, 0)) / period
        al = (al * (period - 1) + max(-ch, 0)) / period
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


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
    rsi = rsi_values(closes, p.vwma_rsi_period)

    # i번째 봉이 끝난 뒤의 신호 (vwma_slope_bars 0 = 기울기 조건 없음)
    def signal(i: int) -> int:
        n, s = max(p.vwma_min_above, 1), p.vwma_slope_bars
        if i < max(n, s) or vwma[i] is None or vwma[i - s] is None:
            return 0
        recent = range(i - n + 1, i + 1)
        if any(vwma[j] is None for j in recent):
            return 0
        r = rsi[i]
        lo, hi = p.vwma_rsi_long_min, p.vwma_rsi_long_max
        rsi_long = r is None and lo <= 0 and hi >= 100 or r is not None and lo <= r <= hi
        rsi_short = r is None and lo <= 0 and hi >= 100 or r is not None and 100 - hi <= r <= 100 - lo
        if (s == 0 or vwma[i] > vwma[i - s]) and all(closes[j] > vwma[j] for j in recent) and rsi_long:
            return 1
        if (s == 0 or vwma[i] < vwma[i - s]) and all(closes[j] < vwma[j] for j in recent) and rsi_short:
            return -1
        return 0
    # 닿음 횟수: 직전 봉 종가가 VWMA 위(아래)인 구간에서 이번 봉이 지정가 수준까지 오면 1회. 종가가 반대로 넘어가면 0 부터
    touch_no = [0] * len(keys)
    side_prev, cnt = 0, 0
    for j in range(1, len(keys)):
        i = j - 1
        if vwma[i] is None:
            continue
        side = 1 if closes[i] > vwma[i] else -1 if closes[i] < vwma[i] else 0
        if side != side_prev:
            cnt, side_prev = 0, side
        g = groups[keys[j]]
        lvl = vwma[i] * (1 + side * p.vwma_band / 100)
        if side == 1 and min(b.l for b in g) <= lvl or side == -1 and max(b.h for b in g) >= lvl:
            cnt += 1
        touch_no[j] = cnt
    sig_at = {}  # 1분봉 시각 → (방향, VWMA) : 그 1분봉이 속한 N분봉 직전 봉의 신호
    for i in range(len(keys) - 1):
        sg = signal(i)
        if p.vwma_max_touch and touch_no[i + 1] > p.vwma_max_touch:
            continue
        if sg and (p.direction == "both" or (sg == 1) == (p.direction == "long")):
            sig_at[keys[i + 1]] = (sg, vwma[i])
    trades: list[Trade] = []
    used: set[datetime] = set()  # 신호 봉마다 진입 한 번
    pos = None  # (side, entry_bar, entry, stop, target)
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
        if k not in sig_at or k in used:
            continue
        side, v = sig_at[k]
        lvl = v * (1 + side * p.vwma_band / 100)
        if not ((b.l <= lvl) if side == 1 else (b.h >= lvl)):
            continue
        used.add(k)
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



def tf_bars(mins: list[MinBar], tf: int, source: str) -> list[tuple[datetime, float, float, float, float]]:
    """1분봉 → tf 분봉 (시각, 시가, 고가, 저가, 종가). source=rth 면 정규장 봉만."""
    groups: dict[datetime, list[MinBar]] = {}
    for b in mins:
        if source == "rth" and not is_rth(b.t):
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        groups.setdefault(k, []).append(b)
    return [(k, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c) for k, g in sorted(groups.items())]


def swing_setups(bars, n: int) -> list[tuple[datetime, float, float]]:
    """상승 패턴 확정 시점 목록: (확정된 봉 다음 봉 시각, 저점2, 고점2).
    피벗은 좌우 n봉 최고·최저, 오른쪽 n봉이 지나야 확정. 같은 종류가 이어지면 더 극단인 것만 남김."""
    hs, ls = [b[2] for b in bars], [b[3] for b in bars]
    piv = []  # (종류 'H'/'L', 값)
    out = []
    for c in range(2 * n, len(bars)):
        i = c - n  # 이번 봉(c)이 끝나면 i 가 피벗인지 확정
        new = []
        if hs[i] >= max(hs[i - n:i]) and hs[i] > max(hs[i + 1:c + 1]):
            new.append(("H", hs[i]))
        if ls[i] <= min(ls[i - n:i]) and ls[i] < min(ls[i + 1:c + 1]):
            new.append(("L", ls[i]))
        for kind, v in new:
            if piv and piv[-1][0] == kind:
                if (kind == "H" and v > piv[-1][1]) or (kind == "L" and v < piv[-1][1]):
                    piv[-1] = (kind, v)
                else:
                    continue
            else:
                piv.append((kind, v))
            if kind == "H" and len(piv) >= 4:
                (k1, l1), (k2, h1), (k3, l2), (k4, h2) = piv[-4:]
                if (k1, k2, k3) == ("L", "H", "L") and l2 > l1 and h2 > h1 and c + 1 < len(bars):
                    out.append((bars[c + 1][0], l2, h2, bars[c][4]))
    return out


def swing_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """상승 패턴 되돌림 분할 매수 (1분봉으로 체결 순서 확인). 반환: 거래 조각, 통계."""
    slip = p.rule.slippage_pct / 100
    bars = tf_bars(mins, p.swing_tf, p.swing_source)
    setups = swing_setups(bars, p.swing_n)
    stats = {"setups": len(setups), "skipped_below": 0, "cancel_high": 0, "cancel_low": 0, "filled": 0}
    by_start: dict[datetime, tuple] = {}
    for t0, l2, h2, close in setups:
        by_start[t0] = (l2, h2, close)
    trades: list[Trade] = []
    setup = None   # dict
    legs: list[list] = []  # [entry, 남은 단위, 진입 1분봉]
    tp1_done = False

    def close_legs(b, px, reason, fee_exit=None):
        nonlocal legs
        for e, u, eb in legs:
            if u > 0:
                trades.append(Trade(symbol, eb.t.date(), 1, eb.t, e, setup["l2"], b.t, px, reason,
                                    fee_entry=p.maker_fee, fee_exit=fee_exit, size_risk=setup["risk"], size_frac=u))
        legs = []

    for b in mins:
        k = b.t.replace(minute=b.t.minute - b.t.minute % p.swing_tf, second=0, microsecond=0)
        if k in by_start and not legs:
            l2, h2, close = by_start.pop(k)
            d = h2 - l2
            m1, m2 = l2 + d * p.swing_buy1, l2 + d * p.swing_buy2
            if d <= 0 or d / h2 * 100 < p.swing_min_d:
                pass
            elif close <= m1:
                stats["skipped_below"] += 1
            else:
                setup = {"l2": l2, "h2": h2, "m1": m1, "m2": m2, "t2": h2 + d * p.swing_tp2,
                         "risk": (m1 - l2) + (m2 - l2) if p.swing_split else (m1 - l2),
                         "f1": False, "f2": not p.swing_split}
                tp1_done = not p.swing_split
        if not is_rth(b.t):
            continue
        close_t = _close_t(b.t.date(), p)
        if legs and (b.t >= close_t or legs[0][2].t.date() != b.t.date()):
            close_legs(b, b.o * (1 - slip), "time")
            setup = None
            continue
        if setup is None:
            continue
        if legs:
            if b.l <= setup["l2"]:
                close_legs(b, min(b.o, setup["l2"]) * (1 - slip), "stop")
                setup = None
                continue
            if not tp1_done and b.h >= setup["h2"]:
                tp1_done = True
                total = sum(u for _, u, _ in legs)
                sell = total / 2
                px = max(b.o, setup["h2"])
                rest = []
                for e, u, eb in legs:  # 먼저 산 조각부터 판다
                    q = min(u, sell)
                    if q > 0:
                        trades.append(Trade(symbol, eb.t.date(), 1, eb.t, e, setup["l2"], b.t, px, "target",
                                            fee_entry=p.maker_fee, fee_exit=p.maker_fee, size_risk=setup["risk"], size_frac=q))
                        sell -= q
                    rest.append([e, u - q, eb])
                legs = [x for x in rest if x[1] > 0]
            if tp1_done and legs and b.h >= setup["t2"]:
                close_legs(b, max(b.o, setup["t2"]), "target", fee_exit=p.maker_fee)
                setup = None
                continue
            if tp1_done and p.swing_split:
                continue  # 1차 매도 뒤에는 추가 매수 안 함
            if not p.swing_split:
                continue
        # 진입 대기 / 추가 매수
        if not legs and b.h > setup["h2"]:
            stats["cancel_high"] += 1
            setup = None
            continue
        if not legs and b.o <= setup["l2"]:  # 저점2 아래로 갭 → 패턴 깨짐
            stats["cancel_low"] += 1
            setup = None
            continue
        if b.t.time() < p.vwap_start or b.t.time() >= p.buy_until or b.t >= close_t:
            continue
        filled_now = False
        if not setup["f1"] and b.l <= setup["m1"]:
            setup["f1"] = True
            legs.append([min(b.o, setup["m1"]), 1.0, b])
            stats["filled"] += 1
            filled_now = True
        if setup["f1"] and not setup["f2"] and b.l <= setup["m2"]:
            setup["f2"] = True
            legs.append([min(b.o, setup["m2"]), 1.0, b])
            filled_now = True
        if filled_now and b.l <= setup["l2"]:  # 진입 1분봉 안에서 저점2 이탈 → 보수적으로 손절
            close_legs(b, setup["l2"] * (1 - slip), "stop")
            setup = None
    return trades, stats


def flag_setups(bars, p: Params) -> dict[datetime, tuple[float, float, float]]:
    """i번째 tf 분봉이 끝난 뒤 깃발형이면 다음 봉 시각 → (돌파 가격, 깃발 저점, 깃대 길이).
    bars: (시각, 시가, 고가, 저가, 종가, 거래량)"""
    out = {}
    hs, ls, vs = [b[2] for b in bars], [b[3] for b in bars], [b[5] for b in bars]
    for i in range(p.flag_pole_bars - 1 + p.flag_min, len(bars) - 1):
        lo_w = max(0, i - p.flag_max)
        h = max(range(lo_w, i + 1), key=lambda j: (hs[j], j))  # 같은 값이면 나중 봉
        m = i - h
        if m < p.flag_min or h - p.flag_pole_bars + 1 < 0:
            continue
        top = hs[h]
        if any(hs[j] >= top for j in range(h + 1, i + 1)):
            continue
        p0 = h - p.flag_pole_bars + 1
        pole_low = min(ls[p0:h + 1])
        pole = top - pole_low
        if pole <= 0 or pole / pole_low * 100 < p.flag_pole_pct:
            continue
        flag_low = min(ls[h + 1:i + 1])
        if (top - flag_low) > pole * p.flag_retrace:
            continue
        if p.flag_vol and sum(vs[h + 1:i + 1]) / m >= sum(vs[p0:h + 1]) / (h - p0 + 1):
            continue
        level = max(hs[h + 1:i + 1]) if p.flag_entry == "flag" else top
        out[bars[i + 1][0]] = (level, flag_low, pole)
    return out


def flag_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """상승 깃발형 돌파 매수 (롱만). 직전 완성된 tf 분봉 기준 패턴이면 다음 tf 분 동안 1분봉으로 돌파·손절·익절 확인.
    한 번에 한 포지션, 진입은 정규장 vwap_start~buy_until, exit_time 정리."""
    slip = p.rule.slippage_pct / 100
    tf = p.flag_tf
    groups: dict[datetime, list[MinBar]] = {}
    for b in mins:
        if p.flag_source == "rth" and not is_rth(b.t):
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        groups.setdefault(k, []).append(b)
    bars = [(k, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
            for k, g in sorted(groups.items())]
    setups = flag_setups(bars, p)
    stats = {"setups": len(setups), "entries": 0}
    trades: list[Trade] = []
    pos = None  # (진입 1분봉, 진입가, 손절, 익절)
    for b in mins:
        if not is_rth(b.t):
            continue
        d = b.t.date()
        close_t = _close_t(d, p)
        if pos:
            eb, entry, stop, target = pos
            reason = px = None
            if b.t >= close_t or eb.t.date() != d:
                reason, px = "time", b.o * (1 - slip)
            elif b.l <= stop:
                reason, px = "stop", min(b.o, stop) * (1 - slip)
            elif target is not None and b.h >= target:
                reason, px = "target", max(b.o, target)
            if reason:
                trades.append(Trade(symbol, eb.t.date(), 1, eb.t, entry, stop, b.t, px, reason,
                                    fee_exit=p.maker_fee if reason == "target" else None))
                pos = None
            continue
        if b.t.time() < p.vwap_start or b.t.time() >= p.buy_until or b.t >= close_t:
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        if k not in setups:
            continue
        level, flag_low, pole = setups[k]
        if b.h < level:
            continue
        del setups[k]  # 신호 봉마다 한 번
        entry = max(b.o, level) * (1 + slip)
        stop = (flag_low if p.flag_stop == "flag" else (flag_low + entry) / 2 if p.flag_stop == "mid"
                else entry * (1 - p.stop_pct / 100))
        if stop >= entry:
            continue
        risk = entry - stop
        target = (entry + risk * p.flag_target_r if p.flag_target_r > 0
                  else entry + pole * p.flag_target if p.flag_target > 0 else None)
        stats["entries"] += 1
        if b.l <= stop and b.o >= level:  # 시가로 바로 진입했는데 같은 1분봉에서 손절가 → 보수적으로 손절
            trades.append(Trade(symbol, d, 1, b.t, entry, stop, b.t, stop * (1 - slip), "stop"))
            continue
        if b.o < level and (b.c if p.flag_entry_bar == "close" else b.l) <= stop:  # 돌파한 1분봉 안에서 손절가 → 손절
            trades.append(Trade(symbol, d, 1, b.t, entry, stop, b.t, stop * (1 - slip), "stop"))
            continue
        pos = (b, entry, stop, target)
    return trades, stats


def vbreak_setups(bars, p: Params) -> dict[datetime, tuple[int, float, float, int]]:
    """tf 분봉 [(시각, 시가, 고가, 저가, 종가, 거래량)] → {봉 시각: (방향, 지정가, 손절가, 셋업 번호)}.
    그 봉 동안 걸어 둘 주문 (직전 완성 봉까지의 값으로 계산)."""
    c = [b[4] for b in bars]
    v = [b[5] for b in bars]

    def ma(n: int, weighted: bool) -> list[float | None]:
        out: list[float | None] = [None] * len(c)
        sp = sv = 0.0
        for i in range(len(c)):
            w = v[i] if weighted else 1.0
            sp += c[i] * w
            sv += w
            if i >= n:
                wo = v[i - n] if weighted else 1.0
                sp -= c[i - n] * wo
                sv -= wo
            if i >= n - 1 and sv > 0:
                out[i] = sp / sv
        return out
    vw = ma(p.vb_len, True)
    fast = ma(p.vb_fast, p.vb_ma == "vwma")
    mid = ma(p.vb_mid, p.vb_ma == "vwma")
    sides = [s for s in (-1, 1) if p.vb_side == "both" or (s == -1) == (p.vb_side == "short")]
    out: dict[datetime, tuple[int, float, float, int]] = {}
    active: list[list] = []  # [방향, 돌파 봉 번호, 손절가, 주문 낸 봉 번호 or None, 셋업 번호]
    sid = 0
    for i in range(1, len(bars)):
        if vw[i] is None or vw[i - 1] is None:
            continue
        keep = []
        for st in active:
            s, j, stop, start, k = st
            if (c[i] > vw[i]) if s == -1 else (c[i] < vw[i]):  # 회복 → 취소
                continue
            if start is None and i - j >= p.vb_wait:
                st[3] = start = i
            if start is not None and p.vb_expire and i - start >= p.vb_expire:
                continue
            keep.append(st)
        active = keep
        for s in sides:
            if fast[i - 1] is None or mid[i - 1] is None or vw[i - 2 if i >= 2 else 0] is None:
                continue
            if s == -1:
                trend = fast[i - 1] > mid[i - 1] > vw[i - 1] and vw[i - 1] > vw[i - 2]
                cross = c[i - 1] >= vw[i - 1] and c[i] < vw[i]
                stop = bars[i][2]
            else:
                trend = fast[i - 1] < mid[i - 1] < vw[i - 1] and vw[i - 1] < vw[i - 2]
                cross = c[i - 1] <= vw[i - 1] and c[i] > vw[i]
                stop = bars[i][3]
            if trend and cross:
                sid += 1
                active.append([s, i, stop, i if p.vb_wait == 0 else None, sid])
        if i + 1 < len(bars):
            for s, j, stop, start, k in active:
                if start is not None and ((vw[i] < stop) if s == -1 else (vw[i] > stop)):
                    out.setdefault(bars[i + 1][0], (s, vw[i], stop, k))  # 같은 봉에 여러 셋업이면 먼저 것
    return out


def vbreak_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """VWMA 하향 돌파 뒤 회복 실패 → VWMA 되돌림 지정가 숏 (1분봉으로 체결·손절·익절 확인).
    진입은 정규장 vwap_start~buy_until, exit_time 정리, 한 번에 한 포지션, 셋업마다 한 번."""
    slip = p.rule.slippage_pct / 100
    tf = p.vb_tf
    groups: dict[datetime, list[MinBar]] = {}
    for b in mins:
        if p.vb_source == "rth" and not is_rth(b.t):
            continue
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        groups.setdefault(k, []).append(b)
    bars = [(k, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
            for k, g in sorted(groups.items())]
    orders = vbreak_setups(bars, p)
    stats = {"setups": len({o[3] for o in orders.values()}), "entries": 0}
    trades: list[Trade] = []
    used: set[int] = set()
    pos = None  # (방향, 진입 1분봉, 진입가, 손절, 익절)
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
        if k not in orders:
            continue
        side, lvl, stop, sid = orders[k]
        if sid in used or not ((b.h >= lvl) if side == -1 else (b.l <= lvl)):
            continue
        used.add(sid)
        entry = max(b.o, lvl) if side == -1 else min(b.o, lvl)
        if (stop <= entry) if side == -1 else (stop >= entry):  # 시가가 손절가를 넘어 갭 → 거래 안 함
            continue
        stats["entries"] += 1
        risk = abs(entry - stop)
        target = entry + side * risk * p.vb_target_r if p.vb_target_r > 0 else None
        if (b.h >= stop) if side == -1 else (b.l <= stop):  # 진입 1분봉 안에서 손절가 → 보수적으로 손절
            trades.append(Trade(symbol, d, side, b.t, entry, stop, b.t, stop * (1 - side * slip), "stop", fee_entry=p.maker_fee))
            continue
        pos = (side, b, entry, stop, target)
    return trades, stats



def rsi_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """장중 RSI 평균회귀 (1분봉으로 손절·익절 확인). 신호는 완성된 tf 분봉 종가 RSI, 진입·청산은 다음 tf 봉 첫 1분봉 시가(테이커).
    진입은 정규장 vwap_start~buy_until, 한 번에 한 포지션, exit_time 정리."""
    bars = tf_bars(mins, p.rsi_tf, p.rsi_source)
    rsi = rsi_values([b[4] for b in bars], p.rsi_period)
    sides = [s for s in (1, -1) if p.rsi_side == "both" or (s == 1) == (p.rsi_side == "long")]
    enter_at: dict[datetime, int] = {}   # tf 봉 시각 → 그 봉 시작에 진입할 방향
    exit_at: dict[datetime, set[int]] = {}  # tf 봉 시각 → 그 봉 시작에 청산할 방향
    for i in range(len(bars) - 1):
        r = rsi[i]
        if r is None:
            continue
        nxt = bars[i + 1][0]
        for s in sides:
            lo = p.rsi_buy if s == 1 else 100 - p.rsi_buy
            hi = p.rsi_sell if s == 1 else 100 - p.rsi_sell
            if (r < lo) if s == 1 else (r > lo):
                enter_at.setdefault(nxt, s)
            if (r >= hi) if s == 1 else (r <= hi):
                exit_at.setdefault(nxt, set()).add(s)
    return signal_trades(symbol, mins, p, p.rsi_tf, enter_at, exit_at)


def boll_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """장중 볼린저밴드. 신호는 완성된 tf 분봉 종가, 진입·청산은 다음 tf 봉 첫 1분봉 시가.
    reversion: 종가가 하단 아래면 롱 → 종가가 중심선(boll_exit=mid) 또는 상단(upper) 이상이면 청산 (숏은 반대)
    breakout: 종가가 상단 위면 롱 → 종가가 중심선 아래면 청산 (숏은 반대)"""
    bars = tf_bars(mins, p.boll_tf, p.boll_source)
    c = [b[4] for b in bars]
    n, k = p.boll_len, p.boll_k
    sides = [s for s in (1, -1) if p.boll_side == "both" or (s == 1) == (p.boll_side == "long")]
    enter_at: dict[datetime, int] = {}
    exit_at: dict[datetime, set[int]] = {}
    for i in range(n - 1, len(bars) - 1):
        w = c[i - n + 1:i + 1]
        mid = sum(w) / n
        sd = (sum((x - mid) ** 2 for x in w) / n) ** 0.5
        up, lo = mid + k * sd, mid - k * sd
        if p.boll_min_width > 0 and (up - lo) / mid * 100 < p.boll_min_width:
            width_ok = False
        else:
            width_ok = True
        nxt = bars[i + 1][0]
        x = c[i]
        for s in sides:
            if p.boll_mode == "reversion":
                ent = (x < lo) if s == 1 else (x > up)
                tgt = (mid if p.boll_exit == "mid" else up) if s == 1 else (mid if p.boll_exit == "mid" else lo)
                ext = (x >= tgt) if s == 1 else (x <= tgt)
            else:
                ent = (x > up) if s == 1 else (x < lo)
                ext = (x < mid) if s == 1 else (x > mid)
            if ent and width_ok:
                enter_at.setdefault(nxt, s)
            if ext:
                exit_at.setdefault(nxt, set()).add(s)
    return signal_trades(symbol, mins, p, p.boll_tf, enter_at, exit_at)


def signal_trades(symbol: str, mins: list[MinBar], p: Params, tf: int, enter_at: dict, exit_at: dict) -> tuple[list[Trade], dict]:
    """tf 봉 시작 시각별 진입·청산 신호로 1분봉 매매. stop_pct 손절·take_profit_pct 익절(0 = 없음), exit_time 정리."""
    slip = p.rule.slippage_pct / 100
    stats = {"signals": len(enter_at), "entries": 0}
    trades: list[Trade] = []
    pos = None  # (방향, 진입 1분봉, 진입가, 손절, 익절)
    for b in mins:
        if not is_rth(b.t):
            continue
        d = b.t.date()
        close_t = _close_t(d, p)
        k = b.t.replace(minute=b.t.minute - b.t.minute % tf, second=0, microsecond=0)
        first = b.t == k  # tf 봉의 첫 1분봉
        if pos:
            side, eb, entry, stop, target = pos
            reason = px = None
            if b.t >= close_t or eb.t.date() != d:
                reason, px = "time", b.o * (1 - side * slip)
            elif first and b.t != eb.t and side in exit_at.get(k, ()):
                reason, px = "signal", b.o * (1 - side * slip)
            elif stop is not None and ((b.l <= stop) if side == 1 else (b.h >= stop)):
                reason = "stop"
                px = (min(b.o, stop) if side == 1 else max(b.o, stop)) * (1 - side * slip)
            elif target is not None and ((b.h >= target) if side == 1 else (b.l <= target)):
                reason, px = "target", (max(b.o, target) if side == 1 else min(b.o, target))
            if reason:
                trades.append(Trade(symbol, eb.t.date(), side, eb.t, entry, stop if stop is not None else entry * (1 - side * 0.05),
                                    b.t, px, reason, fee_exit=p.maker_fee if reason == "target" else None))
                pos = None
            else:
                continue
        if not first or k not in enter_at:
            continue
        if b.t.time() < p.vwap_start or b.t.time() >= p.buy_until or b.t >= close_t:
            continue
        side = enter_at[k]
        entry = b.o * (1 + side * slip)
        stop = entry * (1 - side * p.stop_pct / 100) if p.stop_pct > 0 else None
        target = entry * (1 + side * p.take_profit_pct / 100) if p.take_profit_pct > 0 else None
        stats["entries"] += 1
        if stop is not None and ((b.l <= stop) if side == 1 else (b.h >= stop)):  # 진입 1분봉 안 손절 → 보수적으로 손절
            trades.append(Trade(symbol, d, side, b.t, entry, stop, b.t, stop * (1 - side * slip), "stop"))
            continue
        pos = (side, b, entry, stop, target)
    return trades, stats


def first5_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """첫 5분봉 돌파·눌림 매수. 손절 = 5분봉 종가가 첫 봉 저가 아래 or 매수가 -f5_stop_pct% 닿음, 익절 절반 tp1·나머지 tp2."""
    slip = p.rule.slippage_pct / 100
    days: dict[date, list[MinBar]] = defaultdict(list)
    for b in mins:
        if is_rth(b.t):
            days[b.t.date()].append(b)
    trades: list[Trade] = []
    st = Counter()
    prev_close = None
    first_vols: list[float] = []
    for d in sorted(days):
        bars = days[d]
        pc, prev_close = prev_close, bars[-1].c
        if bars[0].t.time() != time(9, 30):
            continue
        first = [b for b in bars if b.t.time() < time(9, 35)]
        o1, h1, l1, c1, v1 = first[0].o, max(b.h for b in first), min(b.l for b in first), first[-1].c, sum(b.v for b in first)
        avg_v = sum(first_vols[-10:]) / len(first_vols[-10:]) if first_vols else None
        first_vols.append(v1)
        st["days"] += 1
        gap = (o1 / pc - 1) * 100 if pc else None
        if gap is None or not (p.f5_gap_min <= gap <= p.f5_gap_max):
            continue
        st["gap_ok"] += 1
        if h1 <= l1:
            continue
        side = 1 if c1 > o1 else -1
        if (side == 1 and p.f5_side == "short") or (side == -1 and p.f5_side == "long"):
            continue
        if abs(c1 - o1) / (h1 - l1) < p.f5_min_body or c1 == o1:
            continue
        if side == -1 and p.f5_pattern != "A":
            continue  # 숏은 돌파(A)만
        if p.f5_vol_mult > 0 and (avg_v is None or v1 < avg_v * p.f5_vol_mult):
            continue
        st["first_ok"] += 1
        close_t = datetime.combine(d, p.exit_time, ET)
        rest = [b for b in bars if b.t.time() >= time(9, 35)]
        # 5분봉 경계 표시: 각 1분봉이 그 5분봉의 마지막이면 5분봉 (o,h,l,c) 반환
        five: dict[datetime, list[MinBar]] = defaultdict(list)
        for b in rest:
            five[b.t.replace(minute=b.t.minute - b.t.minute % 5)].append(b)
        entry = entry_i = None
        mid = (o1 + h1) / 2 if False else (h1 + l1) / 2
        support = o1 if p.f5_b_support == "open" else mid
        dipped = False
        for i, b in enumerate(rest):
            k = b.t.replace(minute=b.t.minute - b.t.minute % 5)
            g = five[k]
            is_last = b is g[-1]
            if p.f5_pattern == "A":
                if b.t.time() >= p.f5_breakout_until:
                    break
                lvl = h1 if side == 1 else l1
                if p.f5_entry == "touch" and ((b.h >= lvl) if side == 1 else (b.l <= lvl)):
                    entry, entry_i = (max(b.o, lvl) if side == 1 else min(b.o, lvl)) * (1 + side * slip), i
                    break
                if p.f5_entry == "close" and is_last and ((b.c > lvl) if side == 1 else (b.c < lvl)):
                    entry, entry_i = b.c * (1 + side * slip), i
                    break
            else:
                if b.t.time() >= p.f5_b_until:
                    break
                if is_last:
                    go, gh, gl, gc = g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c
                    if gc < support:
                        break  # 지지선을 종가로 깨면 포기
                    if gl <= mid:
                        if dipped and gc > go:
                            entry, entry_i = gc * (1 + slip), i
                            break
                        dipped = True
                        if gc > go:  # 눌림 봉 자체가 반등 양봉이면 그 봉에서
                            entry, entry_i = gc * (1 + slip), i
                            break
                    elif dipped and gc > go:
                        entry, entry_i = gc * (1 + slip), i
                        break
        if entry is None:
            continue
        st["entries"] += 1
        s_ = side
        stop_pct = p.f5_stop_short if (s_ == -1 and p.f5_stop_short > 0) else p.f5_stop_pct
        tp1_pct = p.f5_tp_short if (s_ == -1 and p.f5_tp_short > 0) else p.f5_tp1
        stop_px = entry * (1 - s_ * stop_pct / 100)
        tp1, tp2 = entry * (1 + s_ * tp1_pct / 100), entry * (1 + s_ * p.f5_tp2 / 100)
        risk = abs(entry - stop_px)
        eb = rest[entry_i]
        units = 1.0
        half_done = False

        def piece(b, px, reason, frac, maker=False):
            trades.append(Trade(symbol, d, s_, eb.t, entry, stop_px, b.t, px, reason,
                                fee_exit=p.maker_fee if maker else None, size_risk=risk, size_frac=frac))

        for b in rest[entry_i + 1:]:
            k = b.t.replace(minute=b.t.minute - b.t.minute % 5)
            if b.t >= close_t:
                piece(b, b.o * (1 - s_ * slip), "time", units)
                units = 0
                break
            if (b.l <= stop_px) if s_ == 1 else (b.h >= stop_px):
                px = min(b.o, stop_px) if s_ == 1 else max(b.o, stop_px)
                piece(b, px * (1 - s_ * slip), "stop", units)
                units = 0
                break
            hit1 = (b.h >= tp1) if s_ == 1 else (b.l <= tp1)
            if not p.f5_split and hit1:
                piece(b, max(b.o, tp1) if s_ == 1 else min(b.o, tp1), "target", 1.0, True)
                units = 0
                break
            if not half_done and hit1:
                piece(b, max(b.o, tp1) if s_ == 1 else min(b.o, tp1), "target", 0.5, True)
                units, half_done = 0.5, True
            if half_done and ((b.h >= tp2) if s_ == 1 else (b.l <= tp2)):
                piece(b, max(b.o, tp2) if s_ == 1 else min(b.o, tp2), "target", units, True)
                units = 0
                break
            if b is five[k][-1] and ((b.c < l1) if s_ == 1 else (b.c > h1)):  # 5분봉 종가가 첫 봉 반대편 끝을 넘음
                piece(b, b.c * (1 - s_ * slip), "stop", units)
                units = 0
                break
        if units > 0:
            last = bars[-1]
            piece(last, last.c * (1 - s_ * slip), "time", units)
    return trades, dict(st)


def fvg_at(a, m, b, h1: float, l1: float, loc: str = "zone", entry: str = "full", side_filter: str = "both",
           min_gap: float = 0.0) -> list[tuple[int, float]]:
    """1분봉 3개(a, m, b)가 만든 FVG 가 첫 5분봉 고가(h1) 위 / 저가(l1) 아래면 [(방향, 진입 지정가)]. 봇과 백테스트 공용.
    상승 갭 = a 고가 < b 저가 (갭 a.h~b.l), 하락 갭 = b 고가 < a 저가 (갭 b.h~a.l).
    loc: zone = 갭 전체가 고가 위(저가 아래) / mid = 가운데 봉 종가가 고가 위(저가 아래).
    entry: edge = 갭 첫 닿음 / mid = 갭 가운데 / full = 갭을 다 메운 곳."""
    out = []
    for side in (1, -1):
        if (side == 1 and side_filter == "short") or (side == -1 and side_filter == "long"):
            continue
        if side == 1:
            lo, hi = a.h, b.l
            ok = hi > lo and (lo >= h1 if loc == "zone" else m.c > h1)
        else:
            lo, hi = b.h, a.l
            ok = hi > lo and (hi <= l1 if loc == "zone" else m.c < l1)
        if not ok or (hi - lo) / b.c * 100 < min_gap:
            continue
        edge, full = (hi, lo) if side == 1 else (lo, hi)
        out.append((side, {"edge": edge, "full": full, "mid": (lo + hi) / 2}[entry]))
    return out


def fvg_trades(symbol: str, mins: list[MinBar], p: Params) -> tuple[list[Trade], dict]:
    """첫 5분봉 고가·저가 기준 1분봉 FVG 되돌림 진입. 손절 = 롱은 첫 봉 저가, 숏은 첫 봉 고가 이탈. 하루 한 번."""
    slip = p.rule.slippage_pct / 100
    days: dict[date, list[MinBar]] = defaultdict(list)
    for b in mins:
        if is_rth(b.t):
            days[b.t.date()].append(b)
    trades: list[Trade] = []
    st = Counter()
    for d in sorted(days):
        bars = days[d]
        if bars[0].t.time() != time(9, 30):
            continue
        first = [b for b in bars if b.t.time() < time(9, 35)]
        h1, l1 = max(b.h for b in first), min(b.l for b in first)
        rest = [b for b in bars if b.t.time() >= time(9, 35)]
        close_t = datetime.combine(d, time(9, 30), ET) + timedelta(minutes=p.fvg_minutes)
        st["days"] += 1
        pend = None  # (side, 진입가)
        entry = None
        for i, b in enumerate(rest):
            if b.t >= close_t:
                break
            if pend is not None:  # 앞 봉까지 생긴 FVG 의 지정가 체결 확인
                side, lvl = pend
                if (b.l <= lvl) if side == 1 else (b.h >= lvl):
                    entry, ei = (min(b.o, lvl) if side == 1 else max(b.o, lvl)), i
                    break
            if i >= 2 and (pend is None or p.fvg_pick == "latest"):
                for found in fvg_at(rest[i - 2], rest[i - 1], b, h1, l1, p.fvg_loc, p.fvg_entry, p.fvg_side, p.fvg_min_gap):
                    pend = found
                    st["fvg"] += 1
        if entry is None:
            continue
        side = pend[0]
        stop = l1 if side == 1 else h1
        risk = abs(entry - stop)
        tgt = entry + side * risk * p.fvg_target_r if p.fvg_target_r > 0 else None
        eb = rest[ei]
        st["entries"] += 1

        def done(b, px, reason, maker=False):
            trades.append(Trade(symbol, d, side, eb.t, entry, stop, b.t, px, reason,
                                fee_entry=p.maker_fee, fee_exit=p.maker_fee if maker else None))

        for j, b in enumerate(rest[ei:]):
            if j > 0 and b.t >= close_t:
                done(b, b.o * (1 - side * slip), "time")
                break
            if p.fvg_stop_mode == "touch":
                hit = (b.l < stop) if side == 1 else (b.h > stop)
                px = (min(b.o, stop) if side == 1 else max(b.o, stop)) if j > 0 else stop
            else:
                hit = (b.c < stop) if side == 1 else (b.c > stop)
                px = b.c
            if hit:
                done(b, px * (1 - side * slip), "stop")
                break
            if tgt is not None and j > 0 and ((b.h >= tgt) if side == 1 else (b.l <= tgt)):
                done(b, max(b.o, tgt) if side == 1 else min(b.o, tgt), "target", True)
                break
        else:
            last = rest[-1]
            done(last, last.c * (1 - side * slip), "time")
    return trades, dict(st)


def one_per_day(trades: list[Trade], order: dict | None = None) -> list[Trade]:
    """하루 한 종목: 진입이 가장 빠른 거래만 (같은 분이면 order[(날짜, 종목)] 작은 순, 없으면 종목 이름 순)."""
    best: dict[date, Trade] = {}
    for t in sorted(trades, key=lambda t: (t.entry_t, (order or {}).get((t.day, t.symbol), 10**9), t.symbol)):
        best.setdefault(t.day, t)
    return sorted(best.values(), key=lambda t: t.entry_t)


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
        if p.strategy == "first5":
            tr, st = first5_trades(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: {st}")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        if p.strategy == "swing":
            tr, st = swing_trades(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: 패턴 {st['setups']}개, 확정 때 이미 1차 매수가 아래 {st['skipped_below']}, "
                      f"체결 전 고점 돌파 취소 {st['cancel_high']}, 1차 체결 {st['filled']}")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        if p.strategy == "flag":
            tr, st = flag_trades(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: 깃발형 신호 {st['setups']}개, 진입 {st['entries']}건")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        if p.strategy in ("rsi", "boll"):
            tr, st = (rsi_trades if p.strategy == "rsi" else boll_trades)(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: 신호 {st['signals']}개, 진입 {st['entries']}건")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        if p.strategy == "fvg":
            tr, st = fvg_trades(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: FVG {st.get('fvg', 0)}개, 진입 {st.get('entries', 0)}건")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
        if p.strategy == "vbreak":
            tr, st = vbreak_trades(sym, load_minutes(f), p)
            if verbose:
                print(f"{sym}: 셋업 {st['setups']}개, 진입 {st['entries']}건")
            for t in tr:
                if (start and t.day < start) or (end and t.day > end):
                    continue
                apply_funding(t, funding)
                trades.append(t)
            continue
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
    if p.strategy == "fvg" and p.fvg_one_per_day:
        trades = one_per_day(trades)
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
