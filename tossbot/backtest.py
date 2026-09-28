"""눌림목 전략 백테스트 (일봉 기반).

실전 봇과 같은 판단 함수(selector.analyze / touch_zone, 호가 반올림)를 그대로 사용한다.

    # 1) 과거 일봉 내려받기 (최초 1회, 캐시에 저장)
    python -m tossbot.backtest download --source pykrx --start 2023-09-01
    # 2) 연도별 백테스트 (.env 의 전략 설정을 그대로 사용)
    python -m tossbot.backtest run --years 2024 2025 2026

일봉만으로 장중 체결을 재현하기 위한 가정
  매수 (7일선 터치 구간 [lo, hi], selector.touch_zone)
    - 시가가 구간 안이면 시가에 매수
    - 시가가 구간 위이고 저가가 hi 이하면 hi 에 매수 (위에서 내려와 터치)
    - 시가가 구간 아래(갭하락 이탈)이고 고가가 lo 이상이면 lo 에 매수 (다시 올라와 구간 진입)
    - 같은 날 신호가 여럿이면 급등폭 큰 순서로 빈 자리만큼
  매도
    - 시가가 손절가 이하 → 시가에 손절 (갭하락), 시가가 익절가 이상 → 시가에 익절
    - 장중 저가가 손절가 이하 → 손절가 / 고가가 익절가 이상 → 익절가
      (같은 날 둘 다 닿으면 손절로 가정: 보수적)
    - 매수 당일에도 저가가 손절가 이하면 손절로 가정 (구간 아래에서 올라와 산 경우는 제외)
    - 보유 MAX_HOLD_DAYS 거래일째 종가에 매도 (실전은 15:10)
  비용: 수수료(매수·매도), 매도 시 증권거래세(연도별), 슬리피지(시장가 체결 양쪽)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .broker import round_down_to_tick, round_up_to_tick
from .config import Config, load_dotenv
from .selector import Bar, PullbackParams, analyze, touch_zone
from .strategy import BUY_LIMIT_SLIPPAGE, params_from_config

log = logging.getLogger("tossbot.backtest")

# 매도 시 증권거래세(농특세 포함, 코스피·코스닥 동일하게 근사). 연도별 세율은 가정이므로 필요 시 수정
SELL_TAX_BY_YEAR = {2023: 0.0020, 2024: 0.0018, 2025: 0.0015, 2026: 0.0020}
DEFAULT_CACHE = Path("data/ohlcv")


# ---------------------------------------------------------------- settings
@dataclass
class BacktestSettings:
    params: PullbackParams = field(default_factory=PullbackParams)
    initial_cash: float = 1_000_000
    num_slots: int = 10
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 15.0
    max_hold_days: int = 10
    cooldown_days: int = 5
    commission: float = 0.00015
    slippage: float = 0.001  # 시장가 체결 시 불리한 방향 슬리피지
    # 신규 매수를 허용하는 날짜 (None 이면 모든 날). 예: 코스피 하락일만
    entry_dates: set | None = None

    @classmethod
    def from_config(cls, cfg: Config) -> "BacktestSettings":
        return cls(
            params=params_from_config(cfg),
            initial_cash=cfg.total_budget,
            num_slots=cfg.num_stocks,
            stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct,
            max_hold_days=cfg.max_hold_days,
            cooldown_days=cfg.rebuy_cooldown_days,
        )


@dataclass
class Trade:
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    qty: int
    surge_date: date
    exit_date: date | None = None
    exit_price: float | None = None
    reason: str = ""
    hold_days: int = 0
    pnl: float = 0.0  # 비용 포함 손익 (원)

    @property
    def ret(self) -> float:
        cost = self.entry_price * self.qty
        return self.pnl / cost if cost else 0.0


@dataclass
class Result:
    start: date
    end: date
    initial_cash: float
    final_equity: float
    trades: list[Trade]
    equity_curve: list[tuple[date, float]]
    open_positions: int

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_cash - 1

    @property
    def max_drawdown(self) -> float:
        peak, mdd = -math.inf, 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return mdd

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_date]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_win, gross_loss = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
        return {
            "기간": f"{self.start} ~ {self.end}",
            "수익률": self.total_return,
            "최종자산": self.final_equity,
            "MDD": self.max_drawdown,
            "거래수": len(closed),
            "승률": len(wins) / len(closed) if closed else 0.0,
            "평균수익": sum(t.ret for t in wins) / len(wins) if wins else 0.0,
            "평균손실": sum(t.ret for t in losses) / len(losses) if losses else 0.0,
            "손익비(PF)": gross_win / gross_loss if gross_loss else math.inf,
            "평균보유일": sum(t.hold_days for t in closed) / len(closed) if closed else 0.0,
            "청산사유": dict(Counter(t.reason for t in closed)),
            "미청산": self.open_positions,
        }


# ------------------------------------------------------------------ engine
@dataclass
class _Series:
    name: str
    bars: list[Bar]
    days: list[date]
    index: dict[date, int]


def _prepare(data: dict[str, tuple[str, list[Bar]]]) -> dict[str, _Series]:
    out = {}
    for sym, (name, bars) in data.items():
        bars = sorted((b for b in bars if b.close > 0 and b.open > 0), key=lambda b: b.day)
        if len(bars) < 30:
            continue
        days = [b.day for b in bars]
        out[sym] = _Series(name, bars, days, {d: i for i, d in enumerate(days)})
    return out


def _sell_value(qty: int, price: float, year: int, s: BacktestSettings) -> float:
    tax = SELL_TAX_BY_YEAR.get(year, 0.0020)
    return qty * price * (1 - s.commission - tax)


def run_backtest(
    data: dict[str, tuple[str, list[Bar]]], start: date, end: date, s: BacktestSettings | None = None
) -> Result:
    s = s or BacktestSettings()
    p = s.params
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})

    # 날짜별 후보: 최근 surge_lookback_days 거래일 안에 급등일이 있는 종목 (빠른 1차 필터)
    candidates: dict[date, set[str]] = defaultdict(set)
    for sym, ser in series.items():
        closes = [b.close for b in ser.bars]
        for i in range(1, len(closes)):
            if closes[i] / closes[i - 1] - 1 >= p.surge_pct / 100:
                for j in range(i + 1, min(i + 1 + p.surge_lookback_days, len(closes))):
                    candidates[ser.days[j]].add(sym)

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    cooldown_until: dict[str, date] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]
        cooldown_until[t.symbol] = day + timedelta(days=s.cooldown_days)

    for day in calendar:
        # 1) 보유 종목 청산 판단
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:  # 거래정지 등
                continue
            b = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - s.stop_loss_pct / 100))
            tp = round_up_to_tick(t.entry_price * (1 + s.take_profit_pct / 100))
            if b.open <= stop:
                close_position(t, day, b.open * (1 - s.slippage), "STOP_LOSS")
            elif b.open >= tp:
                close_position(t, day, b.open, "TAKE_PROFIT")
            elif b.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif b.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif s.max_hold_days and t.hold_days >= s.max_hold_days:
                close_position(t, day, b.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 신규 매수
        slots = s.num_slots - len(positions)
        if s.entry_dates is not None and day not in s.entry_dates:
            slots = 0
        if slots > 0:
            signals = []
            for sym in candidates.get(day, ()):
                if sym in positions or cooldown_until.get(sym, date.min) > day:
                    continue
                ser = series[sym]
                i = ser.index[day]
                item, _ = analyze(sym, ser.name, ser.bars[max(0, i - 80):i], day, p)
                if item is None:
                    continue
                lo, hi = touch_zone(item, p)
                b = ser.bars[i]
                if lo <= b.open <= hi:
                    fill, kind = b.open, "open"
                elif b.open > hi and b.low <= hi:
                    fill, kind = hi, "touch"
                elif b.open < lo and b.high >= lo:
                    fill, kind = lo, "rebound"
                else:
                    continue
                if fill <= item.pre_surge_close or fill > p.slot_budget:
                    continue
                signals.append((item.surge_pct, sym, item, fill, kind, b))
            signals.sort(key=lambda x: x[0], reverse=True)
            for _, sym, item, fill, kind, b in signals[:slots]:
                price = fill * (1 + s.slippage)
                qty = int(p.slot_budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, item.name, day, price, qty, item.surge_date)
                positions[sym] = t
                trades.append(t)
                stop = round_down_to_tick(price * (1 - s.stop_loss_pct / 100))
                if kind != "rebound" and b.low <= stop:
                    close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")

        # 3) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class BreakoutSettings:
    """돌파 매매: 종가 기준 N일 신고가에 매수, M일 신저가에 매도 (체결은 신호 당일 종가 무렵)."""
    entry_days: int = 20
    exit_days: int = 10
    stop_loss_pct: float = 0.0  # 0 이면 손절 없음
    take_profit_pct: float = 0.0  # 0 이면 익절 없음. 익절가 지정가 매도 (갭상승이면 시가)
    exit_on_low: bool = True  # exit_days 일 신저가 종가에 매도할지
    max_hold_days: int = 0  # 0 이면 제한 없음. N거래일째 종가에 매도
    # 상한가(전일 대비 +29.5% 이상)로 마감한 종목은 매수 대기 물량이 쌓여 종가 체결이 사실상 불가능하므로 제외
    skip_limit_up: bool = True
    # N > 0 이면 직전 N거래일 동안 entry_days 신고가가 한 번도 없었던 '첫 신고가'만 매수
    first_in_days: int = 0
    # 신호 당일 거래대금(종가 x 거래량) 하한 (0 이면 없음)
    min_day_amount: float = 0.0
    # N > 0 이면 신고가 신호 N거래일 뒤 종가에 매수 (그날 종가가 예산 이하이고 상한가 마감이 아닐 때)
    entry_delay: int = 0
    # 대기 기간(신호 다음 날 ~ 매수일) 조건. 0/False 면 사용 안 함
    delay_max_rise_pct: float = 0.0  # 신고가 날 종가 대비 이 % 이상 오른 적 없어야 매수
    delay_hold_signal_open: bool = False  # 신고가 날 시가 아래로 내려간 적 없어야 매수
    delay_intraday: bool = False  # True 면 위 두 조건을 고가·저가로, False 면 종가로 판정
    # N > 0 이면 '직전 N거래일 동안 신고가가 없었던' 첫 신고가만 매수 (예: 20 = 한 달 이내 첫 신고가)
    first_in_days: int = 0
    min_avg_trading_amount: float = 3_000_000_000
    rank_by: str = "amount"  # 신호가 많을 때 우선순위: amount(당일 거래대금) / change(당일 상승률) / strength(신고가 돌파폭)


def _new_high_flags(closes: list[float], n: int) -> list[bool]:
    """각 날의 종가가 직전 n거래일 종가 최고값보다 높은지 (슬라이딩 최대값, O(len))."""
    from collections import deque

    flags = [False] * len(closes)
    window: deque[int] = deque()  # 직전 n일 인덱스, 종가 내림차순 유지
    for i, c in enumerate(closes):
        while window and window[0] < i - n:
            window.popleft()
        if i >= n:
            flags[i] = c > closes[window[0]]
        while window and closes[window[-1]] <= c:
            window.pop()
        window.append(i)
    return flags


def run_breakout(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    b: BreakoutSettings | None = None,
) -> Result:
    """종가 기준 entry_days 일 신고가 매수 / exit_days 일 신저가 매도.

    - 매수 신호: 오늘 종가 > 직전 entry_days 거래일 종가의 최고값 → 오늘 종가에 매수
    - 매도 신호: 오늘 종가 < 직전 exit_days 거래일 종가의 최저값 → 오늘 종가에 매도
    - stop_loss_pct > 0 이면 장중 저가가 손절가 이하일 때 손절가(갭이면 시가)에 매도
    - 종목당 slot_budget, 최대 num_slots 종목. 신호가 많으면 rank_by 순서
    """
    s = s or BacktestSettings()
    b = b or BreakoutSettings()
    pending: list[tuple[str, int, float, date]] = []  # (종목, 매수할 봉 인덱스, 우선순위, 신호일)
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})

    # 종목별로 각 날이 종가 기준 entry_days 일 신고가였는지 미리 계산
    is_high: dict[str, list[bool]] = {sym: _new_high_flags([x.close for x in ser.bars], b.entry_days)
                                      for sym, ser in series.items()}

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]

    for day in calendar:
        # 1) 매도: 손절 → 신저가
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:
                continue
            bar = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - b.stop_loss_pct / 100)) if b.stop_loss_pct > 0 else None
            tp = round_up_to_tick(t.entry_price * (1 + b.take_profit_pct / 100)) if b.take_profit_pct > 0 else None
            # 같은 날 손절가·익절가 모두 닿으면 손절로 가정 (보수적)
            if stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif b.exit_on_low and i >= b.exit_days and bar.close < min(x.close for x in ser.bars[i - b.exit_days:i]):
                close_position(t, day, bar.close * (1 - s.slippage), f"LOW_{b.exit_days}D")
            elif b.max_hold_days and t.hold_days >= b.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 매수: 종가 신고가
        slots = s.num_slots - len(positions)
        if slots > 0 or b.entry_delay:
            signals = []
            for sym, ser in series.items():
                if sym in positions:
                    continue
                i = ser.index.get(day)
                if i is None or i < max(b.entry_days, 20):
                    continue
                if not is_high[sym][i]:
                    continue
                if b.first_in_days and (i < b.first_in_days or any(is_high[sym][i - b.first_in_days:i])):
                    continue  # 최근 N거래일 안에 이미 신고가가 있었음 → 첫 신고가 아님
                bar = ser.bars[i]
                prev_high = max(x.close for x in ser.bars[i - b.entry_days:i])
                if bar.close > budget:
                    continue
                if b.skip_limit_up and bar.close >= ser.bars[i - 1].close * 1.295:
                    continue
                if b.min_day_amount and bar.close * bar.volume < b.min_day_amount:
                    continue
                if b.first_in_days:
                    if i < b.entry_days + b.first_in_days:
                        continue
                    closes = [x.close for x in ser.bars[i - b.entry_days - b.first_in_days:i]]
                    earlier_high = any(
                        closes[j] > max(closes[j - b.entry_days:j])
                        for j in range(b.entry_days, len(closes))
                    )
                    if earlier_high:
                        continue
                avg_amount = sum(x.close * x.volume for x in ser.bars[i - 20:i]) / 20
                if avg_amount < b.min_avg_trading_amount:
                    continue
                if b.rank_by == "amount":
                    key = bar.close * bar.volume
                elif b.rank_by == "change":
                    key = bar.close / ser.bars[i - 1].close  # 당일 상승률
                else:
                    key = bar.close / prev_high
                if b.entry_delay:
                    pending.append((sym, i + b.entry_delay, key, day))
                else:
                    signals.append((key, sym, bar, day))
            if b.entry_delay:
                # 오늘이 (신호일 + N거래일)인 대기 신호를 오늘 종가에 매수
                keep = []
                for sym, target, key, sig_day in pending:
                    ser = series[sym]
                    i = ser.index.get(day)
                    if i is None or i < target:
                        keep.append((sym, target, key, sig_day))  # 아직 매수일 전 (또는 오늘 거래 없음)
                        continue
                    if i > target or sym in positions:
                        continue  # 거래정지 등으로 매수일을 지나쳤거나 이미 보유
                    bar = ser.bars[i]
                    if bar.close > budget or bar.close >= ser.bars[i - 1].close * 1.295:
                        continue
                    sig = ser.bars[ser.index[sig_day]]
                    waiting = ser.bars[ser.index[sig_day] + 1:i + 1]
                    hi = max((x.high if b.delay_intraday else x.close) for x in waiting)
                    lo = min((x.low if b.delay_intraday else x.close) for x in waiting)
                    if b.delay_max_rise_pct and hi >= sig.close * (1 + b.delay_max_rise_pct / 100):
                        continue
                    if b.delay_hold_signal_open and lo < sig.open:
                        continue
                    signals.append((key, sym, bar, sig_day))
                pending[:] = keep
            if s.entry_dates is not None and day not in s.entry_dates:
                signals = []  # 시장 필터: 오늘은 신규 매수 안 함 (3일 뒤 매수 대기 신호는 그대로 소멸)
            signals.sort(key=lambda x: x[0], reverse=True)
            for _, sym, bar, sig_day in signals[:max(slots, 0)]:
                price = bar.close * (1 + s.slippage)
                qty = int(budget // round_up_to_tick(bar.close * (1 + BUY_LIMIT_SLIPPAGE)))
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, series[sym].name, day, price, qty, surge_date=sig_day)
                positions[sym] = t
                trades.append(t)

        # 3) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class LimitUpSettings:
    """상한가 다음 날 시초가 매수: 전일 상한가 마감 종목을 09:00 시장가(시가)로 매수."""
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 15.0
    max_hold_days: int = 0  # 0 이면 제한 없음. N거래일째 종가에 매도 (1 이면 매수 당일 종가)
    min_avg_trading_amount: float = 3_000_000_000  # 0 이면 필터 없음
    limit_up_ratio: float = 1.295  # 전일 대비 이 비율 이상으로 마감하면 상한가로 간주


def run_limit_up_next_open(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    L: LimitUpSettings | None = None,
) -> Result:
    """전일 상한가 마감 종목을 오늘 시가에 매수 → 손절/익절(장중, 매수 당일 포함).

    - 시가가 다시 상한가(전일 종가 x limit_up_ratio 이상)면 시장가로도 체결이 안 되므로 제외
    - 매수 당일 장중: 저가 <= 손절가 → 손절, 고가 >= 익절가 → 익절 (둘 다면 손절로 가정)
    - 신호가 많으면 전일(상한가 날) 거래대금 큰 순서
    """
    s = s or BacktestSettings()
    L = L or LimitUpSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]

    def check_exit(t: Trade, day: date, bar: Bar, entry_day: bool) -> None:
        stop = round_down_to_tick(t.entry_price * (1 - L.stop_loss_pct / 100))
        tp = round_up_to_tick(t.entry_price * (1 + L.take_profit_pct / 100))
        if not entry_day and bar.open <= stop:
            close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
        elif not entry_day and bar.open >= tp:
            close_position(t, day, bar.open, "TAKE_PROFIT")
        elif bar.low <= stop:
            close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
        elif bar.high >= tp:
            close_position(t, day, tp, "TAKE_PROFIT")
        elif L.max_hold_days and t.hold_days + 1 >= L.max_hold_days:
            close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

    for day in calendar:
        # 1) 기존 보유 종목
        for sym, t in list(positions.items()):
            i = series[sym].index.get(day)
            if i is None:
                t.hold_days += 1
                continue
            check_exit(t, day, series[sym].bars[i], entry_day=False)
            if sym in positions:
                t.hold_days += 1

        # 2) 전일 상한가 종목 시가 매수
        slots = s.num_slots - len(positions)
        if slots > 0:
            signals = []
            for sym, ser in series.items():
                if sym in positions:
                    continue
                i = ser.index.get(day)
                if i is None or i < 22:
                    continue
                y, yy, bar = ser.bars[i - 1], ser.bars[i - 2], ser.bars[i]
                if (ser.days[i] - ser.days[i - 1]).days > 7:  # 거래정지 후 재개 등은 제외
                    continue
                if y.close < yy.close * L.limit_up_ratio:
                    continue  # 전일 상한가 아님
                if bar.open >= y.close * L.limit_up_ratio or bar.open > budget:
                    continue  # 시초가 상한가(매수 불가) 또는 예산 초과
                if L.min_avg_trading_amount:
                    avg_amount = sum(x.close * x.volume for x in ser.bars[i - 21:i - 1]) / 20
                    if avg_amount < L.min_avg_trading_amount:
                        continue
                signals.append((y.close * y.volume, sym, bar))
            signals.sort(key=lambda x: x[0], reverse=True)
            for _, sym, bar in signals[:slots]:
                price = bar.open * (1 + s.slippage)
                qty = int(budget // round_up_to_tick(bar.open * (1 + BUY_LIMIT_SLIPPAGE)))
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, series[sym].name, day, price, qty, surge_date=series[sym].days[series[sym].index[day] - 1])
                positions[sym] = t
                trades.append(t)
                check_exit(t, day, bar, entry_day=True)

        # 3) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class SurgeDojiSettings:
    """거래대금 상위 급등주가 거래량 급감 + 단봉 음봉으로 쉬어 갈 때 종가 매수."""
    surge_pct: float = 15.0  # 급등일 상승률(종가 기준) 하한
    top_n: int = 100  # 급등일 시장 전체 거래대금 순위 상한
    max_days_after: int = 5  # 급등 후 N거래일 안에서만 매수
    volume_ratio: float = 0.5  # 매수일 거래량 <= 급등일 거래량 x N
    max_body_pct: float = 2.0  # 단봉: |종가-시가| / 시가 <= N%
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 15.0
    max_hold_days: int = 10
    cooldown_days: int = 5


def trading_value_ranks(series: dict, calendar: list[date]) -> dict[date, dict[str, int]]:
    """날짜별 거래대금(종가 x 거래량) 순위 {day: {symbol: rank}} (1 = 최대)."""
    by_day: dict[date, list[tuple[float, str]]] = defaultdict(list)
    wanted = set(calendar)
    for sym, ser in series.items():
        for b in ser.bars:
            if b.day in wanted:
                by_day[b.day].append((b.close * b.volume, sym))
    out = {}
    for d, rows in by_day.items():
        rows.sort(reverse=True)
        out[d] = {sym: r for r, (_, sym) in enumerate(rows, 1)}
    return out


def run_surge_doji(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    c: SurgeDojiSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    c = c or SurgeDojiSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start - timedelta(days=20) <= d <= end})
    ranks = trading_value_ranks(series, calendar)
    calendar = [d for d in calendar if d >= start]

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    cooldown_until: dict[str, date] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]
        cooldown_until[t.symbol] = day + timedelta(days=c.cooldown_days)

    for day in calendar:
        # 1) 청산 (종가 매수이므로 매수 다음 날부터)
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:
                continue
            bar = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - c.stop_loss_pct / 100))
            tp = round_up_to_tick(t.entry_price * (1 + c.take_profit_pct / 100))
            if bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif c.max_hold_days and t.hold_days >= c.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 매수 신호 (오늘 종가)
        slots = s.num_slots - len(positions)
        if s.entry_dates is not None and day not in s.entry_dates:
            slots = 0
        if slots > 0:
            signals = []
            for sym, ser in series.items():
                if sym in positions or cooldown_until.get(sym, date.min) > day:
                    continue
                i = ser.index.get(day)
                if i is None or i < 2:
                    continue
                bar = ser.bars[i]
                if not (bar.close < bar.open and (bar.open - bar.close) / bar.open * 100 <= c.max_body_pct):
                    continue  # 단봉 음봉 아님
                if bar.close > budget:
                    continue
                # 최근 N거래일 안의 급등일 (가장 최근 것)
                for k in range(i - 1, max(0, i - c.max_days_after) - 1, -1):
                    sb, pb = ser.bars[k], ser.bars[k - 1] if k > 0 else None
                    if pb is None or sb.close / pb.close - 1 < c.surge_pct / 100:
                        continue
                    if ranks.get(sb.day, {}).get(sym, 10**9) > c.top_n:
                        break
                    if bar.volume <= sb.volume * c.volume_ratio:
                        signals.append((sb.close * sb.volume, sym, bar, sb.day))
                    break
            signals.sort(key=lambda x: x[0], reverse=True)
            for _, sym, bar, surge_day in signals[:slots]:
                price = bar.close * (1 + s.slippage)
                qty = int(budget // round_up_to_tick(bar.close * (1 + BUY_LIMIT_SLIPPAGE)))
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, series[sym].name, day, price, qty, surge_date=surge_day)
                positions[sym] = t
                trades.append(t)

        # 3) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


# -------------------------------------------------------------------- data
def load_cache(cache_dir: Path) -> dict[str, tuple[str, list[Bar]]]:
    names = {}
    names_file = cache_dir / "_names.csv"
    if names_file.exists():
        with open(names_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                names[row["code"]] = row["name"]
    data = {}
    for path in cache_dir.glob("*.csv"):
        if path.name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            bars = [
                Bar(date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["volume"]))
                for r in csv.DictReader(f)
            ]
        data[path.stem] = (names.get(path.stem, path.stem), bars)
    return data


def _write_bars(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


def _is_common_stock(code: str, name: str) -> bool:
    # 보통주 코드는 6자리이고 끝자리가 0 (2024년부터 신규 상장은 영문 포함 코드도 있음)
    return (
        len(code) == 6 and code.isalnum() and code.endswith("0")
        and "스팩" not in name and "리츠" not in name
    )


def adjust_splits(rows: list[tuple]) -> list[tuple]:
    """무상증자·액면분할·병합 수정: KRX 기준가(종가 - 전일대비)가 실제 전일 종가와 다르면
    그 비율만큼 이전 봉들의 가격(과 거래량)을 조정한다.

    rows: (date, open, high, low, close, volume, base) 를 날짜순으로.
    반환: (date, open, high, low, close, volume) 수정주가.
    """
    factors = [1.0] * len(rows)
    k = 1.0
    for i in range(len(rows) - 1, 0, -1):
        factors[i] = k
        base, prev_close = rows[i][6], rows[i - 1][4]
        if base > 0 and prev_close > 0 and abs(base / prev_close - 1) > 0.02:
            k *= base / prev_close
    factors[0] = k
    return [
        (d, o * f, h * f, l * f, c * f, v / f)
        for (d, o, h, l, c, v, _), f in zip(rows, factors)
    ]


def import_marcap(data_dir: Path, start: str, end: str | None, cache_dir: Path) -> None:
    """FinanceData/marcap (KRX 전 종목 일별 데이터, 상장폐지 포함) parquet 을 캐시로 변환."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    y1 = date.fromisoformat(end).year if end else date.today().year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, y1 + 1)]
    cols = ["Code", "Name", "Market", "Date", "Open", "High", "Low", "Close", "Changes", "Volume"]
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in files if f.exists()])
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])]
    df = df[(df["Date"] >= start) & (df["Date"] <= (end or "2100-01-01"))]
    df = df.sort_values(["Code", "Date"])

    cache_dir.mkdir(parents=True, exist_ok=True)
    names = {}
    for code, g in df.groupby("Code", sort=False):
        name = str(g["Name"].iloc[-1])
        if not _is_common_stock(code, name):
            continue
        names[code] = name
        rows = [
            (d.date().isoformat(), o, h, l, c, v, c - ch)
            for d, o, h, l, c, ch, v in zip(g["Date"], g["Open"], g["High"], g["Low"], g["Close"], g["Changes"], g["Volume"])
        ]
        adj = adjust_splits(rows)
        # 거래정지일(거래량 0·시가 0)은 제외
        _write_bars(cache_dir / f"{code}.csv", [r for r in adj if r[5] > 0 and r[1] > 0])
    with open(cache_dir / "_names.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name"])
        w.writerows(sorted(names.items()))
    log.info("marcap → 캐시 %d 종목 (%s ~ %s)", len(names), start, end or "최신")


