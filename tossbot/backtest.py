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

import dataclasses

import argparse
import bisect
import csv
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .breakout import new_high_flags
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
            if closes[i] / closes[i - 1] - 1 >= p.surge_pct / 100 and closes[i] * ser.bars[i].volume >= p.min_surge_amount:
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
    # 매수일 봉 조건 (entry_delay 와 함께): 시가 갭상승 / 양봉 / 윗꼬리 >= 몸통 x N (0 = 조건 없음)
    # 이평선 배열: 위에서부터 순서대로 (예: (60, 5, 20) = 60일선 > 5일선 > 20일선). 신호일 종가 포함 계산
    ma_order: tuple[int, ...] = ()
    buy_day_gap_up: bool = False
    # entry_delay 와 함께: True 면 매수일 종가 대신 시가에 매수 (당일 장중 손절·익절도 적용, 같은 날 둘 다 닿으면 손절)
    buy_at_open: bool = False
    buy_day_bullish: bool = False
    buy_day_min_wick_ratio: float = 0.0
    # N > 0 이면 '직전 N거래일 동안 신고가가 없었던' 첫 신고가만 매수 (예: 20 = 한 달 이내 첫 신고가)
    first_in_days: int = 0
    min_avg_trading_amount: float = 3_000_000_000
    # 신고가 날 봉 길이 하한 (%). body: (종가-시가)/시가 양봉 몸통, range: (고가-저가)/저가
    min_candle_pct: float = 0.0
    candle_measure: str = "body"
    # 0 보다 크면 장중 고가가 매수가 대비 이 % 이상 오른 다음 날부터 손절가를 매수가(본전)로 올림
    # 매수 다음 거래일 매도 규칙: "" 없음 / open 시가 / close 종가 / open_if_loss·close_if_loss 매수가 아래일 때만 / open_if_no_gap 시가가 매수일 종가 이하면
    next_day_exit: str = ""
    max_buys_per_day: int = 0  # 0 보다 크면 하루 신규 매수 종목 수 상한
    min_marcap: float = 0.0  # 0 보다 크면 신호일 시가총액 하한 (원). marcap 이 필요
    marcap: dict | None = None  # {종목코드: {날짜: 시가총액}}
    skip_touched_limit_up: bool = False  # True 면 장중 상한가를 찍고 내려온 종목도 제외
    max_drop_from_high_pct: float = 0.0  # 0 보다 크면 종가가 당일 고가 대비 이 % 이상 내려온 종목 제외
    exclude_marcap: tuple[float, float] | None = None  # (lo, hi): 시가총액이 lo 초과 hi 미만이면 제외
    # N > 0 이면 종가가 N일 이동평균선 아래로 마감한 날 종가에 매도
    ma_exit_days: int = 0
    ma_exit_profit_only: bool = False  # True 면 종가가 매수가보다 높을 때만 이평선 이탈 매도 (익절 전용)
    breakeven_trigger_pct: float = 0.0
    # 0 보다 크면 저가가 매수가 대비 이 % 이상 빠진 다음 날부터 매수가(본전)에 지정가 매도 대기
    recover_after_drop_pct: float = 0.0
    # True 면 손절을 시장가 대신 손절가 지정가로: 갭하락으로 시가가 손절가 아래면 손절가를 회복할 때까지 체결 안 됨
    stop_limit: bool = False
    # N > 0 이면 N일 신고가(종가 기준)이기도 한 종목은 제외 (예: 200 = 200일 신고가 제외). 이력이 N일 미만이면 제외
    exclude_high_days: int = 0
    # 이평선 저항: 종가가 N일선 아래이면서 N일선까지 남은 거리가 pct % 미만이면 매수 금지
    ma_resist_days: int = 0
    ma_resist_pct: float = 0.0
    # N일 최저가(장중 저가) 대비 종가 상승률이 pct % 초과면 매수 금지
    low_rise_days: int = 0
    max_rise_from_low_pct: float = 0.0
    # 0 보다 크면 신고가 돌파폭(종가 / 직전 entry_days 일 최고값 - 1)이 이 % 이하인 종목만
    max_breakout_pct: float = 0.0
    breakout_basis: str = "close"  # 직전 최고값 기준: close = 종가 최고값 / high = 장중 고가 최고값
    # 0 보다 크면 종목당 매수 금액 = 현재 평가금액 x 이 % (복리). 0 이면 slot_budget 고정. 1주 가격 상한은 slot_budget 그대로
    position_pct: float = 0.0
    # True 면 계단식: 종목당 금액 = max(slot_budget, 평가금액 10만원 단위 내림 x 10%). 110만원 → 11만원, 120만원 → 12만원
    step_sizing: bool = False
    # position_pct 와 함께: N > 0 이면 최소 N주를 사되 종목당 평가금액의 max_position_pct % 까지.
    # 1주 가격 상한도 평가금액 x max_position_pct / N 으로 바뀜 (예: 2주, 20% → 1주 가격 <= 평가금액의 10%)
    min_shares: int = 0
    max_position_pct: float = 0.0
    # 최근 winrate_window 건 청산의 승률(수익 청산 비율)이 winrate_cut % 이하면 최대 보유 종목 수를 reduced_slots 로
    winrate_cut: float = 0.0
    winrate_window: int = 20
    # 0 보다 크면 승률 대비 비중(켈리): 종목당 금액 = 평가금액 x 켈리비율 x kelly_scale, kelly_min_pct ~ 이 % 로 제한.
    # 켈리비율 = 승률 - (1 - 승률) / (평균수익 / 평균손실), 최근 winrate_window 건 청산 기준.
    # 청산이 winrate_window 건 모이기 전에는 기본 종목당 금액. 현금이 모자라면 남은 현금만큼만 매수
    kelly_max_pct: float = 0.0
    kelly_min_pct: float = 5.0
    kelly_scale: float = 1.0
    reduced_slots: int = 8
    exclude_symbols: frozenset = frozenset()  # 매수 제외 종목코드 (예: 제약·바이오)
    breakeven_lock_pct: float = 0.0  # 올린 손절가 = 매수가 x (1 + 이 %). 0 이면 본전
    # False 면 신고가 조건 없이 매수 (급등주 매매: min_change_pct 와 함께 사용)
    require_new_high: bool = True
    # 0 보다 크면 당일 상승률(전일 종가 대비)이 이 % 이상인 종목만
    min_change_pct: float = 0.0
    # 0 보다 크면 1주 가격 상한 (원). 매수 금액은 종목당 예산 그대로
    max_price: float = 0.0
    min_price: float = 0.0  # 0 보다 크면 1주 가격 하한 (원)
    # 0 보다 크면 신호 당일 거래대금 상한 (원)
    max_day_amount: float = 0.0
    # 0 보다 크면 신고가 날 시가 갭(시가 / 전일 종가 - 1)이 이 % 이하인 종목만 매수
    max_gap_pct: float = 0.0
    # 신호가 많을 때 우선순위: amount(당일 거래대금) / change(당일 상승률) / strength(신고가 돌파폭) 큰 순,
    # weak(돌파폭 작은 순) / calm(당일 상승률 작은 순)
    rank_by: str = "amount"
    # N > 0 이면 신호 당일 거래대금 순위(전체 종목 중) N위 이내 종목만
    max_amount_rank: int = 0