def download(source: str, start: str, end: str | None, cache_dir: Path) -> None:
    """KOSPI·KOSDAQ 보통주 일봉(수정주가)을 캐시에 저장. 이미 받은 종목은 건너뜀."""
    import pandas as pd  # noqa: F401  (pykrx / FinanceDataReader 의존성)

    cache_dir.mkdir(parents=True, exist_ok=True)
    end = end or date.today().isoformat()
    names: dict[str, str] = {}

    if source == "pykrx":
        from pykrx import stock

        # 기간 중 상장됐던 종목(상장폐지 포함)을 모으기 위해 매월 초 종목 목록을 합친다
        month = date.fromisoformat(start).replace(day=1)
        while month <= date.fromisoformat(end):
            d = stock.get_nearest_business_day_in_a_week(month.strftime("%Y%m%d"))
            for market in ("KOSPI", "KOSDAQ"):
                for code in stock.get_market_ticker_list(d, market=market):
                    if code not in names:
                        names[code] = stock.get_market_ticker_name(code)
            month = (month + timedelta(days=32)).replace(day=1)

        def fetch(code):
            df = stock.get_market_ohlcv_by_date(start.replace("-", ""), end.replace("-", ""), code, adjusted=True)
            return [(i.date().isoformat(), r["시가"], r["고가"], r["저가"], r["종가"], r["거래량"]) for i, r in df.iterrows()]
    elif source == "fdr":
        import FinanceDataReader as fdr

        for market in ("KOSPI", "KOSDAQ"):
            listing = fdr.StockListing(market)
            code_col = "Code" if "Code" in listing.columns else "Symbol"
            for _, r in listing.iterrows():
                names[str(r[code_col])] = str(r["Name"])

        def fetch(code):
            df = fdr.DataReader(code, start, end)
            return [(i.date().isoformat(), r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]) for i, r in df.iterrows()]
    else:
        raise ValueError(f"알 수 없는 데이터 소스: {source}")

    names = {c: n for c, n in names.items() if _is_common_stock(c, n)}
    with open(cache_dir / "_names.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name"])
        w.writerows(sorted(names.items()))

    todo = [c for c in sorted(names) if not (cache_dir / f"{c}.csv").exists()]
    log.info("종목 %d개 중 %d개 다운로드 (%s)", len(names), len(todo), source)
    for n, code in enumerate(todo, 1):
        try:
            rows = fetch(code)
        except Exception as exc:
            log.warning("%s 다운로드 실패: %s", code, exc)
            continue
        _write_bars(cache_dir / f"{code}.csv", rows)
        if n % 100 == 0:
            log.info("  %d / %d", n, len(todo))