def kelly_fraction(rets: list[float]) -> float:
    """승률 - (1 - 승률) / 손익비. 이긴 적이 없으면 0, 진 적이 없으면 1."""
    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r <= 0]
    if not wins:
        return 0.0
    if not losses or sum(losses) == 0:
        return 1.0
    w = len(wins) / len(rets)
    payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    return w - (1 - w) / payoff


def kelly_budget(rets: list[float], cash: float, positions: dict, last_close: dict, b: "BreakoutSettings") -> float:
    equity = cash + sum(p.qty * last_close.get(ps, p.entry_price) for ps, p in positions.items())
    pct = min(max(kelly_fraction(rets) * b.kelly_scale * 100, b.kelly_min_pct), b.kelly_max_pct)
    return equity * pct / 100


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
    is_high: dict[str, list[bool]] = {sym: new_high_flags([x.close for x in ser.bars], b.entry_days)
                                      for sym, ser in series.items()}

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}
    breakeven: set[str] = set()  # 손절가를 본전으로 올린 종목
    recover: set[str] = set()  # 크게 빠져서 본전 탈출을 기다리는 종목
    closed_wins: list[bool] = []  # 청산 순서대로 수익 여부 (최근 승률 계산용)
    closed_rets: list[float] = []  # 청산 순서대로 수익률 (켈리 비중용)
    amount_ranks = trading_value_ranks(series, calendar) if b.max_amount_rank else {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        closed_wins.append(t.pnl > 0)
        closed_rets.append(t.ret)
        del positions[t.symbol]
        breakeven.discard(t.symbol)
        recover.discard(t.symbol)

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
            nd = b.next_day_exit if t.hold_days == 1 else ""
            no_gap = nd == "open_if_no_gap" and i > 0 and bar.open <= ser.bars[i - 1].close  # 매수일 종가 이하 출발
            if nd == "open" or (nd == "open_if_loss" and bar.open < t.entry_price) or no_gap:
                close_position(t, day, bar.open * (1 - s.slippage), "NEXT_OPEN")
                continue
            if sym in recover:
                be = round_up_to_tick(t.entry_price)
                if bar.open >= be:
                    close_position(t, day, bar.open, "RECOVER")
                    continue
                if bar.high >= be:
                    close_position(t, day, be, "RECOVER")
                    continue
            reason = "STOP_LOSS"
            if sym in breakeven:
                stop, reason = round_down_to_tick(t.entry_price * (1 + b.breakeven_lock_pct / 100)), "BREAKEVEN"
            # 같은 날 손절가·익절가 모두 닿으면 손절로 가정 (보수적)
            if stop and b.stop_limit and bar.open < stop:
                if bar.high >= stop:
                    close_position(t, day, stop, reason + "_LIMIT")  # 갭하락 후 손절가까지 회복해 지정가 체결
                continue  # 미체결: 지정가 매도 대기 (익절 판단 없음)
            if stop and b.stop_limit and bar.low <= stop:
                close_position(t, day, stop, reason + "_LIMIT")  # 장중 손절가 도달 → 지정가 체결 (슬리피지 없음)
            elif stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), reason)
            elif tp and bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), reason)
            elif tp and bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif b.exit_on_low and i >= b.exit_days and bar.close < min(x.close for x in ser.bars[i - b.exit_days:i]):
                close_position(t, day, bar.close * (1 - s.slippage), f"LOW_{b.exit_days}D")
            elif b.max_hold_days and t.hold_days >= b.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")
            elif (b.ma_exit_days and i >= b.ma_exit_days - 1
                  and bar.close < sum(x.close for x in ser.bars[i - b.ma_exit_days + 1:i + 1]) / b.ma_exit_days
                  and (not b.ma_exit_profit_only or bar.close > t.entry_price)):
                close_position(t, day, bar.close * (1 - s.slippage), f"MA{b.ma_exit_days}_EXIT")
            elif nd == "close" or (nd == "close_if_loss" and bar.close < t.entry_price):
                close_position(t, day, bar.close * (1 - s.slippage), "NEXT_CLOSE")
            if (b.recover_after_drop_pct and sym in positions
                    and bar.low <= t.entry_price * (1 - b.recover_after_drop_pct / 100)):
                recover.add(sym)  # 다음 날부터 본전 지정가 매도
            if (b.breakeven_trigger_pct and sym in positions
                    and bar.high >= t.entry_price * (1 + b.breakeven_trigger_pct / 100)):
                breakeven.add(sym)  # 장중 순서를 알 수 없으므로 다음 날부터 적용

        # 2) 매수: 종가 신고가
        max_slots = s.num_slots
        if b.winrate_cut and len(closed_wins) >= b.winrate_window:
            recent = closed_wins[-b.winrate_window:]
            if sum(recent) / len(recent) * 100 <= b.winrate_cut:
                max_slots = min(max_slots, b.reduced_slots)  # 최근 승률이 낮으면 보유 종목 수 축소
        slots = max_slots - len(positions)
        price_cap = budget
        if b.min_shares and b.position_pct:
            eq = cash + sum(p.qty * last_close.get(ps, p.entry_price) for ps, p in positions.items())
            price_cap = eq * b.max_position_pct / 100 / b.min_shares
        if slots > 0 or b.entry_delay:
            signals = []
            for sym, ser in series.items():
                if sym in positions or sym in b.exclude_symbols:
                    continue
                i = ser.index.get(day)
                if i is None or i < max(b.entry_days, 20):
                    continue
                if b.require_new_high and not is_high[sym][i]:
                    continue
                if b.min_change_pct and (ser.bars[i].close / ser.bars[i - 1].close - 1) * 100 < b.min_change_pct - 1e-9:
                    continue
                if b.max_amount_rank and amount_ranks.get(day, {}).get(sym, 10**9) > b.max_amount_rank:
                    continue
                if b.first_in_days and (i < b.first_in_days or any(is_high[sym][i - b.first_in_days:i])):
                    continue  # 최근 N거래일 안에 이미 신고가가 있었음 → 첫 신고가 아님
                bar = ser.bars[i]
                prev_high = max(x.close for x in ser.bars[i - b.entry_days:i])
                if bar.close > price_cap or (b.max_price and bar.close > b.max_price) or bar.close < b.min_price:
                    continue
                # 대기 후 매수(entry_delay)면 신호일 상한가도 매수 가능하므로 skip_limit_up 을 끌 수 있다
                if b.skip_limit_up and bar.close >= ser.bars[i - 1].close * 1.295:
                    continue
                if b.min_day_amount and bar.close * bar.volume < b.min_day_amount:
                    continue
                if b.skip_touched_limit_up and bar.high >= ser.bars[i - 1].close * 1.295:
                    continue
                if b.ma_resist_days:
                    if i + 1 < b.ma_resist_days:
                        continue
                    ma = sum(x.close for x in ser.bars[i + 1 - b.ma_resist_days:i + 1]) / b.ma_resist_days
                    if bar.close < ma < bar.close * (1 + b.ma_resist_pct / 100):
                        continue  # 바로 위에 이평선 저항
                if b.max_breakout_pct:
                    ref = max((x.high if b.breakout_basis == "high" else x.close) for x in ser.bars[i - b.entry_days:i])
                    if bar.close > ref * (1 + b.max_breakout_pct / 100) + 1e-9:
                        continue
                if b.low_rise_days:
                    if i + 1 < b.low_rise_days:
                        continue
                    low = min(x.low for x in ser.bars[i + 1 - b.low_rise_days:i + 1])
                    if bar.close > low * (1 + b.max_rise_from_low_pct / 100):
                        continue
                if b.exclude_high_days and (i < b.exclude_high_days
                                            or bar.close > max(x.close for x in ser.bars[i - b.exclude_high_days:i])):
                    continue
                if b.ma_order:
                    if i + 1 < max(b.ma_order):
                        continue
                    mas = [sum(x.close for x in ser.bars[i + 1 - n:i + 1]) / n for n in b.ma_order]
                    if any(mas[k] <= mas[k + 1] for k in range(len(mas) - 1)):
                        continue
                if b.max_drop_from_high_pct and bar.close <= bar.high * (1 - b.max_drop_from_high_pct / 100) + 1e-9:
                    continue
                if b.min_marcap and (b.marcap or {}).get(sym, {}).get(day, 0) < b.min_marcap:
                    continue
                if b.exclude_marcap and b.exclude_marcap[0] < (b.marcap or {}).get(sym, {}).get(day, 0) < b.exclude_marcap[1]:
                    continue
                if b.max_day_amount and bar.close * bar.volume > b.max_day_amount:
                    continue
                if b.max_gap_pct and (bar.open / ser.bars[i - 1].close - 1) * 100 > b.max_gap_pct + 1e-9:
                    continue
                if b.min_candle_pct:
                    size = (bar.close / bar.open - 1) if b.candle_measure == "body" else (bar.high / bar.low - 1)
                    if size * 100 < b.min_candle_pct:
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
                elif b.rank_by == "calm":
                    key = -bar.close / ser.bars[i - 1].close  # 당일 상승률 작은 순
                elif b.rank_by == "weak":
                    key = -bar.close / prev_high  # 신고가를 살짝 넘은 순
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
                    px = bar.open if b.buy_at_open else bar.close
                    if (px > budget or (b.max_price and px > b.max_price) or px < b.min_price
                            or px >= ser.bars[i - 1].close * 1.295):
                        continue
                    sig = ser.bars[ser.index[sig_day]]
                    waiting = ser.bars[ser.index[sig_day] + 1:i + 1]
                    hi = max((x.high if b.delay_intraday else x.close) for x in waiting)
                    lo = min((x.low if b.delay_intraday else x.close) for x in waiting)
                    if b.delay_max_rise_pct and hi >= sig.close * (1 + b.delay_max_rise_pct / 100):
                        continue
                    if b.delay_hold_signal_open and lo < sig.open:
                        continue
                    if b.buy_day_gap_up and bar.open <= ser.bars[i - 1].close:
                        continue
                    if b.buy_day_bullish and bar.close <= bar.open:
                        continue
                    if b.buy_day_min_wick_ratio and (bar.high - bar.close) < (bar.close - bar.open) * b.buy_day_min_wick_ratio:
                        continue
                    signals.append((key, sym, bar, sig_day))
                pending[:] = keep
            if s.entry_dates is not None and day not in s.entry_dates:
                signals = []  # 시장 필터: 오늘은 신규 매수 안 함 (3일 뒤 매수 대기 신호는 그대로 소멸)
            signals.sort(key=lambda x: x[0], reverse=True)
            n_buy = min(max(slots, 0), b.max_buys_per_day) if b.max_buys_per_day else max(slots, 0)
            for _, sym, bar, sig_day in signals[:n_buy]:
                at_open = b.buy_at_open and b.entry_delay
                px = bar.open if at_open else bar.close
                price = px * (1 + s.slippage)
                pos_budget = budget
                if b.position_pct or b.step_sizing:
                    equity_now = cash + sum(p.qty * last_close.get(ps, p.entry_price) for ps, p in positions.items())
                    if b.step_sizing:
                        pos_budget = max(budget, (equity_now // 100_000) * 10_000)
                    else:
                        pos_budget = equity_now * b.position_pct / 100
                if b.kelly_max_pct and len(closed_rets) >= b.winrate_window:
                    pos_budget = kelly_budget(closed_rets[-b.winrate_window:], cash, positions, last_close, b)
                unit = round_up_to_tick(px * (1 + BUY_LIMIT_SLIPPAGE))
                qty = int(pos_budget // unit)
                if b.kelly_max_pct:
                    qty = min(qty, int(cash // (price * (1 + s.commission))))
                if (b.min_shares and b.position_pct and qty < b.min_shares
                        and unit * b.min_shares <= equity_now * b.max_position_pct / 100):
                    qty = b.min_shares
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, series[sym].name, day, price, qty, surge_date=sig_day)
                positions[sym] = t
                trades.append(t)
                if at_open:  # 시가 매수 → 당일 남은 장중에 손절·익절 (둘 다 닿으면 손절로 가정)
                    stop = round_down_to_tick(price * (1 - b.stop_loss_pct / 100)) if b.stop_loss_pct > 0 else None
                    tp = round_up_to_tick(price * (1 + b.take_profit_pct / 100)) if b.take_profit_pct > 0 else None
                    if stop and bar.low <= stop:
                        close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
                    elif tp and bar.high >= tp:
                        close_position(t, day, tp, "TAKE_PROFIT")

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


@dataclass
class RetestSettings:
    """기준봉(급등 + N일 신고가) 이후 이전 고점까지 되돌릴 때 지정가 매수."""
    surge_pct: float = 15.0  # 기준봉 전일 대비 상승률 하한
    surge_max_pct: float = 0.0  # 0 보다 크면 기준봉 상승률 상한
    min_amount: float = 20_000_000_000  # 기준봉 거래대금 하한
    entry_days: int = 20  # 기준봉 종가가 직전 N거래일 종가 최고값을 넘어야 함 (N일 신고가)
    level: str = "high"  # 매수가: high = 직전 N거래일 장중 고가 최고값, close = 종가 최고값
    first_in_days: int = 0  # N > 0 이면 직전 N거래일 동안 N일 신고가가 없던 '첫 신고가' 기준봉만
    watch_days: int = 10  # 기준봉 다음 날부터 N거래일 안에 닿아야 매수
    max_break_pct: float = 3.0  # 시가가 매수가보다 이 % 넘게 아래서 시작하면(지지 이탈) 매수 취소
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 20.0
    min_avg_trading_amount: float = 3_000_000_000


def run_retest(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    r: RetestSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    r = r or RetestSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    # 종목별 대기 중인 기준봉: sym -> (기준봉 인덱스, 매수가, 우선순위)
    watch: dict[str, tuple[int, float, float]] = {}
    is_high = {sym: new_high_flags([x.close for x in ser.bars], r.entry_days) for sym, ser in series.items()}

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

    def stop_of(price: float) -> float:
        return round_down_to_tick(price * (1 - r.stop_loss_pct / 100))

    for day in calendar:
        # 1) 청산
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None or t.entry_date == day:
                continue
            bar = ser.bars[i]
            stop, tp = stop_of(t.entry_price), round_up_to_tick(t.entry_price * (1 + r.take_profit_pct / 100))
            if bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")

        # 2) 대기 중인 기준봉이 이전 고점에 닿았으면 매수 (장중 지정가)
        signals = []
        for sym, (k, level, key) in list(watch.items()):
            ser = series[sym]
            i = ser.index.get(day)
            if i is None:
                continue
            if i - k > r.watch_days:
                del watch[sym]
                continue
            bar = ser.bars[i]
            if bar.low > level:
                continue
            del watch[sym]  # 첫 터치에서만 판단
            if bar.open <= level:
                if bar.open < level * (1 - r.max_break_pct / 100):
                    continue  # 지지선 아래로 갭하락 출발
                fill = bar.open
            else:
                fill = level
            if sym not in positions and fill <= budget:
                signals.append((key, sym, fill, bar, ser.bars[k].day))
        slots = s.num_slots - len(positions)
        if s.entry_dates is not None and day not in s.entry_dates:
            slots = 0
        signals.sort(key=lambda x: x[0], reverse=True)
        for _, sym, fill, bar, surge_day in signals[:max(slots, 0)]:
            price = fill * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=surge_day)
            positions[sym] = t
            trades.append(t)
            if bar.low <= stop_of(price):  # 같은 날 손절가까지 밀렸으면 손절로 가정 (보수적)
                close_position(t, day, stop_of(price) * (1 - s.slippage), "STOP_LOSS")

        # 3) 오늘 종가로 새 기준봉 등록
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < max(r.entry_days, 20) + 1 or sym in positions:
                continue
            bar, prev = ser.bars[i], ser.bars[i - 1]
            if bar.close / prev.close - 1 < r.surge_pct / 100 or bar.close * bar.volume < r.min_amount:
                continue
            if r.surge_max_pct and bar.close / prev.close - 1 > r.surge_max_pct / 100:
                continue
            window = ser.bars[i - r.entry_days:i]
            if bar.close <= max(x.close for x in window):
                continue  # N일 신고가 아님
            if r.first_in_days and (i < r.entry_days + r.first_in_days or any(is_high[sym][i - r.first_in_days:i])):
                continue  # 최근 N거래일 안에 이미 신고가가 있었음 → 첫 신고가 아님
            if sum(x.close * x.volume for x in ser.bars[i - 20:i]) / 20 < r.min_avg_trading_amount:
                continue
            level = max((x.high if r.level == "high" else x.close) for x in window)
            if level >= bar.close:
                continue
            watch[sym] = (i, round_down_to_tick(level), bar.close * bar.volume)

        # 4) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class MaPullbackSettings:
    """기준봉(거래대금 + 급등, first_in_days 안에서 첫 번째, 종가가 ma_period 일선 위) 이후
    ma_period 일선까지 눌리면 이평선 가격에 지정가 매수."""
    surge_pct: float = 10.0  # 기준봉 전일 대비 상승률 하한
    min_amount: float = 200_000_000_000  # 기준봉 거래대금 하한
    first_in_days: int = 60  # 직전 N거래일 안에 같은 조건 기준봉이 없던 첫 기준봉만 (0 = 조건 없음)
    ma_period: int = 200
    watch_days: int = 60  # 기준봉 다음 날부터 N거래일 안에 이평선에 닿아야 매수
    max_break_pct: float = 3.0  # 시가가 이평선보다 이 % 넘게 아래서 시작하면 매수 취소
    stop_loss_pct: float = 5.0
    take_profit_pct: float = 20.0
    # 매수 방식: ma = 이평선 눌림 지정가 / bear = 기준봉 뒤 첫 음봉(종가 < 시가) 종가 매수
    entry: str = "ma"
    # bear: 첫 음봉 하락폭이 이 % 이내일 때만 매수 (넘으면 기준봉 폐기). body = 시가 대비, change = 전일 종가 대비
    bear_max_pct: float = 3.0
    bear_measure: str = "body"
    require_above_ma: bool = True  # False 면 기준봉 종가의 이평선 위 조건 없음
    # 하루에 기준봉이 여럿이면 1종목만 감시: "" 모두 / amount 거래대금 1위 / change 상승률 1위 /
    # both 거래대금·상승률 모두 1위인 종목만 (다르면 없음) / combo 두 순위 합이 가장 작은 종목 (같으면 거래대금 큰 쪽)
    pick: str = ""
    ma_exit_days: int = 0  # N > 0 이면 종가가 N일선 아래로 마감한 날 종가 매도 (매수 다음 날부터)


def run_ma_pullback(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    m: MaPullbackSettings | None = None,
) -> Result:
    """매수가 = 전일까지의 ma_period 일 이동평균 (장 시작 전에 알 수 있는 값). 장중 저가가 닿으면 체결
    (시가가 이미 아래면 시가). 매수 당일 저가가 손절가 이하면 손절로 가정."""
    s = s or BacktestSettings()
    m = m or MaPullbackSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    csum: dict[str, list[float]] = {}  # 종가 누적합 (이동평균 계산용)
    surge_flags: dict[str, list[bool]] = {}
    for sym, ser in series.items():
        acc, run = [0.0], 0.0
        for x in ser.bars:
            run += x.close
            acc.append(run)
        csum[sym] = acc
        surge_flags[sym] = [i > 0 and x.close >= ser.bars[i - 1].close * (1 + m.surge_pct / 100)
                            and x.close * x.volume >= m.min_amount for i, x in enumerate(ser.bars)]

    def ma(sym: str, i: int) -> float | None:
        """bars[i - ma_period + 1 .. i] 종가 평균."""
        if i + 1 < m.ma_period:
            return None
        return (csum[sym][i + 1] - csum[sym][i + 1 - m.ma_period]) / m.ma_period

    watch: dict[str, tuple[int, float]] = {}  # sym -> (기준봉 인덱스, 우선순위)
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

    def stop_of(price: float) -> float:
        return round_down_to_tick(price * (1 - m.stop_loss_pct / 100))

    for day in calendar:
        # 1) 청산: 손절 / 익절 (같은 날 둘 다면 손절)
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None or t.entry_date == day:
                continue
            bar = ser.bars[i]
            stop = stop_of(t.entry_price) if m.stop_loss_pct > 0 else None
            tp = round_up_to_tick(t.entry_price * (1 + m.take_profit_pct / 100)) if m.take_profit_pct > 0 else None
            if stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif m.ma_exit_days and i + 1 >= m.ma_exit_days and bar.close < (
                    csum[sym][i + 1] - csum[sym][i + 1 - m.ma_exit_days]) / m.ma_exit_days:
                close_position(t, day, bar.close * (1 - s.slippage), f"MA{m.ma_exit_days}_EXIT")

        # 2) 대기 중인 기준봉이 이평선에 닿으면 매수
        signals = []
        for sym, (k, key) in list(watch.items()):
            ser = series[sym]
            i = ser.index.get(day)
            if i is None:
                continue
            if i - k > m.watch_days:
                del watch[sym]
                continue
            if m.entry == "bear":
                bar, prev = ser.bars[i], ser.bars[i - 1]
                if bar.close >= bar.open:
                    continue  # 음봉 아님
                del watch[sym]  # 첫 음봉에서만 판단
                base = bar.open if m.bear_measure == "body" else prev.close
                if (1 - bar.close / base) * 100 > m.bear_max_pct + 1e-9:
                    continue
                if bar.close >= prev.close * 1.295 or sym in positions or bar.close > budget:
                    continue
                signals.append((key, sym, bar.close, None, ser.bars[k].day))
                continue
            level_raw = ma(sym, i - 1)
            if level_raw is None:
                continue
            level = round_down_to_tick(level_raw)
            bar = ser.bars[i]
            if bar.low > level:
                continue
            del watch[sym]  # 첫 터치에서만 판단
            if bar.open <= level:
                if bar.open < level * (1 - m.max_break_pct / 100):
                    continue  # 이평선 아래로 갭하락 출발
                fill = bar.open
            else:
                fill = level
            if sym not in positions and fill <= budget:
                signals.append((key, sym, fill, bar, ser.bars[k].day))
        slots = s.num_slots - len(positions)
        signals.sort(key=lambda x: x[0], reverse=True)
        for _, sym, fill, bar, surge_day in signals[:max(slots, 0)]:
            price = fill * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=surge_day)
            positions[sym] = t
            trades.append(t)
            if bar is not None and m.stop_loss_pct > 0 and bar.low <= stop_of(price):
                close_position(t, day, stop_of(price) * (1 - s.slippage), "STOP_LOSS")

        # 3) 오늘 종가로 새 기준봉 등록
        new = []
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or sym in positions or not surge_flags[sym][i]:
                continue
            if m.first_in_days and (i < m.first_in_days or any(surge_flags[sym][i - m.first_in_days:i])):
                continue  # 최근 N거래일 안에 이미 기준봉이 있었음
            if m.require_above_ma:
                level = ma(sym, i)
                if level is None or ser.bars[i].close <= level:
                    continue  # 종가가 이평선 위가 아님 (또는 이력 부족)
            bar = ser.bars[i]
            new.append((sym, i, bar.close * bar.volume, bar.close / ser.bars[i - 1].close))
        if m.pick and new:
            by_amt = sorted(new, key=lambda x: -x[2])
            by_chg = sorted(new, key=lambda x: -x[3])
            if m.pick == "amount":
                new = by_amt[:1]
            elif m.pick == "change":
                new = by_chg[:1]
            elif m.pick == "both":
                new = by_amt[:1] if by_amt[0][0] == by_chg[0][0] else []
            else:
                ra = {x[0]: r for r, x in enumerate(by_amt)}
                rc = {x[0]: r for r, x in enumerate(by_chg)}
                new = [min(new, key=lambda x: (ra[x[0]] + rc[x[0]], ra[x[0]]))]
        for sym, i, amt, _ in new:
            watch[sym] = (i, amt)

        # 4) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class EnvelopeSettings:
    """엔벨로프 하단선 근접 종가 매수 → hold_days 거래일 뒤 종가 매도."""
    period: int = 20  # 이동평균 기간
    pct: float = 20.0  # 하단선 = 이동평균 x (1 - pct%)
    near_pct: float = 2.0  # 종가가 하단선의 ±near_pct% 이내 (mode="near") 또는 하단선 x (1 + near_pct%) 이하 (mode="below")
    mode: str = "near"
    hold_days: int = 1  # 매수 후 N거래일째 종가에 매도 (1 = 익일 종가, 0 = 제한 없음)
    position_pct: float = 10.0  # 종목당 평가금액의 N%
    stop_loss_pct: float = 0.0  # 0 이면 없음 (장중 손절가 도달 시 매도)
    take_profit_pct: float = 0.0  # 0 이면 없음 (장중 익절가 도달 시 지정가 매도, 같은 날 둘 다면 손절)
    universe: dict | None = None  # {날짜: 매수 가능 종목코드 집합} (예: 코스피 시가총액 상위 200)
    rank: dict | None = None  # {날짜: {종목코드: 시가총액}} 신호가 많을 때 큰 순


def load_top_universe(data_dir: Path, start: str, end: str | None = None, market: str = "KOSPI",
                      top_n: int = 200) -> tuple[dict, dict]:
    """날짜별 시가총액 상위 top_n 종목 (코스피200 근사). ({날짜: 종목 집합}, {날짜: {종목: 시가총액}})."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    y1 = date.fromisoformat(end).year if end else date.today().year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, y1 + 1)]
    df = pd.concat([pd.read_parquet(f, columns=["Code", "Name", "Market", "Date", "Marcap"]) for f in files if f.exists()])
    df = df[df["Market"] == market]
    df = df[[_is_common_stock(c, n) for c, n in zip(df["Code"], df["Name"])]]
    uni, caps = {}, {}
    for d, g in df.groupby("Date"):
        top = g.nlargest(top_n, "Marcap")
        uni[d.date()] = set(top["Code"])
        caps[d.date()] = dict(zip(top["Code"], top["Marcap"].astype(float)))
    return uni, caps


def run_envelope(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    e: EnvelopeSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    e = e or EnvelopeSettings()
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

    for day in calendar:
        # 1) 매도: 손절(장중) → N거래일째 종가
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            if i is None:
                continue
            t.hold_days += 1
            bar = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - e.stop_loss_pct / 100)) if e.stop_loss_pct else None
            tp = round_up_to_tick(t.entry_price * (1 + e.take_profit_pct / 100)) if e.take_profit_pct else None
            if stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif tp and bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif e.hold_days and t.hold_days >= e.hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 매수: 엔벨로프 하단선 근접 종가
        slots = s.num_slots - len(positions)
        uni = e.universe.get(day, set()) if e.universe is not None else None
        if slots > 0 and (uni is None or uni):
            signals = []
            for sym in (uni if uni is not None else series):
                ser = series.get(sym)
                if ser is None or sym in positions:
                    continue
                i = ser.index.get(day)
                if i is None or i + 1 < e.period:
                    continue
                bar = ser.bars[i]
                lower = sum(x.close for x in ser.bars[i + 1 - e.period:i + 1]) / e.period * (1 - e.pct / 100)
                if e.mode == "near":
                    ok = abs(bar.close / lower - 1) * 100 <= e.near_pct + 1e-9
                else:
                    ok = bar.close <= lower * (1 + e.near_pct / 100) + 1e-9
                if not ok or bar.close >= ser.bars[i - 1].close * 1.295:
                    continue
                key = (e.rank or {}).get(day, {}).get(sym, 0.0)
                signals.append((key, sym, bar))
            signals.sort(key=lambda x: x[0], reverse=True)
            equity_now = cash + sum(p.qty * last_close.get(ps, p.entry_price) for ps, p in positions.items())
            for _, sym, bar in signals[:slots]:
                price = bar.close * (1 + s.slippage)
                qty = int(equity_now * e.position_pct / 100 // price)
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, series[sym].name, day, price, qty, surge_date=day)
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


def load_marcap(data_dir: Path, start: str, end: str | None = None) -> dict[str, dict[date, float]]:
    """FinanceData/marcap parquet 에서 종목별·날짜별 시가총액."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    y1 = date.fromisoformat(end).year if end else date.today().year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, y1 + 1)]
    df = pd.concat([pd.read_parquet(f, columns=["Code", "Date", "Marcap"]) for f in files if f.exists()])
    out: dict[str, dict[date, float]] = defaultdict(dict)
    for code, d, cap in zip(df["Code"], df["Date"], df["Marcap"]):
        out[code][d.date()] = float(cap)
    return out


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


def index_streak_up_days(index: list[Bar], streak: int = 3) -> set[date]:
    """지수 종가가 오늘 포함 streak 거래일 연속 전일보다 오른 날."""
    out, run = set(), 0
    for prev, cur in zip(index, index[1:]):
        run = run + 1 if cur.close > prev.close else 0
        if run >= streak:
            out.add(cur.day)
    return out


def index_not_down_days(index: list[Bar]) -> set[date]:
    """지수 종가가 전일 종가 이상인 날 (실전 MARKET_FILTER=kospi_not_down 에 해당)."""
    return {cur.day for prev, cur in zip(index, index[1:]) if cur.close >= prev.close}


def index_regime_days(index: list[Bar], long: int = 200, short: int = 20, slope_days: int = 5) -> set[date]:
    """지수가 long 일선 위면 short 일선이 오르는 중(slope_days 거래일 전보다 높음)인 날,
    long 일선 아래면 short 일선이 내리는 중인 날만."""
    closes = [b.close for b in index]
    ma = lambda n, i: sum(closes[i - n + 1:i + 1]) / n  # noqa: E731
    out = set()
    for i in range(max(long, short + slope_days) - 1, len(index)):
        rising = ma(short, i) > ma(short, i - slope_days)
        if (closes[i] > ma(long, i)) == rising:
            out.add(index[i].day)
    return out


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
    r.add_argument("--strategy", choices=["pullback", "breakout", "limitup", "surgedoji", "retest", "mapullback"], default="pullback",
                   help="pullback: 급등 후 이평선 터치 / breakout: 종가 N일 신고가 매수·M일 신저가 매도 / "
                        "limitup: 전일 상한가 종목 시초가 매수 / "
                        "surgedoji: 거래대금 상위 +15%% 급등 후 거래량 급감 단봉 음봉 종가 매수 / "
                        "retest: 급등 + N일 신고가 기준봉 이후 이전 고점까지 되돌리면 지정가 매수")
    r.add_argument("--surge-pct", type=float, default=15, help="retest: 기준봉 상승률 하한 %%")
    r.add_argument("--surge-max-pct", type=float, default=0, help="retest: 기준봉 상승률 상한 %% (0 = 없음)")
    r.add_argument("--retest-level", choices=["high", "close"], default="high",
                   help="retest: 이전 고점 = 직전 N일 장중 고가 최고값(high) / 종가 최고값(close)")
    r.add_argument("--mp-entry", choices=["ma", "bear"], default="ma",
                   help="mapullback: ma = 이평선 눌림 지정가 매수 / bear = 기준봉 뒤 첫 음봉 종가 매수")
    r.add_argument("--bear-max", type=float, default=3.0, help="mapullback bear: 첫 음봉 하락폭 N%% 이내만 매수")
    r.add_argument("--bear-measure", choices=["body", "change"], default="body",
                   help="mapullback bear: 하락폭 기준 body = 시가 대비 / change = 전일 종가 대비")
    r.add_argument("--mp-pick", choices=["", "amount", "change", "both", "combo"], default="",
                   help="mapullback: 하루 기준봉 1종목만 (amount 거래대금 1위 / change 상승률 1위 / both 둘 다 1위 / combo 순위 합)")
    r.add_argument("--no-above-ma", action="store_true", help="mapullback: 기준봉 종가 이평선 위 조건 끄기")
    r.add_argument("--pullback-ma", type=int, default=200, help="mapullback: 눌림 매수 이평선 기간")
    r.add_argument("--watch-days", type=int, default=10, help="retest: 기준봉 뒤 N거래일 안에 닿아야 매수")
    r.add_argument("--max-break", type=float, default=3.0, help="retest: 시가가 매수가보다 N%% 넘게 낮으면 매수 취소")
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
    r.add_argument("--surge-min-amount", type=float, default=0,
                   help="pullback: 급등일(기준봉) 거래대금 하한 (원)")
    r.add_argument("--allow-limit-up-signal", action="store_true",
                   help="breakout: 신호일 상한가 마감도 신호로 인정 (--entry-delay 와 함께)")
    r.add_argument("--breakeven-at", type=float, default=0,
                   help="breakout: 고가가 매수가 대비 N%% 오르면 다음 날부터 손절가를 본전으로 (0 = 없음)")
    r.add_argument("--next-day-exit", choices=["", "open", "close", "open_if_loss", "close_if_loss", "open_if_no_gap"], default="",
                   help="breakout: 매수 다음 거래일 매도 (시가/종가, _if_loss 는 매수가 아래일 때만)")
    r.add_argument("--ma-exit", type=int, default=0, help="breakout: 종가가 N일선 아래로 마감하면 종가 매도 (0 = 없음)")
    r.add_argument("--ma-exit-profit-only", action="store_true", help="breakout: 이평선 이탈 매도를 수익 중일 때만")
    r.add_argument("--max-buys-per-day", type=int, default=0, help="breakout: 하루 신규 매수 종목 수 상한 (0 = 없음)")
    r.add_argument("--marcap-dir", type=Path, default=Path("marcap/data"), help="breakout: 시가총액용 marcap parquet 폴더")
    r.add_argument("--min-marcap", type=float, default=0, help="breakout: 신호일 시가총액 하한 (원, --marcap-dir 필요)")
    r.add_argument("--exclude-marcap", type=float, nargs=2, metavar=("LO", "HI"),
                   help="breakout: 시가총액이 LO 초과 HI 미만인 종목 제외 (--marcap-dir 필요)")
    r.add_argument("--skip-touched-limit-up", action="store_true", help="breakout: 장중 상한가를 찍었던 종목 제외")
    r.add_argument("--max-drop-from-high", type=float, default=0,
                   help="breakout: 종가가 당일 고가 대비 N%% 이상 내려온 종목 제외 (0 = 없음)")
    r.add_argument("--buy-day-gap-up", action="store_true", help="breakout: 매수일 시가가 전일 종가보다 높을 때만 (--entry-delay)")
    r.add_argument("--buy-at-open", action="store_true", help="breakout: --entry-delay 매수를 매수일 시가에")
    r.add_argument("--buy-day-bullish", action="store_true", help="breakout: 매수일 양봉일 때만 (--entry-delay)")
    r.add_argument("--buy-day-wick", type=float, default=0,
                   help="breakout: 매수일 윗꼬리가 몸통의 N배 이상일 때만 (--entry-delay, 0 = 없음)")
    r.add_argument("--ma-order", type=str, default="",
                   help="breakout: 이평선 배열, 위에서부터 (예: 60,5,20 = 60일선 > 5일선 > 20일선)")
    r.add_argument("--recover-after-drop", type=float, default=0,
                   help="breakout: 매수가 대비 N%% 이상 빠지면 다음 날부터 본전 지정가 매도 (0 = 없음)")
    r.add_argument("--stop-limit", action="store_true",
                   help="breakout: 손절을 손절가 지정가로 (갭하락이면 손절가 회복까지 미체결)")
    r.add_argument("--exclude-high-days", type=int, default=0,
                   help="breakout: N일 신고가이기도 한 종목 제외 (예: 200)")
    r.add_argument("--exclude-list", type=Path, help="breakout: 매수 제외 종목 목록 파일 (줄마다 '종목코드 이름', # 주석)")
    r.add_argument("--ma-resist", type=float, nargs=2, metavar=("DAYS", "PCT"),
                   help="breakout: 종가가 DAYS일선 아래이고 DAYS일선까지 PCT%% 미만 남았으면 매수 금지 (예: 200 5)")
    r.add_argument("--max-rise-from-low", type=float, nargs=2, metavar=("DAYS", "PCT"),
                   help="breakout: DAYS일 최저가 대비 상승률이 PCT%% 초과면 매수 금지 (예: 250 100)")
    r.add_argument("--max-breakout", type=float, default=0,
                   help="breakout: 직전 고점 대비 돌파폭이 N%% 이하인 종목만 (0 = 없음)")
    r.add_argument("--breakout-basis", choices=["close", "high"], default="close",
                   help="breakout: --max-breakout 의 직전 고점 = 종가 최고값(close) / 장중 고가 최고값(high)")
    r.add_argument("--position-pct", type=float, default=0,
                   help="breakout: 종목당 매수 금액 = 현재 평가금액의 N%% (복리, 0 = 종목당 예산 고정)")
    r.add_argument("--step-sizing", action="store_true",
                   help="breakout: 평가금액 10만원 늘 때마다 종목당 1만원씩 증액 (110만원 → 11만원, 최소 종목당 예산)")
    r.add_argument("--min-shares", type=int, default=0,
                   help="breakout: 최소 N주 매수 (--position-pct, --max-position-pct 와 함께)")
    r.add_argument("--max-position-pct", type=float, default=20,
                   help="breakout: --min-shares 사용 시 종목당 평가금액 상한 %%")
    r.add_argument("--winrate-cut", type=float, default=0,
                   help="breakout: 최근 청산 승률이 N%% 이하면 최대 보유 종목 수를 --reduced-slots 로 (0 = 없음)")
    r.add_argument("--kelly-max-pct", type=float, default=0,
                   help="breakout: 최근 --winrate-window 건 승률·손익비로 켈리 비중, 종목당 평가금액의 최대 N%% (0 = 없음)")
    r.add_argument("--kelly-min-pct", type=float, default=5, help="breakout: 켈리 비중 최소 %% (켈리가 0 이하여도 이만큼 매수)")
    r.add_argument("--kelly-scale", type=float, default=1.0, help="breakout: 켈리 비율 배수 (0.5 = 하프 켈리)")
    r.add_argument("--winrate-window", type=int, default=20, help="breakout: 최근 승률 계산 청산 건수")
    r.add_argument("--reduced-slots", type=int, default=8, help="breakout: 승률 저하 시 최대 보유 종목 수")
    r.add_argument("--continuous", action="store_true",
                   help="연도마다 새로 시작하지 않고 첫 해 1월 1일부터 끝까지 한 번에 (복리 확인용)")
    r.add_argument("--lock-profit", type=float, default=0,
                   help="breakout: --breakeven-at 이후 손절가를 매수가 +N%%로 (0 = 본전)")
    r.add_argument("--no-new-high", action="store_true", help="breakout: 신고가 조건 없이 매수 (--min-change 와 함께)")
    r.add_argument("--min-change", type=float, default=0, help="breakout: 당일 상승률 N%% 이상인 종목만")
    r.add_argument("--min-price", type=float, default=0, help="breakout: 1주 가격 하한 (원)")
    r.add_argument("--max-price", type=float, default=0, help="breakout: 1주 가격 상한 (원, 0 = 종목당 예산)")
    r.add_argument("--max-day-amount", type=float, default=0, help="breakout: 신호 당일 거래대금 상한 (원, 0 = 없음)")
    r.add_argument("--entry-delay", type=int, default=0, help="breakout: 신고가 신호 N거래일 뒤 종가에 매수")
    r.add_argument("--min-candle", type=float, default=0, help="breakout: 신고가 봉 길이 하한 %%")
    r.add_argument("--candle-measure", choices=["body", "range"], default="body",
                   help="breakout: body = 양봉 몸통 (종가/시가), range = 고가/저가")
    r.add_argument("--max-gap", type=float, default=0,
                   help="breakout: 신고가 날 시가 갭상승(전일 종가 대비)이 N%% 이하인 종목만 매수 (0 = 없음)")
    r.add_argument("--kospi-not-down", action="store_true",
                   help="breakout: 코스피 종가가 전일보다 낮은 날은 매수 금지")
    r.add_argument("--kospi-regime", action="store_true",
                   help="breakout: 코스피가 200일선 위면 20일선 상승일만, 아래면 20일선 하락일만 매수")
    r.add_argument("--regime-slope-days", type=int, default=5, help="breakout: 20일선 기울기 비교 기간 (거래일)")
    r.add_argument("--kospi-up-days", type=int, default=0,
                   help="breakout: 코스피 종가가 N거래일 전보다 높은 날에만 매수 (0 = 필터 없음)")
    r.add_argument("--slot-budget", type=float, default=0,
                   help="종목당 매수 금액·1주 가격 상한 (원). 시작 자금 = 이 값 x 최대 종목 수 (0 = .env 설정)")
    r.add_argument("--max-amount-rank", type=int, default=0,
                   help="breakout: 신호 당일 거래대금 순위 N위 이내 종목만 (0 = 없음)")
    r.add_argument("--rank-by", choices=["amount", "change", "strength", "weak", "calm"], default="amount",
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
    if args.slot_budget:
        # 종목당 예산(= 1주 가격 상한)을 바꾸고 시작 자금은 종목당 예산 x 최대 종목 수
        cfg = dataclasses.replace(cfg, total_budget=int(args.slot_budget) * cfg.num_stocks)
    settings = BacktestSettings.from_config(cfg)
    settings.params.min_surge_amount = args.surge_min_amount
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
                              entry_delay=args.entry_delay, delay_max_rise_pct=args.delay_max_rise, rank_by=args.rank_by, max_amount_rank=args.max_amount_rank,
                              delay_hold_signal_open=args.delay_hold_open, delay_intraday=args.delay_intraday,
                              min_avg_trading_amount=settings.params.min_avg_trading_amount,
                              min_candle_pct=args.min_candle, candle_measure=args.candle_measure,
                              max_gap_pct=args.max_gap, max_day_amount=args.max_day_amount,
                              max_price=args.max_price, min_price=args.min_price,
                              require_new_high=not args.no_new_high, min_change_pct=args.min_change,
                              skip_limit_up=not args.allow_limit_up_signal,
                              breakeven_trigger_pct=args.breakeven_at, breakeven_lock_pct=args.lock_profit,
                              next_day_exit=args.next_day_exit, ma_exit_days=args.ma_exit,
                              ma_exit_profit_only=args.ma_exit_profit_only, max_buys_per_day=args.max_buys_per_day,
                              min_marcap=args.min_marcap, skip_touched_limit_up=args.skip_touched_limit_up,
                              max_drop_from_high_pct=args.max_drop_from_high,
                              buy_day_gap_up=args.buy_day_gap_up, buy_at_open=args.buy_at_open, buy_day_bullish=args.buy_day_bullish,
                              buy_day_min_wick_ratio=args.buy_day_wick, recover_after_drop_pct=args.recover_after_drop,
                              stop_limit=args.stop_limit, exclude_high_days=args.exclude_high_days,
                              position_pct=args.position_pct, step_sizing=args.step_sizing,
                              min_shares=args.min_shares, max_position_pct=args.max_position_pct,
                              winrate_cut=args.winrate_cut, winrate_window=args.winrate_window,
                              kelly_max_pct=args.kelly_max_pct, kelly_min_pct=args.kelly_min_pct, kelly_scale=args.kelly_scale,
                              reduced_slots=args.reduced_slots,
                              max_breakout_pct=args.max_breakout, breakout_basis=args.breakout_basis,
                              low_rise_days=int(args.max_rise_from_low[0]) if args.max_rise_from_low else 0,
                              max_rise_from_low_pct=args.max_rise_from_low[1] if args.max_rise_from_low else 0.0,
                              ma_resist_days=int(args.ma_resist[0]) if args.ma_resist else 0,
                              ma_resist_pct=args.ma_resist[1] if args.ma_resist else 0.0,
                              exclude_symbols=frozenset(
                                  line.split()[0] for line in args.exclude_list.read_text(encoding="utf-8").splitlines()
                                  if line.strip() and not line.startswith("#")) if args.exclude_list else frozenset(),
                              ma_order=tuple(int(x) for x in args.ma_order.split(",")) if args.ma_order else (),
                              exclude_marcap=tuple(args.exclude_marcap) if args.exclude_marcap else None)
        if args.min_marcap or args.exclude_marcap:
            bs.marcap = load_marcap(args.marcap_dir, "2023-01-01")
            print(f"시가총액 조건: 하한 {args.min_marcap / 1e8:,.0f}억, 제외 구간 {args.exclude_marcap}")
        if args.kospi_not_down:
            settings.entry_dates = index_not_down_days(load_index(args.cache, "KOSPI"))
            print("코스피가 전일보다 낮은 날은 매수 금지")
        if args.kospi_regime:
            settings.entry_dates = index_regime_days(load_index(args.cache, "KOSPI"), slope_days=args.regime_slope_days)
            print(f"코스피 200일선 위 → 20일선 상승일만 / 아래 → 20일선 하락일만 매수 (기울기 {args.regime_slope_days}일)")
        if args.kospi_up_days:
            settings.entry_dates = index_uptrend_days(load_index(args.cache, "KOSPI"), args.kospi_up_days)
            print(f"코스피가 {args.kospi_up_days}거래일 전보다 높은 날에만 매수")
        runner = lambda y0, y1: run_breakout(data, y0, y1, settings, bs)  # noqa: E731
    elif args.strategy == "retest":
        rt = RetestSettings(surge_pct=args.surge_pct, surge_max_pct=args.surge_max_pct, min_amount=args.min_day_amount or 2e10,
                            entry_days=args.entry_days, level=args.retest_level, watch_days=args.watch_days,
                            first_in_days=args.first_in_days,
                            max_break_pct=args.max_break, stop_loss_pct=args.stop_loss or 4.7,
                            take_profit_pct=args.take_profit or 20.0)
        runner = lambda y0, y1: run_retest(data, y0, y1, settings, rt)  # noqa: E731
    elif args.strategy == "mapullback":
        mp = MaPullbackSettings(surge_pct=args.surge_pct, min_amount=args.min_day_amount or 2e11,
                                first_in_days=args.first_in_days, ma_period=args.pullback_ma,
                                watch_days=args.watch_days, max_break_pct=args.max_break,
                                stop_loss_pct=args.stop_loss if args.ma_exit else (args.stop_loss or 5.0),
                                take_profit_pct=args.take_profit if args.ma_exit else (args.take_profit or 20.0),
                                pick=args.mp_pick, ma_exit_days=args.ma_exit,
                                entry=args.mp_entry, bear_max_pct=args.bear_max, bear_measure=args.bear_measure,
                                require_above_ma=not args.no_above_ma)
        runner = lambda y0, y1: run_ma_pullback(data, y0, y1, settings, mp)  # noqa: E731
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
    if args.continuous:
        results = [runner(date(min(args.years), 1, 1), min(date(max(args.years), 12, 31), last_day))]
        print(f"종목 {len(data)}개, {settings.initial_cash:,.0f}원으로 전체 기간 한 번에\n")
    else:
        results = [runner(date(y, 1, 1), min(date(y, 12, 31), last_day)) for y in args.years]
        print(f"종목 {len(data)}개, 연도마다 {settings.initial_cash:,.0f}원으로 새로 시작\n")
    print_report(results)
    print(f"\n거래 내역: {save_trades(results, args.out)}")


if __name__ == "__main__":
    main()