def download_index(symbol: str, cache_dir: Path, count: int = 1500) -> Path:
    """네이버 차트에서 지수 일봉(KOSPI / KOSDAQ)을 받아 _index_{symbol}.csv 로 저장."""
    import re
    import urllib.request

    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={symbol}&timeframe=day&count={count}&requestType=0"
    text = urllib.request.urlopen(url, timeout=30).read().decode("euc-kr", "ignore")
    rows = []
    for item in re.findall(r'data="([^"]+)"', text):
        d, o, h, l, c, v = item.split("|")
        rows.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(o), float(h), float(l), float(c), float(v)))
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"_index_{symbol}.csv"
    _write_bars(path, rows)
    return path


def load_index(cache_dir: Path, symbol: str = "KOSPI") -> list[Bar]:
    path = cache_dir / f"_index_{symbol}.csv"
    with open(path, encoding="utf-8") as f:
        return [
            Bar(date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]))
            for r in csv.DictReader(f)
        ]


def index_uptrend_days(index: list[Bar], lookback: int = 5) -> set[date]:
    """지수 종가가 lookback 거래일 전 종가보다 높은 날 (직전 N일 상승 흐름)."""
    return {cur.day for prev, cur in zip(index, index[lookback:]) if cur.close > prev.close}


def index_down_days(index: list[Bar], mode: str = "close") -> set[date]:
    """지수 하락일. mode=close: 종가 < 전일 종가, open: 시가 < 전일 종가, both: 둘 다."""
    out = set()
    for prev, cur in zip(index, index[1:]):
        down_close, down_open = cur.close < prev.close, cur.open < prev.close
        if (mode == "close" and down_close) or (mode == "open" and down_open) or (mode == "both" and down_close and down_open):
            out.add(cur.day)
    return out


# --------------------------------------------------------------------- CLI
def _fmt_pct(x: float) -> str:
    return f"{x:+.1%}"


def print_report(results: list[Result]) -> None:
    rows = [r.summary() for r in results]
    header = ["기간", "수익률", "MDD", "거래수", "승률", "평균수익", "평균손실", "손익비(PF)", "평균보유일", "최종자산"]
    print(" | ".join(header))
    for r in rows:
        print(" | ".join([
            r["기간"], _fmt_pct(r["수익률"]), _fmt_pct(r["MDD"]), str(r["거래수"]), f"{r['승률']:.0%}",
            _fmt_pct(r["평균수익"]), _fmt_pct(r["평균손실"]), f"{r['손익비(PF)']:.2f}",
            f"{r['평균보유일']:.1f}", f"{r['최종자산']:,.0f}",
        ]))
    for res, r in zip(results, rows):
        print(f"  {res.start.year} 청산사유: {r['청산사유']}, 기간 말 미청산 {r['미청산']}종목")


def save_trades(results: list[Result], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trades.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["연도", "종목코드", "종목명", "급등일", "매수일", "매수가", "수량", "매도일", "매도가", "사유", "보유일", "손익", "수익률"])
        for res in results:
            for t in res.trades:
                w.writerow([
                    res.start.year, t.symbol, t.name, t.surge_date, t.entry_date, round(t.entry_price),
                    t.qty, t.exit_date or "", round(t.exit_price) if t.exit_price else "", t.reason or "보유중",
                    t.hold_days, round(t.pnl), f"{t.ret:.4f}",
                ])
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tossbot.backtest", description="눌림목 전략 백테스트")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("download", help="과거 일봉 다운로드")
    d.add_argument("--source", choices=["marcap", "pykrx", "fdr"], default="marcap")
    d.add_argument("--marcap-dir", type=Path, default=Path("marcap/data"),
                   help="git clone https://github.com/FinanceData/marcap 한 폴더의 data 경로")
    d.add_argument("--start", default="2023-09-01", help="지표 계산을 위해 백테스트 시작 3~4개월 전부터")
    d.add_argument("--end", default=None)
    d.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    r = sub.add_parser("run", help="연도별 백테스트 실행")
    r.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    r.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    r.add_argument("--out", type=Path, default=Path("backtest_results"))
    r.add_argument("--env", default=".env")
    r.add_argument("--strategy", choices=["pullback", "breakout", "limitup", "surgedoji"], default="pullback",
                   help="pullback: 급등 후 이평선 터치 / breakout: 종가 N일 신고가 매수·M일 신저가 매도 / "
                        "limitup: 전일 상한가 종목 시초가 매수 / "
                        "surgedoji: 거래대금 상위 +15%% 급등 후 거래량 급감 단봉 음봉 종가 매수")
    r.add_argument("--entry-days", type=int, default=20, help="breakout: 매수 신고가 기간")
    r.add_argument("--exit-days", type=int, default=10, help="breakout: 매도 신저가 기간")
    r.add_argument("--stop-loss", type=float, default=0.0, help="breakout: 손절 %% (0 = 없음)")
    r.add_argument("--take-profit", type=float, default=0.0, help="breakout: 익절 %% (0 = 없음)")
    r.add_argument("--kospi-down", choices=["config", "none", "close", "open", "both"], default="config",
                   help="pullback 신규 매수를 코스피 하락일로 제한. close=하락 마감, open=하락 출발, "
                        "config=.env 의 MARKET_FILTER 를 따름(kospi_down 이면 close)")
    r.add_argument("--no-exit-on-low", action="store_true", help="breakout: 신저가 매도 끄기")
    r.add_argument("--max-hold", type=int, default=0, help="breakout: 최대 보유 거래일 (0 = 없음)")
    r.add_argument("--first-in-days", type=int, default=0, help="breakout: 직전 N거래일 안에 신고가가 없던 첫 신고가만")
    r.add_argument("--min-day-amount", type=float, default=0, help="breakout: 신호 당일 거래대금 하한 (원)")
    r.add_argument("--entry-delay", type=int, default=0, help="breakout: 신고가 신호 N거래일 뒤 종가에 매수")
    r.add_argument("--kospi-up-days", type=int, default=0,
                   help="breakout: 코스피 종가가 N거래일 전보다 높은 날에만 매수 (0 = 필터 없음)")
    r.add_argument("--rank-by", choices=["amount", "change", "strength"], default="amount",
                   help="breakout: 신호가 많을 때 우선순위 (거래대금 / 당일 상승률 / 신고가 돌파폭)")
    r.add_argument("--delay-max-rise", type=float, default=0, help="breakout: 대기 중 신고가 종가 대비 N%% 이상 상승 시 매수 취소")
    r.add_argument("--delay-hold-open", action="store_true", help="breakout: 대기 중 신고가 봉 시가 아래로 내려가면 매수 취소")
    r.add_argument("--delay-intraday", action="store_true", help="breakout: 대기 조건을 종가 대신 고가·저가로 판정")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.command == "download":
        if args.source == "marcap":
            import_marcap(args.marcap_dir, args.start, args.end, args.cache)
        else:
            download(args.source, args.start, args.end, args.cache)
        for symbol in ("KOSPI", "KOSDAQ"):
            try:
                download_index(symbol, args.cache)
            except Exception as exc:
                log.warning("%s 지수 다운로드 실패: %s", symbol, exc)
        return

    load_dotenv(args.env)
    cfg = Config.from_env()
    settings = BacktestSettings.from_config(cfg)
    mode = args.kospi_down
    if mode == "config":
        mode = "close" if cfg.market_filter == "kospi_down" else "none"
    if args.strategy == "pullback" and mode != "none":
        settings.entry_dates = index_down_days(load_index(args.cache, "KOSPI"), mode)
        print(f"코스피 하락일({mode})에만 신규 매수")
    data = load_cache(args.cache)
    if not data:
        raise SystemExit(f"{args.cache} 에 데이터가 없습니다. 먼저 download 를 실행하세요.")
    last_day = max(b.day for _, bars in data.values() for b in bars[-1:])
    if args.strategy == "breakout":
        bs = BreakoutSettings(args.entry_days, args.exit_days, args.stop_loss, args.take_profit,
                              exit_on_low=not args.no_exit_on_low, max_hold_days=args.max_hold,
                              first_in_days=args.first_in_days, min_day_amount=args.min_day_amount,
                              entry_delay=args.entry_delay, delay_max_rise_pct=args.delay_max_rise, rank_by=args.rank_by,
                              delay_hold_signal_open=args.delay_hold_open, delay_intraday=args.delay_intraday,
                              min_avg_trading_amount=settings.params.min_avg_trading_amount)
        if args.kospi_up_days:
            settings.entry_dates = index_uptrend_days(load_index(args.cache, "KOSPI"), args.kospi_up_days)
            print(f"코스피가 {args.kospi_up_days}거래일 전보다 높은 날에만 매수")
        runner = lambda y0, y1: run_breakout(data, y0, y1, settings, bs)  # noqa: E731
    elif args.strategy == "surgedoji":
        sd = SurgeDojiSettings(stop_loss_pct=settings.stop_loss_pct, take_profit_pct=settings.take_profit_pct,
                               max_hold_days=settings.max_hold_days, cooldown_days=settings.cooldown_days)
        if mode != "none":
            settings.entry_dates = index_down_days(load_index(args.cache, "KOSPI"), mode)
        runner = lambda y0, y1: run_surge_doji(data, y0, y1, settings, sd)  # noqa: E731
    elif args.strategy == "limitup":
        ls = LimitUpSettings(args.stop_loss or settings.stop_loss_pct, args.take_profit or settings.take_profit_pct,
                             max_hold_days=args.max_hold,
                             min_avg_trading_amount=settings.params.min_avg_trading_amount)
        runner = lambda y0, y1: run_limit_up_next_open(data, y0, y1, settings, ls)  # noqa: E731
    else:
        runner = lambda y0, y1: run_backtest(data, y0, y1, settings)  # noqa: E731
    results = [runner(date(y, 1, 1), min(date(y, 12, 31), last_day)) for y in args.years]
    print(f"종목 {len(data)}개, 연도마다 {settings.initial_cash:,.0f}원으로 새로 시작\n")
    print_report(results)
    print(f"\n거래 내역: {save_trades(results, args.out)}")


if __name__ == "__main__":
    main()
