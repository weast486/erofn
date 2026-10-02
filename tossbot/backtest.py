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
import itertools

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
from .broker import round_down_to_tick, round_up_to_tick, tick_size
from .config import Config, load_dotenv
from .rsi import rsi_series
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
    # N > 0 이면 N일선 돌파일만: 전일 종가 <= 전일 N일선, 당일 종가 > 당일 N일선
    cross_ma: int = 0
    # True 면 상승 장악형만: 전일 음봉, 당일 양봉, 당일 시가 <= 전일 종가, 당일 종가 >= 전일 시가
    engulf: bool = False
    # N > 0 이면 당일 종가가 N일선(당일 포함) 아래인 종목만
    below_ma: int = 0
    # N자 패턴: "" 없음 / A = 눌림 저점 다음 첫 양봉 종가 매수 / B = 1차 고점(종가) 돌파 종가 매수.
    # 1차 고점 = 직전 np_lookback 거래일 종가 최고값, 그 전 np_lookback 거래일 최저 종가 대비 np_rise_pct % 이상 상승,
    # 상승 구간(저점~고점) 중 하루라도 거래대금 np_amount 이상. 고점 뒤 눌림 저점(종가)이 고점 대비
    # np_min_pull ~ np_max_pull % 하락, 1차 상승분의 절반 아래로는 안 빠짐. 고점과 오늘 사이 최소 2거래일
    npattern: str = ""
    np_lookback: int = 20
    np_rise_pct: float = 15.0
    np_min_pull: float = 5.0
    np_max_pull: float = 15.0
    np_amount: float = 20_000_000_000
    # C = 기준봉 재출현: 기준봉(거래대금 np_amount 이상·전일 대비 np_rise_pct % 이상 상승) 뒤 np_lookback 거래일 안에
    # 기준봉 저가(np_base_ref="open" 이면 시가)를 한 번도 깨지 않고 같은 조건의 봉이 다시 나온 날 종가 매수.
    # 기준봉 = 오늘 전 가장 최근의 같은 조건 봉, 기준봉과 오늘 사이 최소 np_min_gap 거래일.
    # np_first_base 면 기준봉 앞 np_lookback 거래일 안에 같은 조건 봉이 없어야 함 (오늘이 정확히 두 번째)
    np_base_ref: str = "low"
    np_min_gap: int = 2
    np_first_base: bool = True
    # C: 기준봉 다음 날 ~ 어제 사이 거래량이 평균 이하로 떨어진 날 조건. "" 없음 / any = 하루 이상 / all = 모든 날 /
    # last = 매수 전날(두 번째 봉 직전 봉)만.
    # 평균 = 그날까지 np_vol_avg 일 평균 거래량(rolling) 또는 기준봉 전날까지 np_vol_avg 일 평균(base)
    np_quiet: str = ""
    np_vol_avg: int = 20
    np_vol_basis: str = "rolling"
    # C: N > 0 이면 기준봉이 N일선 부근인 것만. open = 기준봉 시가가 N일선(전날까지) ±np_base_ma_pct % 안 /
    # cross = 기준봉이 N일선을 뚫음 (시가 <= N일선 < 종가) / low = 기준봉 저가가 N일선 ±% 안
    np_base_ma: int = 0
    np_base_ma_pct: float = 5.0
    np_base_ma_mode: str = "open"
    # D = 기준봉 절반 눌림 뒤 거래량 증가 양봉: 기준봉(C 와 같은 조건, np_lookback 거래일 안의 가장 최근) 뒤
    # 종가가 기준봉 종가를 넘은 날이 없고, 기준봉 몸통 절반((시가+종가)/2) 이하로 내려간 날(np_dip_ref 저가/종가)이 있은 뒤,
    # 기준봉 시가를 깨지 않은 채(np_break_ref 저가/종가) 처음 나온 양봉 + 거래량 증가(전날 대비, np_volup_avg > 0 이면 N일 평균 대비) 종가 매수
    np_dip_ref: str = "low"
    np_break_ref: str = "low"
    np_volup_avg: int = 0


def _np_strong(bars: list[Bar], k: int, b: "BreakoutSettings") -> bool:
    x = bars[k]
    return (k >= 1 and x.close * x.volume >= b.np_amount
            and (x.close / bars[k - 1].close - 1) * 100 >= b.np_rise_pct - 1e-9)


def _np_half_dip_signal(bars: list[Bar], i: int, b: "BreakoutSettings") -> bool:
    L = b.np_lookback
    if i < L + 1:
        return False
    k = next((k for k in range(i - 1, i - L - 1, -1) if _np_strong(bars, k, b)), None)
    if k is None or i - k < 2:
        return False
    base = bars[k]
    mid, floor = (base.open + base.close) / 2, base.open

    def broke(x: Bar) -> bool:
        return (x.close if b.np_break_ref == "close" else x.low) < floor

    def bull_volup(j: int) -> bool:
        x = bars[j]
        if not x.close > x.open:
            return False
        if b.np_volup_avg:
            n = b.np_volup_avg
            return j >= n and x.volume > sum(y.volume for y in bars[j - n:j]) / n
        return x.volume > bars[j - 1].volume

    dipped = False
    for j in range(k + 1, i + 1):
        x = bars[j]
        if broke(x) or x.close > base.close:
            return False
        if dipped and bull_volup(j):
            return j == i  # 눌림 뒤 첫 신호만
        if (x.close if b.np_dip_ref == "close" else x.low) <= mid:
            dipped = True
    return False


def n_pattern_signal(bars: list[Bar], i: int, b: "BreakoutSettings") -> bool:
    """오늘(i) 종가 기준 N자 패턴 매수 신호인지 (BreakoutSettings.npattern 설명 참고)."""
    L = b.np_lookback
    if b.npattern == "D":
        return _np_half_dip_signal(bars, i, b)
    if b.npattern == "C":
        if i < L + 1 or not _np_strong(bars, i, b):
            return False
        k = next((k for k in range(i - 1, i - L - 1, -1) if _np_strong(bars, k, b)), None)
        if k is None or i - k < b.np_min_gap:
            return False
        if b.np_first_base and any(_np_strong(bars, j, b) for j in range(max(k - L, 1), k)):
            return False
        if b.np_base_ma:
            n = b.np_base_ma
            if k < n:
                return False
            ma = sum(x.close for x in bars[k - n:k]) / n  # 기준봉 전날까지 N일선
            x, tol = bars[k], b.np_base_ma_pct / 100
            if b.np_base_ma_mode == "cross":
                if not (x.open <= ma * (1 + tol) and x.close > ma):
                    return False
            elif abs((x.low if b.np_base_ma_mode == "low" else x.open) / ma - 1) > tol:
                return False
        ref = bars[k].open if b.np_base_ref == "open" else bars[k].low
        if min(x.low for x in bars[k + 1:i + 1]) < ref:
            return False
        if b.np_quiet:
            n = b.np_vol_avg
            if k < n:
                return False
            base_avg = sum(x.volume for x in bars[k - n:k]) / n

            def quiet(j: int) -> bool:
                avg = base_avg if b.np_vol_basis == "base" else sum(x.volume for x in bars[j + 1 - n:j + 1]) / n
                return bars[j].volume <= avg

            q = [quiet(j) for j in range(k + 1, i)]
            if b.np_quiet == "last":
                q = q[-1:]  # 매수 전날 하루만
            if not q or not (any(q) if b.np_quiet == "any" else all(q)):
                return False
        return True
    if i < 2 * L + 1:
        return False
    closes = [x.close for x in bars[i - 2 * L:i + 1]]  # closes[-1] = 오늘
    base = 2 * L  # 오늘의 closes 인덱스
    win = range(base - L, base)  # 직전 L 거래일
    p = max(win, key=lambda k: (closes[k], k))  # 1차 고점 (같으면 최근)
    if base - p < 3:
        return False  # 고점 뒤 눌림이 최소 2거래일
    peak = closes[p]
    lo_k = min(range(max(p - L, 0), p), key=lambda k: closes[k])
    low = closes[lo_k]
    if peak < low * (1 + b.np_rise_pct / 100):
        return False
    j0 = i - base  # closes 인덱스 → bars 인덱스
    if max(bars[j0 + k].close * bars[j0 + k].volume for k in range(lo_k, p + 1)) < b.np_amount:
        return False
    after = closes[p + 1:base]  # 고점 다음 날 ~ 어제
    trough = min(after)
    pull = (1 - trough / peak) * 100
    if not (b.np_min_pull <= pull <= b.np_max_pull) or trough < low + (peak - low) / 2:
        return False
    today, yday = bars[i], bars[i - 1]
    if b.npattern == "A":
        # 어제가 눌림 저점이고, 오늘 양봉으로 반등 (아직 고점 아래)
        return yday.close == trough and today.close > today.open and today.close > yday.close and today.close <= peak
    return today.close > peak  # B: 고점 돌파 (직전 L일 최고 종가라 첫 돌파)


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
    signal_log: list | None = None,
) -> Result:
    """종가 기준 entry_days 일 신고가 매수 / exit_days 일 신저가 매도.

    - 매수 신호: 오늘 종가 > 직전 entry_days 거래일 종가의 최고값 → 오늘 종가에 매수
    - 매도 신호: 오늘 종가 < 직전 exit_days 거래일 종가의 최저값 → 오늘 종가에 매도
    - stop_loss_pct > 0 이면 장중 저가가 손절가 이하일 때 손절가(갭이면 시가)에 매도
    - 종목당 slot_budget, 최대 num_slots 종목. 신호가 많으면 rank_by 순서
    - signal_log 를 주면 매일의 매수 후보 (날짜, 우선순위, 종목) 를 기록 (entry_delay 없을 때)
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
        if slots > 0 or b.entry_delay or signal_log is not None:
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
                if b.npattern and not n_pattern_signal(ser.bars, i, b):
                    continue
                if b.engulf:
                    pb, cb = ser.bars[i - 1], ser.bars[i]
                    if not (pb.close < pb.open and cb.close > cb.open and cb.open <= pb.close and cb.close >= pb.open):
                        continue
                if b.below_ma:
                    n = b.below_ma
                    if i + 1 < n or ser.bars[i].close >= sum(x.close for x in ser.bars[i + 1 - n:i + 1]) / n:
                        continue
                if b.cross_ma:
                    n = b.cross_ma
                    if i < n:
                        continue
                    ma_today = sum(x.close for x in ser.bars[i + 1 - n:i + 1]) / n
                    ma_prev = sum(x.close for x in ser.bars[i - n:i]) / n
                    if not (ser.bars[i - 1].close <= ma_prev and ser.bars[i].close > ma_today):
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
                    if signal_log is not None:  # 보유·자금과 무관한 매수 후보 기록 (num_slots=0 으로 호출)
                        signal_log.append((day, key, sym))
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
    bear_min_ma: int = 0  # bear: N > 0 이면 첫 음봉 종가가 N일선(당일 종가 포함) 아래면 매수 안 함 (기준봉 폐기)


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
                if m.bear_min_ma and (i + 1 < m.bear_min_ma or bar.close < (
                        csum[sym][i + 1] - csum[sym][i + 1 - m.bear_min_ma]) / m.bear_min_ma):
                    continue  # 첫 음봉이 이평선 이탈
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
class MaCrossSettings:
    """ma_period 일선이 하락 기울기일 때 종가가 이평선을 상향 돌파(기준일) → 이후 watch_days 안에 이평선이
    상승 기울기로 바뀐 상태에서 위에서 내려와 이평선에 닿으면 이평선 가격(전일까지 평균) 지정가 매수.
    목표가 = 돌파 전 high_lookback 거래일의 최고 고가(전고점) 지정가 매도, 손절 stop_loss_pct %."""
    ma_period: int = 50
    slope_days: int = 5  # 기울기 = 오늘 이평선 vs slope_days 거래일 전 이평선
    min_amount: float = 10_000_000_000  # 돌파일 거래대금 하한
    watch_days: int = 60  # 돌파 다음 날부터 N거래일 안에 닿아야 매수
    high_lookback: int = 120  # 전고점 = 돌파일 직전 N거래일 최고 고가
    min_upside_pct: float = 5.0  # 매수가 대비 전고점까지 이 % 이상 남아야 매수
    max_break_pct: float = 3.0  # 시가가 이평선보다 이 % 넘게 아래서 시작하면 매수 취소
    stop_loss_pct: float = 5.0  # 0 = 손절 없음
    max_hold_days: int = 0  # 0 = 제한 없음. N거래일째 종가 매도
    ma_exit_pct: float = 0.0  # > 0 이면 종가가 이평선보다 이 % 넘게 아래로 마감한 날 종가 매도
    # 전고점 기준: before = 돌파 전 high_lookback 거래일 최고 고가 / since = 돌파일부터 매수 전날까지 최고 고가
    target: str = "before"


def run_ma_cross(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    m: MaCrossSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    m = m or MaCrossSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    mas: dict[str, list[float | None]] = {}
    for sym, ser in series.items():
        acc, run = [0.0], 0.0
        for x in ser.bars:
            run += x.close
            acc.append(run)
        n = m.ma_period
        mas[sym] = [(acc[i + 1] - acc[i + 1 - n]) / n if i + 1 >= n else None for i in range(len(ser.bars))]

    def slope(sym: str, i: int) -> int:
        """1 상승 / -1 하락 / 0 모름·보합."""
        if i - m.slope_days < 0:
            return 0
        a, b = mas[sym][i], mas[sym][i - m.slope_days]
        if a is None or b is None or a == b:
            return 0
        return 1 if a > b else -1

    watch: dict[str, tuple[int, float, float]] = {}  # sym -> (돌파 인덱스, 전고점, 거래대금)
    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    targets: dict[str, float] = {}
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
        # 1) 청산: 손절 / 전고점 익절 (같은 날 둘 다면 손절) / 이평선 이탈 / 보유기간
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None or t.entry_date == day:
                continue
            bar = ser.bars[i]
            stop = stop_of(t.entry_price) if m.stop_loss_pct > 0 else None
            tp = round_up_to_tick(targets[sym])
            if stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif m.ma_exit_pct and mas[sym][i] and bar.close < mas[sym][i] * (1 - m.ma_exit_pct / 100):
                close_position(t, day, bar.close * (1 - s.slippage), "MA_EXIT")
            elif m.max_hold_days and t.hold_days >= m.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "MAX_HOLD")

        # 2) 이평선 상승 기울기에서 위에서 내려와 닿으면 매수
        signals = []
        for sym, (k, target, amt) in list(watch.items()):
            ser = series[sym]
            i = ser.index.get(day)
            if i is None or i <= k:
                continue
            if i - k > m.watch_days:
                del watch[sym]
                continue
            level_raw = mas[sym][i - 1]
            if level_raw is None or slope(sym, i - 1) <= 0 or ser.bars[i - 1].close <= level_raw:
                continue  # 이평선 하락·보합 중이거나 전일 종가가 이평선 아래 (위에서 닿는 것만)
            level = round_down_to_tick(level_raw)
            bar = ser.bars[i]
            if bar.low > level:
                continue
            del watch[sym]  # 조건 맞는 첫 터치에서만 판단
            if m.target == "since":
                target = max(x.high for x in ser.bars[k:i])
            if bar.open <= level:
                if bar.open < level * (1 - m.max_break_pct / 100):
                    continue
                fill = bar.open
            else:
                fill = level
            if target < fill * (1 + m.min_upside_pct / 100):
                continue  # 전고점까지 여유 부족 (이미 넘었거나 가까움)
            if sym not in positions and fill <= budget:
                signals.append((amt, sym, fill, bar, target, ser.bars[k].day))
        slots = s.num_slots - len(positions)
        signals.sort(key=lambda x: x[0], reverse=True)
        for _, sym, fill, bar, target, cross_day in signals[:max(slots, 0)]:
            price = fill * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=cross_day)
            positions[sym] = t
            targets[sym] = target
            trades.append(t)
            if m.stop_loss_pct > 0 and bar.low <= stop_of(price):
                close_position(t, day, stop_of(price) * (1 - s.slippage), "STOP_LOSS")

        # 3) 오늘 종가로 하락 기울기 이평선 상향 돌파 등록
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < max(1, m.high_lookback) or sym in positions:
                continue
            a, a0 = mas[sym][i], mas[sym][i - 1]
            bar, prev = ser.bars[i], ser.bars[i - 1]
            if a is None or a0 is None or not (prev.close <= a0 and bar.close > a):
                continue
            if slope(sym, i) >= 0 or bar.close * bar.volume < m.min_amount:
                continue
            target = max(x.high for x in ser.bars[i - m.high_lookback:i])
            watch[sym] = (i, target, bar.close * bar.volume)

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
class DoubleBottomSettings:
    """하락 추세 쌍바닥: (1) 하락 추세 속 저점 L1 (downtrend_days 거래일 최저가 + 그 기간 이평선 하락,
    drop_pct > 0 이면 drop_lookback 거래일 최고가 대비 drop_pct % 이상 하락) → (2) 거래대금 min_amount 이상 양봉으로 반등,
    반등 고점 H → (3) L1 ±near_pct % 까지 다시 눌림, 눌림 최저가 L2 → (4) 다시 거래대금 min_amount 이상 양봉 날 종가 매수.
    손절 = L2 이탈(L2 한 호가 아래), 익절 = H 지정가."""
    downtrend_days: int = 60
    drop_pct: float = 0.0
    drop_lookback: int = 250
    min_amount: float = 10_000_000_000  # 반등일·매수일 거래대금 하한
    min_change_pct: float = 0.0  # 반등일·매수일 전일 대비 상승률 하한
    amount_mult: float = 0.0  # > 0 이면 반등일·매수일 거래대금이 직전 20거래일 평균의 N배 이상이어야 함
    min_bounce_pct: float = 10.0  # 반등 고점 H 가 L1 대비 이 % 이상이어야 눌림 인정
    near_pct: float = 5.0  # 눌림 저점이 L1 x (1 ± near_pct %) 안
    bounce_wait: int = 20  # L1 뒤 N거래일 안에 반등
    pullback_wait: int = 60  # 반등 뒤 N거래일 안에 눌림
    entry_wait: int = 20  # 눌림 뒤 N거래일 안에 두 번째 거래대금
    min_upside_pct: float = 5.0  # 매수가 대비 H 까지 이 % 이상 남아야 매수
    max_hold_days: int = 0


def run_double_bottom(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    m: DoubleBottomSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    m = m or DoubleBottomSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    n = m.downtrend_days
    mas: dict[str, list[float | None]] = {}
    for sym, ser in series.items():
        acc, run = [0.0], 0.0
        for x in ser.bars:
            run += x.close
            acc.append(run)
        mas[sym] = [(acc[i + 1] - acc[i + 1 - n]) / n if i + 1 >= n else None for i in range(len(ser.bars))]

    def is_downtrend_low(sym: str, i: int) -> bool:
        bars = series[sym].bars
        if i < 2 * n or mas[sym][i] is None or mas[sym][i - n] is None or mas[sym][i] >= mas[sym][i - n]:
            return False
        if bars[i].low > min(x.low for x in bars[i - n + 1:i + 1]):
            return False
        if m.drop_pct > 0:
            hi = max(x.high for x in bars[max(0, i - m.drop_lookback):i + 1])
            if bars[i].low > hi * (1 - m.drop_pct / 100):
                return False
        return True

    def big_up(bars: list[Bar], i: int) -> bool:
        b, p = bars[i], bars[i - 1]
        if m.amount_mult > 0:
            if i < 20:
                return False
            avg = sum(x.close * x.volume for x in bars[i - 20:i]) / 20
            if b.close * b.volume < avg * m.amount_mult:
                return False
        return (b.close > b.open and b.close * b.volume >= m.min_amount
                and b.close >= p.close * (1 + m.min_change_pct / 100) and b.close > p.close)

    # 종목별 상태: [단계, L1, L1 인덱스, H, 반등 인덱스, L2, 눌림 인덱스]  단계 1 저점 / 2 반등 / 3 눌림
    state: dict[str, list] = {}
    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    exits: dict[str, tuple[float, float]] = {}  # sym -> (손절가, 목표가)
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
        # 1) 청산: L2 이탈 손절 / H 익절 (같은 날 둘 다면 손절)
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None or t.entry_date == day:
                continue
            bar = ser.bars[i]
            stop, tp = exits[sym]
            if bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif m.max_hold_days and t.hold_days >= m.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "MAX_HOLD")

        # 2) 패턴 진행, 매수 신호는 종가 기준
        signals = []
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < 1:
                continue
            bars, bar = ser.bars, ser.bars[i]
            st = state.get(sym)
            if st and st[0] == 3:
                if bar.low < st[1] * (1 - m.near_pct / 100):
                    st = None  # 저점 크게 이탈 → 새 저점부터 다시
                elif i - st[6] > m.entry_wait:
                    st = None
                else:
                    if bar.low < st[5]:
                        st[5], st[6] = bar.low, i
                    if i > st[6] and big_up(bars, i):
                        fill = bar.close
                        if st[3] >= fill * (1 + m.min_upside_pct / 100) and sym not in positions and fill <= budget:
                            signals.append((bar.close * bar.volume, sym, fill, st[5], st[3], bars[st[4]].day))
                        st = None  # 눌림 저점을 만든 날 자체는 매수하지 않음 (다음 날부터)
            elif st and st[0] == 2:
                st[3] = max(st[3], bar.high)
                if bar.low < st[1] * (1 - m.near_pct / 100) or i - st[4] > m.pullback_wait:
                    st = None
                elif bar.low <= st[1] * (1 + m.near_pct / 100) and i > st[4]:
                    if st[3] >= st[1] * (1 + m.min_bounce_pct / 100):
                        st[0], st[5], st[6] = 3, bar.low, i
                    else:
                        st = None
            elif st and st[0] == 1:
                if bar.low < st[1]:
                    st = None  # 더 낮은 저점 → 아래에서 다시 판정
                elif i - st[2] > m.bounce_wait:
                    st = None
                elif big_up(bars, i):
                    st[0], st[3], st[4] = 2, bar.high, i
            if st is None and is_downtrend_low(sym, i):
                st = [1, bar.low, i, 0.0, 0, 0.0, 0]
            if st is None:
                state.pop(sym, None)
            else:
                state[sym] = st

        slots = s.num_slots - len(positions)
        signals.sort(key=lambda x: x[0], reverse=True)
        for _, sym, fill, low2, target, bounce_day in signals[:max(slots, 0)]:
            price = fill * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=bounce_day)
            positions[sym] = t
            stop = round_down_to_tick(low2)
            stop = round_down_to_tick(stop - tick_size(stop))
            exits[sym] = (stop, round_up_to_tick(target))
            trades.append(t)

        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class EngulfRetestSettings:
    """하락 추세 상승 장악형 되돌림: 하락 추세(종가가 ma_period 일선 아래, 이평선이 slope_days 전보다 낮음) 중
    전일 음봉 몸통을 감싸는 양봉(시가 <= 음봉 종가, 종가 >= 음봉 시가)이 나오고 양봉 몸통이 음봉 몸통의 body_mult 배 이상이면,
    다음 날부터 watch_days 거래일 안에 음봉 시가까지 내려오면 그 가격 지정가 매수.
    손절 = 음봉 저가 이탈(한 호가 아래), 익절 = 매수가 +take_profit_pct %."""
    ma_period: int = 20
    slope_days: int = 5
    body_mult: float = 2.0
    min_amount: float = 0.0  # 양봉 날 거래대금 하한
    watch_days: int = 10
    take_profit_pct: float = 20.0
    max_hold_days: int = 0
    # True 면 음봉 시가까지 내려온 날 종가가 음봉 몸통(음봉 종가~시가) 안에 있을 때만 그날 종가 매수.
    # 아니면 대기 기간 동안 계속 지켜봄
    close_in_body: bool = False
    # True 면 음봉 시가까지 내려온 날 저가가 음봉 몸통 안(음봉 종가 이상)일 때만 그날 종가 매수. 아니면 계속 지켜봄
    low_in_body: bool = False
    # True 면 양봉 다음 날부터 저가가 음봉 몸통 아래(음봉 종가 미만)로 한 번이라도 빠지면 그 종목은 대기 목록에서 제외
    drop_below_body: bool = False


def run_engulf_retest(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    m: EngulfRetestSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    m = m or EngulfRetestSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    n = m.ma_period
    mas: dict[str, list[float | None]] = {}
    for sym, ser in series.items():
        acc, run = [0.0], 0.0
        for x in ser.bars:
            run += x.close
            acc.append(run)
        mas[sym] = [(acc[i + 1] - acc[i + 1 - n]) / n if i + 1 >= n else None for i in range(len(ser.bars))]

    watch: dict[str, tuple[int, float, float, float, float]] = {}  # sym -> (양봉 인덱스, 음봉 시가, 손절가, 거래대금, 음봉 종가)
    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    exits: dict[str, tuple[float, float]] = {}
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
        # 1) 청산: 음봉 저가 이탈 손절 / +N% 익절 (같은 날 둘 다면 손절)
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None or t.entry_date == day:
                continue
            bar = ser.bars[i]
            stop, tp = exits[sym]
            if bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif bar.open >= tp:
                close_position(t, day, bar.open, "TAKE_PROFIT")
            elif bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif bar.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif m.max_hold_days and t.hold_days >= m.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "MAX_HOLD")

        # 2) 음봉 시가 되돌림 지정가 매수
        signals = []
        for sym, (k, level_raw, stop, amt, body_low) in list(watch.items()):
            ser = series[sym]
            i = ser.index.get(day)
            if i is None or i <= k:
                continue
            if i - k > m.watch_days:
                del watch[sym]
                continue
            bar = ser.bars[i]
            level = round_down_to_tick(level_raw)
            if m.drop_below_body and bar.low < body_low:
                del watch[sym]
                continue
            if bar.low > level:
                continue
            if m.close_in_body or m.low_in_body:
                if m.close_in_body and not (body_low <= bar.close <= level_raw):
                    continue
                if m.low_in_body and bar.low < body_low:
                    continue
                del watch[sym]
                if bar.close <= stop:
                    continue
                if sym not in positions and bar.close <= budget:
                    signals.append((amt, sym, bar.close, None, stop, ser.bars[k].day))
                continue
            del watch[sym]
            fill = min(bar.open, level)
            if fill <= stop:
                continue  # 시가가 이미 손절가 아래
            if sym not in positions and fill <= budget:
                signals.append((amt, sym, fill, bar, stop, ser.bars[k].day))
        slots = s.num_slots - len(positions)
        signals.sort(key=lambda x: x[0], reverse=True)
        for _, sym, fill, bar, stop, engulf_day in signals[:max(slots, 0)]:
            price = fill * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=engulf_day)
            positions[sym] = t
            exits[sym] = (stop, round_up_to_tick(fill * (1 + m.take_profit_pct / 100)))
            trades.append(t)
            if bar is not None and bar.low <= stop:  # 매수 뒤 같은 날 저가 이탈 (보수적)
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")

        # 3) 오늘 종가로 하락 추세 상승 장악형 등록
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < n + m.slope_days or sym in positions:
                continue
            b, p = ser.bars[i], ser.bars[i - 1]
            ma_p, ma_old = mas[sym][i - 1], mas[sym][i - 1 - m.slope_days]
            if ma_p is None or ma_old is None or not (p.close < ma_p and ma_p < ma_old):
                continue  # 음봉 날 기준 하락 추세
            body_p, body_b = p.open - p.close, b.close - b.open
            if body_p <= 0 or body_b < body_p * m.body_mult or b.open > p.close or b.close < p.open:
                continue
            if b.close * b.volume < m.min_amount:
                continue
            stop = round_down_to_tick(p.low)
            stop = round_down_to_tick(stop - tick_size(stop))
            watch[sym] = (i, p.open, stop, b.close * b.volume, p.close)

        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


@dataclass
class VolBreakoutSettings:
    """변동성 돌파 (래리 윌리엄스): 오늘 시가 + 전일 (고가 - 저가) x k 를 장중에 넘으면 그 가격에 매수,
    다음 거래일 시가에 매도. 후보는 전일 거래대금 min_amount 이상, 여러 종목이 닿으면 전일 거래대금 큰 순
    (장중 어느 종목이 먼저 닿았는지 일봉으로는 알 수 없음)."""
    k: float = 0.5
    min_amount: float = 20_000_000_000  # 전일 거래대금 하한
    ma_filter: int = 0  # N > 0 이면 전일 종가가 N일선(전일 포함) 위인 종목만
    stop_loss_pct: float = 0.0  # 0 보다 크면 매수 당일 저가가 손절가 이하일 때 손절 (보수적: 매수 뒤 닿았다고 가정)
    exit: str = "open"  # open = 다음 날 시가 매도 / close = 매수 당일 종가 매도
    min_range_pct: float = 0.0  # 전일 변동폭(고가-저가)/종가가 이 % 이상인 종목만
    universe: dict | None = None  # {날짜: 종목 집합} 전일 기준으로 여기 든 종목만 (예: 코스피 시가총액 상위 200)


def run_vol_breakout(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    v: VolBreakoutSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    v = v or VolBreakoutSettings()
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

    for day in calendar:
        # 1) 전날 산 종목 시가 매도
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is not None:
                close_position(t, day, ser.bars[i].open * (1 - s.slippage), "NEXT_OPEN")

        # 2) 오늘 목표가 돌파 매수
        signals = []
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < max(v.ma_filter, 21):
                continue
            prev, bar = ser.bars[i - 1], ser.bars[i]
            if v.universe is not None and sym not in v.universe.get(prev.day, ()):
                continue
            if prev.close * prev.volume < v.min_amount:
                continue
            if sum(x.close * x.volume for x in ser.bars[i - 21:i - 1]) / 20 < 3_000_000_000:
                continue
            rng = prev.high - prev.low
            if rng <= 0 or rng / prev.close * 100 < v.min_range_pct:
                continue
            if v.ma_filter and prev.close <= sum(x.close for x in ser.bars[i - v.ma_filter:i]) / v.ma_filter:
                continue
            target = round_up_to_tick(bar.open + rng * v.k)
            if bar.high < target or target >= prev.close * 1.295 or target > budget:
                continue
            signals.append((prev.close * prev.volume, sym, target, bar))
        signals.sort(key=lambda x: x[0], reverse=True)
        slots = s.num_slots - len(positions)
        for _, sym, target, bar in signals[:max(slots, 0)]:
            price = target * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(target * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=day)
            positions[sym] = t
            trades.append(t)
            stop = round_down_to_tick(price * (1 - v.stop_loss_pct / 100)) if v.stop_loss_pct > 0 else None
            if stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif v.exit == "close":
                close_position(t, day, bar.close * (1 - s.slippage), "SAME_CLOSE")

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
class RsiSettings:
    """RSI 평균회귀: RSI 가 buy_below 아래로 마감하면 종가 매수, 매도 조건(RSI >= sell_above / 종가 > exit_ma 일선 /
    손절 / 최대 보유일) 중 먼저 오는 것에 매도. 신호가 많으면 RSI 낮은 순."""
    period: int = 14
    buy_below: float = 30.0
    sell_above: float = 50.0  # 0 이면 RSI 매도 없음
    trend_ma: int = 0  # N > 0 이면 종가가 N일선 위인 종목만 매수 (상승 추세 안의 눌림)
    exit_ma: int = 0  # N > 0 이면 종가가 N일선 위로 마감하면 종가 매도
    stop_loss_pct: float = 0.0
    max_hold_days: int = 0
    min_amount: float = 0.0  # 신호일 거래대금 하한
    universe: dict | None = None  # {날짜: 종목 집합}
    max_price: float = 0.0  # 0 보다 크면 1주 가격 상한 (원)
    # N > 0 이면 "두 번째 과매도"만 매수: RSI 가 buy_below 아래로 새로 들어온 날(전일 >= buy_below),
    # 직전 N거래일 안에 이미 과매도 구간이 있었고 그 사이 RSI 가 second_reset 이상으로 회복했을 때
    second_within: int = 0
    second_reset: float = 0.0  # 0 이면 buy_below (한 번 30 위로 올라오기만 하면 됨)
    second_diverge: bool = False  # 두 번째 종가가 첫 과매도 최저 종가보다 낮은데 RSI 는 더 높을 때만 (상승 다이버전스)


def rsi_second_flags(closes: list[float], rsi: list[float | None], r: RsiSettings) -> list[bool]:
    """i 날이 두 번째(이상) 과매도 진입일인지."""
    out = [False] * len(closes)
    reset = r.second_reset or r.buy_below
    for i in range(1, len(closes)):
        if rsi[i] is None or rsi[i - 1] is None or not (rsi[i] < r.buy_below <= rsi[i - 1]):
            continue
        recovered = False
        for j in range(i - 1, max(i - r.second_within, 0) - 1, -1):
            v = rsi[j]
            if v is None:
                break
            if v >= reset:
                recovered = True
            elif v < r.buy_below:
                if not recovered:
                    break  # second_reset 까지 회복 못 하고 다시 빠짐 → 같은 구간의 연장으로 봄
                if r.second_diverge:
                    k, lo_c, lo_r = j, closes[j], v  # 첫 과매도 구간 최저 종가·RSI
                    while k - 1 >= 0 and rsi[k - 1] is not None and rsi[k - 1] < r.buy_below:
                        k -= 1
                        lo_c, lo_r = min(lo_c, closes[k]), min(lo_r, rsi[k])
                    out[i] = closes[i] < lo_c and rsi[i] > lo_r
                else:
                    out[i] = True
                break
        # N거래일 안에 앞선 과매도가 없으면 첫 번째 과매도 → 매수 안 함
    return out


def run_rsi(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings | None = None,
    r: RsiSettings | None = None,
) -> Result:
    s = s or BacktestSettings()
    r = r or RsiSettings()
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    rsi = {sym: rsi_series([x.close for x in ser.bars], r.period) for sym, ser in series.items()}
    second = {sym: rsi_second_flags([x.close for x in ser.bars], rsi[sym], r) for sym, ser in series.items()
              } if r.second_within else None
    csum: dict[str, list[float]] = {}
    for sym, ser in series.items():
        acc, run = [0.0], 0.0
        for x in ser.bars:
            run += x.close
            acc.append(run)
        csum[sym] = acc

    def ma(sym: str, i: int, n: int) -> float | None:
        return None if i + 1 < n else (csum[sym][i + 1] - csum[sym][i + 1 - n]) / n

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
        # 1) 매도
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:
                continue
            bar = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - r.stop_loss_pct / 100)) if r.stop_loss_pct > 0 else None
            if stop and bar.open <= stop:
                close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
            elif stop and bar.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif r.sell_above and rsi[sym][i] is not None and rsi[sym][i] >= r.sell_above:
                close_position(t, day, bar.close * (1 - s.slippage), "RSI_EXIT")
            elif r.exit_ma and (m := ma(sym, i, r.exit_ma)) is not None and bar.close > m:
                close_position(t, day, bar.close * (1 - s.slippage), f"MA{r.exit_ma}_EXIT")
            elif r.max_hold_days and t.hold_days >= r.max_hold_days:
                close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 매수 (오늘 종가)
        signals = []
        for sym, ser in series.items():
            if sym in positions:
                continue
            i = ser.index.get(day)
            if i is None or i < 21 or rsi[sym][i] is None or rsi[sym][i] >= r.buy_below:
                continue
            if second is not None and not second[sym][i]:
                continue
            bar = ser.bars[i]
            if r.universe is not None and sym not in r.universe.get(day, ()):
                continue
            if bar.close > budget or bar.close * bar.volume < r.min_amount or (r.max_price and bar.close > r.max_price):
                continue
            if sum(x.close * x.volume for x in ser.bars[i - 20:i]) / 20 < 3_000_000_000:
                continue
            if r.trend_ma and ((m := ma(sym, i, r.trend_ma)) is None or bar.close <= m):
                continue
            if bar.close <= ser.bars[i - 1].close * 0.705:
                continue  # 하한가 마감은 매수 가정에서 제외
            signals.append((rsi[sym][i], sym, bar))
        signals.sort(key=lambda x: x[0])
        for _, sym, bar in signals[:max(s.num_slots - len(positions), 0)]:
            price = bar.close * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(bar.close * (1 + BUY_LIMIT_SLIPPAGE)))
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


def run_shared(
    data: dict[str, tuple[str, list[Bar]]],
    start: date,
    end: date,
    s: BacktestSettings,
    b: BreakoutSettings,
    r: RsiSettings,
    priority: str = "breakout",
) -> Result:
    """신고가(breakout) + RSI 평균회귀를 한 계좌(현금·최대 종목 수 공유)로 운용.
    매일 매도(각 전략 규칙) → 종가 매수: 빈 자리를 priority 순서로 채움
    (breakout / rsi = 그 전략 후보 먼저, mix = 번갈아). 종목당 slot_budget 고정. Trade.reason 앞에 전략 표시."""
    budget = s.params.slot_budget
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})
    log: list = []
    run_breakout(data, start, end, BacktestSettings(params=s.params, initial_cash=0, num_slots=0), b, signal_log=log)
    bo_cands: dict[date, list[str]] = defaultdict(list)
    for d, key, sym in sorted(log, key=lambda x: (x[0], -x[1])):
        bo_cands[d].append(sym)
    rsi = {sym: rsi_series([x.close for x in ser.bars], r.period) for sym, ser in series.items()}

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    kind: dict[str, str] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, f"{kind.pop(t.symbol)}:{reason}"
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]

    for day in calendar:
        # 1) 매도
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:
                continue
            bar = ser.bars[i]
            if kind[sym] == "BO":
                stop = round_down_to_tick(t.entry_price * (1 - b.stop_loss_pct / 100))
                tp = round_up_to_tick(t.entry_price * (1 + b.take_profit_pct / 100))
                if bar.open <= stop:
                    close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
                elif bar.open >= tp:
                    close_position(t, day, bar.open, "TAKE_PROFIT")
                elif bar.low <= stop:
                    close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
                elif bar.high >= tp:
                    close_position(t, day, tp, "TAKE_PROFIT")
            else:
                stop = round_down_to_tick(t.entry_price * (1 - r.stop_loss_pct / 100)) if r.stop_loss_pct > 0 else None
                if stop and bar.open <= stop:
                    close_position(t, day, bar.open * (1 - s.slippage), "STOP_LOSS")
                elif stop and bar.low <= stop:
                    close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
                elif r.sell_above and rsi[sym][i] is not None and rsi[sym][i] >= r.sell_above:
                    close_position(t, day, bar.close * (1 - s.slippage), "RSI_EXIT")
                elif r.max_hold_days and t.hold_days >= r.max_hold_days:
                    close_position(t, day, bar.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 매수 후보
        bo = [("BO", sym) for sym in bo_cands.get(day, [])]
        rs = []
        for sym, ser in series.items():
            i = ser.index.get(day)
            if i is None or i < 21 or rsi[sym][i] is None or rsi[sym][i] >= r.buy_below:
                continue
            bar = ser.bars[i]
            if r.universe is not None and sym not in r.universe.get(day, ()):
                continue
            if bar.close > budget or (r.max_price and bar.close > r.max_price) or bar.close * bar.volume < r.min_amount:
                continue
            if sum(x.close * x.volume for x in ser.bars[i - 20:i]) / 20 < 3_000_000_000:
                continue
            if bar.close <= ser.bars[i - 1].close * 0.705:
                continue
            rs.append((rsi[sym][i], sym))
        rs = [("RSI", sym) for _, sym in sorted(rs)]
        if priority == "rsi":
            order = rs + bo
        elif priority == "mix":
            order = [x for pair in itertools.zip_longest(bo, rs) for x in pair if x]
        else:
            order = bo + rs
        for k, sym in order:
            if len(positions) >= s.num_slots:
                break
            if sym in positions:
                continue
            bar = series[sym].bars[series[sym].index[day]]
            price = bar.close * (1 + s.slippage)
            qty = int(budget // round_up_to_tick(bar.close * (1 + BUY_LIMIT_SLIPPAGE)))
            cost = qty * price * (1 + s.commission)
            if qty <= 0 or cost > cash:
                continue
            cash -= cost
            t = Trade(sym, series[sym].name, day, price, qty, surge_date=day)
            positions[sym] = t
            kind[sym] = k
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


def load_marcap_universe(data_dir: Path, start: str, min_marcap: float, market: str | None = None) -> dict:
    """날짜별 시가총액 min_marcap 이상 종목 {날짜: 종목 집합}. market 을 주면 그 시장(KOSPI/KOSDAQ)만."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, date.today().year + 1)]
    df = pd.concat([pd.read_parquet(f, columns=["Code", "Name", "Market", "Date", "Marcap"]) for f in files if f.exists()])
    df = df[df["Marcap"] >= min_marcap]
    if market:
        df = df[df["Market"] == market]
    df = df[[_is_common_stock(c, n) for c, n in zip(df["Code"], df["Name"])]]
    return {d.date(): set(g["Code"]) for d, g in df.groupby("Date")}


def write_universe_file(data_dir: Path, top_n: int, out: Path) -> None:
    """가장 최근 날짜의 코스피 시가총액 상위 top_n 종목을 '종목코드 종목명' 으로 저장 (실전 RSI 전략 대상)."""
    import pandas as pd

    files = sorted(data_dir.glob("marcap-*.parquet"))
    df = pd.read_parquet(files[-1], columns=["Code", "Name", "Market", "Date", "Marcap"])
    last = df["Date"].max()
    df = df[(df["Date"] == last) & (df["Market"] == "KOSPI")]
    df = df[[_is_common_stock(c, n) for c, n in zip(df["Code"], df["Name"])]].nlargest(top_n, "Marcap")
    lines = [f"# 코스피 시가총액 상위 {top_n} (우선주 등 제외, 기준일 {last:%Y-%m-%d}). "
             f"갱신: python -m tossbot.backtest universe"]
    lines += [f"{c} {n}" for c, n in zip(df["Code"], df["Name"])]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(df)}종목 (기준일 {last:%Y-%m-%d})")


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


# 과거 캔들 이어받기에 쓸 수 있는 파라미터 후보 (공개 스펙 미확인 → 실제로 더 과거가 오는 것을 찾는다)
_PAGE_KEYS = ("to", "before", "endDateTime", "endDate", "end", "until", "from")
_MINUTE_INTERVALS = ("1m", "3m", "5m", "10m", "15m", "30m", "60m", "1h", "1min", "minute")


def probe_toss_minute(out: Path, env: str = ".env", symbols: tuple[str, ...] = ("005930", "247540"),
                      request_interval: float = 0.12) -> list[str]:
    """토스증권 Open API 로 국내 분봉을 어디까지 받을 수 있는지 확인만 한다 (저장·주문 없음).

    확인 항목: 지원하는 분봉 간격, 한 번에 받는 최대 개수, 과거 이어받기 파라미터, 이어받기로 닿는 가장 오래된 날짜.
    결과는 화면과 out 파일에 남긴다 (API 키·계좌 정보는 쓰지 않음).
    """
    import json
    import time
    from datetime import datetime

    from .client import TossApiError, TossClient
    from .config import KST

    load_dotenv(env)
    cfg = Config.from_env()
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    lines: list[str] = []

    def note(msg: str) -> None:
        print(msg, flush=True)
        lines.append(msg)

    def candles(symbol: str, interval: str, count: int, extra: dict | None = None) -> list[dict]:
        params = {"symbol": symbol, "interval": interval, "count": count, "adjusted": "true", **(extra or {})}
        time.sleep(request_interval)
        return (client._request("GET", "/api/v1/candles", params=params) or {}).get("candles", [])

    def kst(ts: str) -> str:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M")

    def span(raw: list[dict]) -> str:
        if not raw:
            return "0개"
        ts = sorted(c["timestamp"] for c in raw)
        return f"{len(raw)}개 {kst(ts[0])} ~ {kst(ts[-1])}"

    note(f"[토스 분봉 확인] {datetime.now(KST):%Y-%m-%d %H:%M}")
    symbol = symbols[0]
    ok_intervals = []
    for interval in _MINUTE_INTERVALS:
        try:
            raw = candles(symbol, interval, 10)
        except TossApiError as exc:
            note(f"간격 {interval}: 안 됨 ({exc.status} {exc.code})")
            continue
        note(f"간격 {interval}: {span(raw)}")
        if raw:
            ok_intervals.append(interval)
    if not ok_intervals:
        note("결론: 분봉을 받을 수 없음")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return lines
    note(f"원본 예시: {json.dumps(candles(symbol, ok_intervals[0], 1))[:400]}")

    targets = [i for i in ("1m", "5m") if i in ok_intervals] or ok_intervals[:1]
    for interval in targets:
        count = 0
        for n in (5000, 2000, 1000, 500, 300, 200, 100, 50):
            try:
                raw = candles(symbol, interval, n)
            except TossApiError:
                continue
            count = n
            note(f"{interval} 한 번에 count={n} 요청 → {span(raw)}")
            break
        if not count or not raw:
            continue
        oldest = min(raw, key=lambda c: c["timestamp"])["timestamp"]
        page_key = None
        for key in _PAGE_KEYS:
            try:
                older = candles(symbol, interval, count, {key: oldest})
            except TossApiError as exc:
                note(f"  이어받기 {key}: 거부 ({exc.code})")
                continue
            if older and min(c["timestamp"] for c in older) < oldest:
                page_key = key
                note(f"  이어받기 {key}: 됨 → {span(older)}")
                break
            note(f"  이어받기 {key}: 효과 없음")
        if not page_key:
            note(f"{interval} 결론: 최근 {span(raw)} 까지만")
            continue
        # 하루씩 이어받으면 수백 번 요청해야 하므로, 날짜를 건너뛰며 그 시점 직전 데이터가 오는지만 본다
        for sym in symbols:
            found = None
            misses = 0
            for days_back in (7, 30, 90, 180, 365, 730, 1095, 1825):
                target = datetime.now(KST).replace(hour=15, minute=30, second=0, microsecond=0) - timedelta(days=days_back)
                try:
                    got = candles(sym, interval, count, {page_key: target.isoformat(timespec="milliseconds")})
                except TossApiError as exc:
                    note(f"  {sym} {days_back}일 전: 거부 ({exc.code})")
                    got = []
                # 그 시점 직전(5일 이내) 데이터가 와야 '있다'로 본다 (범위 밖이면 최근 것을 돌려줄 수도 있어서)
                newest = max((c["timestamp"] for c in got), default="")
                ok = bool(newest) and kst(newest) <= target.strftime("%Y-%m-%d %H:%M") \
                    and kst(newest) >= (target - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
                note(f"  {sym} {days_back}일 전({target:%Y-%m-%d}) 직전 요청 → {span(got)} {'있음' if ok else '없음'}")
                if ok:
                    found, misses = days_back, 0
                else:
                    misses += 1
                    if misses >= 2:
                        break
            note(f"{interval} 결론 {sym}: " + (f"최소 {found}일 전({(datetime.now(KST) - timedelta(days=found)):%Y-%m-%d})까지 있음"
                                               if found else "1주 전 데이터도 없음"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


# ---------------------------------------------------------------- 분봉 (1분봉) 단타
MINUTE_DIR = Path("data/minute")


@dataclass
class MinuteBar:
    t: str  # "HH:MM" (KST)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MinuteSettings:
    mode: str = "next"  # next: 일봉 첫 신고가 다음 날 / same: 장중 신고가 돌파한 당일
    entry_days: int = 20
    first_in_days: int = 20
    top_universe: int = 100  # 0 이면 시총 조건 없음 (전날 기준 코스피+코스닥 상위 N)
    max_price: float = 99_999
    min_day_amount: float = 2e11  # same 모드는 장중 누적 거래대금이 이 값을 넘은 뒤에만 매수
    min_avg_amount: float = 0  # 직전 20일 평균 거래대금 하한
    ma: int = 200  # 1분봉 이평선
    stop_loss: float = 1.0
    take_profit: float = 3.0
    entry_start: str = "09:00"
    exit_time: str = "15:10"  # 이 시각 봉의 시가(= 직전 분 종가 부근)에 시장가 정리
    commission: float = 0.00015
    slippage: float = 0.001


def load_top_universe_all(data_dir: Path, start: str, top_n: int) -> dict[date, set[str]]:
    """날짜별 코스피+코스닥 시가총액 상위 top_n 보통주."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, date.today().year + 1)]
    df = pd.concat([pd.read_parquet(f, columns=["Code", "Name", "Market", "Date", "Marcap"]) for f in files if f.exists()])
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])]
    df = df[[_is_common_stock(c, n) for c, n in zip(df["Code"], df["Name"])]]
    return {d.date(): set(g.nlargest(top_n, "Marcap")["Code"]) for d, g in df.groupby("Date")}


def _next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def minute_signals(data: dict, top_uni: dict | None, start: date, end: date | None, m: MinuteSettings) -> list[dict]:
    """일봉으로 분봉 매매 대상 (종목, 매매일) 을 고른다.

    next: D 일 종가가 20일 첫 신고가 + 시총·가격·거래대금 조건 → D+1 일 매매 (warm-up 은 D 일 분봉)
    same: D 일 고가가 직전 20일 최고 종가 돌파(직전 20일 신고가 없음) + 일 거래대금 조건(장중 누적은 분봉에서 다시 확인)
          → D 일 매매 (warm-up 은 D-1 일 분봉). 시총·가격은 전날 기준.
    """
    out = []
    for code, (name, bars) in data.items():
        closes = [b.close for b in bars]
        flags = new_high_flags(closes, m.entry_days)
        amounts = [b.close * b.volume for b in bars]
        for i in range(m.entry_days + m.first_in_days, len(bars)):
            d = bars[i].day
            if d < start or (end and d > end):
                continue
            if any(flags[i - m.first_in_days:i]):
                continue
            prev_high = max(closes[i - m.entry_days:i])
            avg = sum(amounts[i - 20:i]) / 20
            if avg < m.min_avg_amount or amounts[i] < m.min_day_amount:
                continue
            if m.mode == "next":
                if not flags[i] or closes[i] > m.max_price:
                    continue
                if top_uni is not None and code not in top_uni.get(d, ()):
                    continue
                trade_day = bars[i + 1].day if i + 1 < len(bars) else _next_weekday(d)
                warm_day = d
            else:
                ref = bars[i - 1]
                if bars[i].high <= prev_high or ref.close > m.max_price:
                    continue
                if top_uni is not None and code not in top_uni.get(ref.day, ()):
                    continue
                trade_day, warm_day = d, ref.day
            out.append({"code": code, "name": name, "signal_day": d, "trade_day": trade_day, "warm_day": warm_day,
                        "prev_high": prev_high, "day_amount": amounts[i]})
    return sorted(out, key=lambda s: (s["trade_day"], s["code"]))


def load_minute(minute_dir: Path, code: str, d: date, tail: bool = False) -> list[MinuteBar] | None:
    path = minute_dir / (f"{code}_{d.isoformat()}_tail.csv" if tail else f"{code}_{d.isoformat()}.csv")
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        # 거래량 0 봉(09:00 장 시작 전, 15:21~15:30 동시호가 등 빈 봉)은 뺀다
        return [MinuteBar(r["time"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                          float(r["volume"])) for r in csv.DictReader(f) if float(r["volume"]) > 0]


def simulate_minute(sig: dict, warm: list[MinuteBar], day: list[MinuteBar], m: MinuteSettings) -> dict | None:
    """1분봉 이평선 눌림 지정가 매수 → 손절/익절/시각 정리. 하루 한 번만 매수.

    - 눌림: 직전 봉 종가가 이평선 위였고 이번 봉 저가가 이평선 이하 → 이평선 가격(시가가 이미 아래면 시가)에 매수
    - same 모드: 그 전에 장중 고가가 직전 20일 최고 종가를 넘었고, 누적 거래대금이 min_day_amount 이상이어야 함
    - 매수한 봉에서 저가가 손절가 이하면 손절로 가정 (봉 안의 순서를 모르므로 보수적), 익절은 다음 봉부터
    - 같은 봉에서 손절·익절 둘 다 닿으면 손절
    """
    closes = [b.close for b in warm] + [b.close for b in day]
    off = len(warm)
    broke = m.mode == "next"
    cum = 0.0
    entry = None
    for j, b in enumerate(day):
        k = off + j
        cum += b.close * b.volume
        if entry is None:
            if b.t >= m.exit_time:
                break
            if k < m.ma or b.t < m.entry_start:
                broke = broke or b.high > sig["prev_high"]
                continue
            ma_prev = sum(closes[k - m.ma:k]) / m.ma
            ma_now = (sum(closes[k - m.ma + 1:k]) + b.open) / m.ma  # 봉 진행 중 이평선 ≈ 이번 봉 시가 반영
            prev_close = closes[k - 1]
            ready = broke and (m.mode == "next" or cum - b.close * b.volume >= m.min_day_amount)
            if ready and j > 0 and prev_close > ma_prev and b.low <= ma_now:
                price = min(b.open, ma_now)
                entry = {"time": b.t, "price": price}
                stop = price * (1 - m.stop_loss / 100)
                target = price * (1 + m.take_profit / 100)
                if b.low <= stop:
                    return _minute_trade(sig, entry, b.t, stop * (1 - m.slippage), "STOP_LOSS", m)
            broke = broke or b.high > sig["prev_high"]
            continue
        if b.t >= m.exit_time:
            return _minute_trade(sig, entry, b.t, b.open * (1 - m.slippage), "TIME_EXIT", m)
        if b.open <= stop:
            return _minute_trade(sig, entry, b.t, b.open * (1 - m.slippage), "STOP_LOSS", m)
        if b.low <= stop:
            return _minute_trade(sig, entry, b.t, stop * (1 - m.slippage), "STOP_LOSS", m)
        if b.high >= target:
            return _minute_trade(sig, entry, b.t, max(b.open, target), "TAKE_PROFIT", m)
    if entry is not None:
        last = day[-1]
        return _minute_trade(sig, entry, last.t, last.close * (1 - m.slippage), "TIME_EXIT", m)
    return None


def _minute_trade(sig: dict, entry: dict, t: str, exit_price: float, reason: str, m: MinuteSettings) -> dict:
    tax = SELL_TAX_BY_YEAR.get(sig["trade_day"].year, 0.0020)
    ret = exit_price * (1 - m.commission - tax) / (entry["price"] * (1 + m.commission)) - 1
    return {"trade_day": sig["trade_day"].isoformat(), "code": sig["code"], "name": sig["name"],
            "entry_time": entry["time"], "entry_price": round(entry["price"], 1), "exit_time": t,
            "exit_price": round(exit_price, 1), "reason": reason, "ret_pct": round(ret * 100, 3)}


def run_minute(data: dict, top_uni: dict | None, start: date, end: date | None, m: MinuteSettings,
               minute_dir: Path) -> tuple[list[dict], list[dict], int]:
    """(신호 목록, 거래 목록, 분봉 없는 신호 수)."""
    sigs = minute_signals(data, top_uni, start, end, m)
    trades, missing = [], 0
    for s in sigs:
        warm = load_minute(minute_dir, s["code"], s["warm_day"])
        day = load_minute(minute_dir, s["code"], s["trade_day"])
        if warm is None or not day:
            missing += 1
            continue
        t = simulate_minute(s, warm, day, m)
        if t:
            trades.append(t)
    return sigs, trades, missing


def print_minute_report(sigs: list[dict], trades: list[dict], missing: int, m: MinuteSettings) -> None:
    rets = [t["ret_pct"] for t in trades]
    print(f"신호 {len(sigs)}건 (분봉 없음 {missing}) → 매수 {len(trades)}건")
    if not rets:
        return
    wins = sum(r > 0 for r in rets)
    reasons = Counter(t["reason"] for t in trades)
    print(f"승률 {wins / len(rets) * 100:.1f}%  거래당 평균 {sum(rets) / len(rets):+.2f}%  합계 {sum(rets):+.1f}%p  "
          f"({', '.join(f'{k} {v}' for k, v in reasons.most_common())})")
    by_month: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_month[t["trade_day"][:7]].append(t["ret_pct"])
    for mo, rs in sorted(by_month.items()):
        print(f"  {mo}: {len(rs)}건 평균 {sum(rs) / len(rs):+.2f}%")


def write_minute_days(sig_lists: list[list[dict]], out: Path) -> int:
    """분봉을 받아야 할 (종목, 날짜) 목록 CSV."""
    need = set()
    for sigs in sig_lists:
        for s in sigs:
            need.add((s["code"], s["warm_day"].isoformat()))
            need.add((s["code"], s["trade_day"].isoformat()))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "date"])
        w.writerows(sorted(need))
    return len(need)


# ---------------------------------------------------------------- 업종 동반 상승 → 다음 날 갭상승 눌림 (1분봉)
@dataclass
class SectorGapSettings:
    top_n: int = 50  # 전일 거래대금 상위 N
    min_sector_up: int = 3  # 같은 업종에서 상승 마감한 종목 수 하한
    pick: str = "sector"  # sector: 업종 동반 상승 / breadth: 상위 N 절반 이상 상승한 날 상승률·거래대금 상위 pick_n
    min_up_ratio: float = 0.5
    pick_n: int = 3
    take_profit: float = 5.0  # 평균 매입가 대비
    stop_loss: float = 0.0  # 0 이면 손절 없음
    first_weight: float = 0.5  # 1차(시가) 비중, 나머지는 2차(전일 종가). 1.0 이면 2차 없음
    max_gap: float = 0.0  # >0 이면 시가 갭상승이 이 % 이하인 날만
    stop_prev_close: bool = False  # 전일 종가 아래로 빠지면 손절
    buy_until: str = "15:00"
    exit_time: str = "15:15"
    commission: float = 0.00015
    slippage: float = 0.001


def load_sector_map(path: Path) -> dict[str, str]:
    with open(path, encoding="utf-8") as f:
        return {r["code"]: r["sector"] for r in csv.DictReader(f) if r.get("sector")}


def sector_gap_candidates(data: dict, start: date, end: date | None, top_n: int,
                          sectors: dict[str, str] | None = None, min_sector_up: int = 3, breadth: bool = False,
                          min_up_ratio: float = 0.5, pick_n: int = 3) -> list[dict]:
    """D 일 거래대금 상위 top_n 중 상승 마감 종목 → D+1 이 매매일. sectors 를 주면 같은 업종 상승 종목이
    min_sector_up 개 이상인 업종만. 다음 날 갭상승 여부는 일봉 시가로 판단(데이터 없으면 분봉에서)."""
    by_day: dict[date, list[tuple]] = defaultdict(list)
    for code, (name, bars) in data.items():
        for i in range(1, len(bars)):
            b = bars[i]
            if b.day < start or (end and b.day > end) or b.volume <= 0:
                continue
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            by_day[b.day].append((b.close * b.volume, code, name, b, bars[i - 1].close, nxt))
    out = []
    for d, rows in by_day.items():
        top = sorted(rows, key=lambda r: r[0], reverse=True)[:top_n]
        up = [r for r in top if r[3].close > r[4]]
        if breadth:
            # 상위 N 중 절반(min_up_ratio) 이상 상승 마감한 날만, 상승 종목 중 상승률 상위 pick_n + 거래대금 상위 pick_n
            if len(up) < min_up_ratio * len(top):
                continue
            by_chg = sorted(up, key=lambda r: r[3].close / r[4], reverse=True)[:pick_n]
            by_amt = sorted(up, key=lambda r: r[0], reverse=True)[:pick_n]
            up = list({r[1]: r for r in by_chg + by_amt}.values())
        elif sectors is not None:
            cnt = Counter(sectors.get(r[1]) for r in up if sectors.get(r[1]))
            up = [r for r in up if sectors.get(r[1]) and cnt[sectors[r[1]]] >= min_sector_up]
        for _, code, name, b, _, nxt in up:
            if nxt is not None and nxt.open <= b.close:
                continue  # 다음 날 갭상승 아님
            out.append({"code": code, "name": name, "signal_day": d, "trade_day": nxt.day if nxt else _next_weekday(d),
                        "prev_close": b.close, "sector": (sectors or {}).get(code, "")})
    return sorted(out, key=lambda s: (s["trade_day"], s["code"]))


def simulate_sector_gap(sig: dict, day: list[MinuteBar], g: SectorGapSettings) -> dict | None:
    """갭상승한 날: 1차 = 시가에 지정가(시가 이후 다시 시가까지 내려오면), 2차 = 전일 종가에 지정가.
    익절 = 평균 매입가 +take_profit% 지정가, buy_until 까지만 매수, exit_time 에 시장가 정리.
    봉 안의 순서는 모르므로: 기존 보유분의 익절·손절을 먼저 보고, 그다음 이 봉의 신규 체결. 신규 체결한 봉에서는 익절 안 함."""
    if not day:
        return None
    open_px, prev = day[0].open, sig["prev_close"]
    if open_px <= prev or (g.max_gap and (open_px / prev - 1) * 100 > g.max_gap):
        return None
    w1, w2 = g.first_weight, 1 - g.first_weight
    fills: list[tuple[str, float, float]] = []  # (시각, 가격, 비중)
    done1 = done2 = False
    exit_t = exit_px = reason = None
    for j, b in enumerate(day):
        if fills:
            avg = sum(w for _, _, w in fills) / sum(w / p for _, p, w in fills)  # 금액 가중 평균 매입가
            if b.t >= g.exit_time:
                exit_t, exit_px, reason = b.t, b.open * (1 - g.slippage), "TIME_EXIT"
                break
            stop = avg * (1 - g.stop_loss / 100) if g.stop_loss else None
            if g.stop_prev_close and b.low < prev:
                exit_t, exit_px, reason = b.t, min(b.open, prev) * (1 - g.slippage), "STOP_PREV_CLOSE"
                break
            if stop and b.low <= stop:
                exit_t, exit_px, reason = b.t, min(b.open, stop) * (1 - g.slippage), "STOP_LOSS"
                break
            target = avg * (1 + g.take_profit / 100)
            if b.high >= target:
                exit_t, exit_px, reason = b.t, max(b.open, target), "TAKE_PROFIT"
                break
        elif b.t >= g.buy_until:
            break
        if b.t < g.buy_until:
            if not done1 and j >= 1 and b.low <= open_px:
                fills.append((b.t, min(b.open, open_px), w1))
                done1 = True
            if not done2 and w2 > 0 and b.low <= prev:
                fills.append((b.t, min(b.open, prev), w2))
                done2 = True
    if not fills:
        return None
    if exit_t is None:
        exit_t, exit_px, reason = day[-1].t, day[-1].close * (1 - g.slippage), "TIME_EXIT"
    tax = SELL_TAX_BY_YEAR.get(sig["trade_day"].year, 0.0020)
    wsum = sum(w for _, _, w in fills)  # 비중 = 종목당 예산 중 쓴 돈의 비율
    shares = sum(w / (p * (1 + g.commission)) for _, p, w in fills)
    pnl = shares * exit_px * (1 - g.commission - tax) - wsum  # 종목당 예산 1 기준 손익
    return {"trade_day": sig["trade_day"].isoformat(), "code": sig["code"], "name": sig["name"], "sector": sig["sector"],
            "prev_close": prev, "open": open_px, "gap_pct": round((open_px / prev - 1) * 100, 2),
            "fills": "+".join(f"{t}@{p:.0f}" for t, p, _ in fills), "filled_weight": wsum,
            "exit_time": exit_t, "exit_price": round(exit_px, 1), "reason": reason,
            "ret_pct": round(pnl / wsum * 100, 3), "slot_ret_pct": round(pnl * 100, 3)}


def run_sector_gap(data: dict, sectors: dict, start: date, end: date | None, g: SectorGapSettings,
                   minute_dir: Path) -> tuple[list[dict], list[dict], int]:
    sigs = sector_gap_candidates(data, start, end, g.top_n, sectors, g.min_sector_up, g.pick == "breadth",
                                 g.min_up_ratio, g.pick_n)
    trades, missing = [], 0
    for s in sigs:
        day = load_minute(minute_dir, s["code"], s["trade_day"])
        if day is None:
            missing += 1
            continue
        t = simulate_sector_gap(s, day, g)
        if t:
            trades.append(t)
    return sigs, trades, missing


def print_sector_gap_report(sigs: list[dict], trades: list[dict], missing: int) -> None:
    print(f"후보 {len(sigs)}건 (분봉 없음 {missing}) → 매수 {len(trades)}건")
    if not trades:
        return
    rets = [t["ret_pct"] for t in trades]
    slot = [t["slot_ret_pct"] for t in trades]
    both = sum(t["filled_weight"] >= 0.999 for t in trades)
    reasons = Counter(t["reason"] for t in trades)
    print(f"승률 {sum(r > 0 for r in rets) / len(rets) * 100:.1f}%  산 금액 대비 평균 {sum(rets) / len(rets):+.2f}%  "
          f"종목당 예산 대비 평균 {sum(slot) / len(slot):+.2f}%  합계 {sum(slot):+.1f}%p  2차까지 체결 {both}건  "
          f"({', '.join(f'{k} {v}' for k, v in reasons.most_common())})")
    by_month: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_month[t["trade_day"][:7]].append(t["slot_ret_pct"])
    for mo, rs in sorted(by_month.items()):
        print(f"  {mo}: {len(rs)}건 예산 대비 평균 {sum(rs) / len(rs):+.2f}%")


def download_sector_info(codes: list[str], out: Path, env: str = ".env", request_interval: float = 0.15) -> None:
    """업종 확인용 원본 저장: 토스 종목 정보 + 네이버 모바일 종목 정보 (사용자 PC 에서 실행, 주문 없음)."""
    import json
    import time
    import urllib.request

    from .client import TossClient

    load_dotenv(env)
    cfg = Config.from_env()
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    raw: dict = {"toss": [], "naver": {}}
    try:
        raw["toss"] = client.get_stocks(codes)
    except Exception as exc:
        raw["toss_error"] = str(exc)
    for n, code in enumerate(codes, 1):
        for key, url in (("integration", f"https://m.stock.naver.com/api/stock/{code}/integration"),
                         ("basic", f"https://m.stock.naver.com/api/stock/{code}/basic")):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                raw["naver"].setdefault(code, {})[key] = json.loads(urllib.request.urlopen(req, timeout=10).read())
            except Exception as exc:
                raw["naver"].setdefault(code, {})[key] = {"error": str(exc)}
            time.sleep(request_interval)
        if n % 50 == 0:
            print(f"  업종 정보 {n} / {len(codes)}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    print(f"업종 정보 저장: {out}")


# ---------------------------------------------------------------- 5분봉 이평선 눌림 → 전고점 돌파 (1분봉 체결)
@dataclass
class PullBreakSettings:
    min_change: float = 5.0  # 매수 시점 전일 대비 상승률 하한 %
    max_price: float = 100_000  # 1주 가격 상한 (0 이면 제한 없음)
    min_cum_amount: float = 0  # 매수 시점 장중 누적 거래대금 하한
    ma: int = 20  # 5분봉 이평선
    band: tuple[int, int] | None = None  # (5, 22): 위 이평선 위에 있다가 저가가 두 이평선 사이로 들어오면 눌림
    band_allow_below: bool = False  # True 면 아래 이평선 밑으로 빠져도 눌림으로 인정
    aligned: tuple[int, ...] = ()  # (5, 10, 20): 눌림 봉에서 5 > 10 > 20 이평 정배열일 때만
    vwma: int = 0  # >0 이면 '전고점 돌파' 대신 n분봉 VWMA(이 개수) 터치 지정가 매수 (vwma_touch_events)
    bar_minutes: int = 5
    take_profit: float = 5.0
    buy_until: str = "14:59"
    exit_time: str = "15:00"
    max_trades: int = 5  # 하루 최대 매매 횟수
    max_consec_losses: int = 3  # 연속 손실 이 횟수면 그날 종료
    rt_top: int = 0  # >0 이면 매수 순간 장중 누적 거래대금 순위 이내만 (실시간 순위)
    stop_pct: float = 0.0  # >0 이면 손절 = 매수가 -N% (0 이면 눌림 저점 이탈)
    hold_bars: int = -1  # >=0 이면 손절 없이 매수 봉 + N 개 1분봉 뒤 종가에 매도 (그 사이 익절가 닿으면 익절)
    entry_bar_stop: str = "low"  # 매수한 1분봉 안 손절 판단: low(저가가 닿으면, 보수적) / close(종가가 손절가 이하일 때만)
    commission: float = 0.00015
    slippage: float = 0.001


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:])


def to_bars_n(bars: list[MinuteBar], n: int) -> list[MinuteBar]:
    """1분봉(시각 = 봉이 끝난 시각) → n분봉. 시각은 n분봉이 끝나는 시각."""
    out: list[MinuteBar] = []
    for b in bars:
        end = 540 + -(-(_minutes(b.t) - 540) // n) * n
        t = f"{end // 60:02d}:{end % 60:02d}"
        if out and out[-1].t == t:
            o = out[-1]
            out[-1] = MinuteBar(t, o.open, max(o.high, b.high), min(o.low, b.low), b.close, o.volume + b.volume)
        else:
            out.append(MinuteBar(t, b.open, b.high, b.low, b.close, b.volume))
    return out


def pullback_break_events(warm: list[MinuteBar], day: list[MinuteBar], prev_close: float,
                          s: PullBreakSettings) -> list[dict]:
    """한 종목·하루의 매수 신호 (시간순). 포지션과 무관하게 셋업만 본다.

    - n분봉 종가 이평선(전날 끝부분 포함) 위에 있던 봉 다음에 저가가 이평선 이하인 봉이 끝나면 '눌림'
    - 전고점 = 직전 신호(또는 장 시작) 이후 눌림 봉까지의 최고가, 손절가 = 눌림 이후 최저가
    - 눌림 뒤 1분봉 고가가 전고점을 넘으면 매수 (가격 = max(시가, 전고점) + 슬리피지)
    - 매수 시점 상승률 min_change% 이상, 누적 거래대금 min_cum_amount 이상일 때만
    """
    n = s.bar_minutes
    wb = to_bars_n(warm, n) if warm else []
    db = to_bars_n(day, n)
    closes = [b.close for b in wb]
    events = []
    leg_high = 0.0
    armed = False
    pivot = stop = 0.0
    j = 0  # day 1분봉 위치
    cum = 0.0
    for k, b in enumerate(db):
        end = _minutes(b.t)
        # 이 n분봉이 끝나기 전 1분봉들: 눌림 상태면 돌파 확인
        while j < len(day) and _minutes(day[j].t) <= end:
            m = day[j]
            cum += m.close * m.volume
            if armed and m.high > pivot and m.t <= s.buy_until:
                price = max(m.open, pivot) * (1 + s.slippage)
                if (price >= prev_close * (1 + s.min_change / 100) and cum >= s.min_cum_amount
                        and (not s.max_price or price <= s.max_price)):
                    events.append({"time": m.t, "idx": j, "price": price, "stop": stop, "pivot": pivot,
                                   "chg": (price / prev_close - 1) * 100})
                armed = False
                leg_high = m.high
            elif armed:
                stop = min(stop, m.low)
            leg_high = max(leg_high, m.high)
            j += 1
        closes.append(b.close)
        if s.band:
            fast, slow = s.band
            if len(closes) < slow + 1:
                continue
            up_now, lo_now = sum(closes[-fast:]) / fast, sum(closes[-slow:]) / slow
            up_prev = sum(closes[-fast - 1:-1]) / fast
            touched = (closes[-2] > up_prev and up_now >= lo_now and b.low <= up_now
                       and (s.band_allow_below or b.low >= lo_now))
        else:
            if len(closes) < s.ma + 1:
                continue
            ma_now = sum(closes[-s.ma:]) / s.ma
            ma_prev = sum(closes[-s.ma - 1:-1]) / s.ma
            touched = closes[-2] > ma_prev and b.low <= ma_now
        if touched and s.aligned:
            if len(closes) < max(s.aligned):
                touched = False
            else:
                mas = [sum(closes[-n:]) / n for n in s.aligned]
                touched = all(a > b2 for a, b2 in zip(mas, mas[1:]))
        if not armed and touched and leg_high > b.close:
            armed, pivot, stop = True, leg_high, b.low
    return events


def vwma_touch_events(warm: list[MinuteBar], day: list[MinuteBar], prev_close: float,
                      s: "PullBreakSettings") -> list[dict]:
    """n분봉 거래량가중이동평균(VWMA, s.vwma 개) 위에 있다가 1분봉 저가가 VWMA 에 닿으면 매수 (지정가 = VWMA,
    시가가 이미 아래면 시가). VWMA 는 직전에 끝난 n분봉까지로 계산(전날 분봉 포함), 개수가 모자라면 신호 없음.
    매수 순간 상승률 min_change% 이상·1주 max_price 이하만. 한 번 닿으면 VWMA 위로 다시 올라간 뒤에만 다음 신호."""
    n = s.bar_minutes
    bars = (to_bars_n(warm, n) if warm else [])
    pv = [b.close * b.volume for b in bars]
    vol = [b.volume for b in bars]
    db = to_bars_n(day, n)
    events = []
    j = 0
    above = False
    cum = 0.0
    for b in db:
        end = _minutes(b.t)
        vw = sum(pv[-s.vwma:]) / sum(vol[-s.vwma:]) if len(pv) >= s.vwma and sum(vol[-s.vwma:]) > 0 else None
        while j < len(day) and _minutes(day[j].t) <= end:
            m = day[j]
            cum += m.close * m.volume
            if vw is not None:
                if above and m.low <= vw and m.t <= s.buy_until:
                    price = min(m.open, vw)
                    if (price >= prev_close * (1 + s.min_change / 100) and cum >= s.min_cum_amount
                            and (not s.max_price or price <= s.max_price)):
                        events.append({"time": m.t, "idx": j, "price": price, "stop": 0.0, "pivot": round(vw, 1),
                                       "chg": (price / prev_close - 1) * 100})
                    above = False
                elif m.close > vw:
                    above = True
            j += 1
        pv.append(b.close * b.volume)
        vol.append(b.volume)
    return events


def _exit_trade(day: list[MinuteBar], ev: dict, s: PullBreakSettings) -> tuple[int, str, float, str]:
    """매수 후 청산: (청산 1분봉 위치, 시각, 가격, 사유). 매수 봉에서 손절가 닿으면 손절로 가정."""
    entry = ev["price"]
    stop = entry * (1 - s.stop_pct / 100) if s.stop_pct else ev["stop"]  # 0 이면 손절 없음
    target = ev.get("target") or entry * (1 + s.take_profit / 100)
    i0 = ev["idx"]
    if s.hold_bars >= 0:
        # 손절 없음: 매수 봉에서는 익절 판단 안 함(순서 모름), 이후 봉에서 익절가 닿으면 익절, 아니면 N 봉 뒤 종가
        last = min(i0 + s.hold_bars, len(day) - 1)
        for i in range(i0 + 1, last + 1):
            if day[i].high >= target:
                return i, day[i].t, max(day[i].open, target), "TAKE_PROFIT"
        return last, day[last].t, day[last].close * (1 - s.slippage), "TIME_EXIT"
    if (day[i0].close if s.entry_bar_stop == "close" else day[i0].low) <= stop:
        return i0, day[i0].t, min(stop, day[i0].close) * (1 - s.slippage), "STOP_LOSS"
    for i in range(i0 + 1, len(day)):
        b = day[i]
        if b.t >= s.exit_time:
            return i, b.t, b.open * (1 - s.slippage), "TIME_EXIT"
        if b.open <= stop or b.low <= stop:
            return i, b.t, min(b.open, stop) * (1 - s.slippage), "STOP_LOSS"
        if b.high >= target:
            return i, b.t, max(b.open, target), "TAKE_PROFIT"
    last = day[-1]
    return len(day) - 1, last.t, last.close * (1 - s.slippage), "TIME_EXIT"


def kelly_equity(trades: list[dict], scale: float, start: float = 1e6, window: int = 50, warmup: int = 20,
                 min_pct: float = 5.0, max_pct: float = 100.0, fixed_pct: float = 0.0) -> dict:
    """한 번에 한 종목씩 차례로 매매한 거래 목록에 비중을 입혀 계좌 곡선을 만든다.

    매매 금액 = 평가금액 x 비중. 비중 = 직전 window 건(이미 끝난 거래)의 켈리 비율 x scale (0.5 = 하프 켈리),
    min_pct ~ max_pct 로 제한. 끝난 거래가 warmup 건 미만이면 min_pct. fixed_pct > 0 이면 고정 비중.
    수량은 정수 주 (매매 금액 // (매수가 x (1+수수료))). 1주도 못 사면 건너뛴다 (켈리 기록에는 신호 결과로 남김).
    """
    equity, peak, mdd = start, start, 0.0
    n_traded = 0
    past: list[float] = []
    pcts = []
    by_month: dict[str, float] = {}
    for t in trades:
        if fixed_pct:
            pct = fixed_pct
        elif len(past) < warmup:
            pct = min_pct
        else:
            pct = min(max(kelly_fraction(past[-window:]) * scale * 100, min_pct), max_pct)
        pcts.append(pct)
        mo = t["trade_day"][:7]
        by_month.setdefault(mo, equity)
        unit = t["entry_price"] * 1.00015
        qty = int(equity * pct / 100 // unit)
        if qty > 0:
            equity += qty * unit * t["ret_pct"] / 100
            n_traded += 1
        past.append(t["ret_pct"])
        peak = max(peak, equity)
        mdd = min(mdd, equity / peak - 1)
    months = sorted(by_month)
    month_ret = {m: ((by_month[months[k + 1]] if k + 1 < len(months) else equity) / by_month[m] - 1) * 100
                 for k, m in enumerate(months)}
    return {"ret_pct": (equity / start - 1) * 100, "mdd_pct": mdd * 100, "avg_pct": sum(pcts) / len(pcts) if pcts else 0,
            "month_ret": month_ret, "traded": n_traded}


def realtime_amount_rank(minute_dir: Path, d: date, pool: list[str]) -> dict[str, list[int]]:
    """pool 종목들의 분별 장중 누적 거래대금 순위. {종목: [09:01 ~ 15:30 각 분의 순위(1부터)]}.
    m 분의 값은 그 분 직전까지(= m-1 분봉까지) 누적 기준 (매수 순간에 알 수 있는 값)."""
    n_min = 390
    cums: dict[str, list[float]] = {}
    for code in pool:
        bars = load_minute(minute_dir, code, d)
        if not bars:
            continue
        per = [0.0] * (n_min + 1)
        for b in bars:
            k = _minutes(b.t) - 540
            if 1 <= k <= n_min:
                per[k] += b.close * b.volume
        cum, acc = [0.0] * (n_min + 1), 0.0
        for k in range(1, n_min + 1):
            cum[k] = acc  # k 분봉이 끝나기 전까지
            acc += per[k]
        cums[code] = cum
    ranks: dict[str, list[int]] = {c: [0] * (n_min + 1) for c in cums}
    codes = list(cums)
    for k in range(1, n_min + 1):
        order = sorted(codes, key=lambda c: cums[c][k], reverse=True)
        for r, c in enumerate(order, 1):
            ranks[c][k] = r
    return ranks


def run_pullback_break(days: dict[date, list[dict]], minute_dir: Path, s: PullBreakSettings,
                       rank_pool: dict[date, list[str]] | None = None) -> list[dict]:
    """days: {매매일: [{code, name, prev_close, warm_day}]}. 하루에 한 번에 한 종목만, 최대 max_trades 번,
    연속 max_consec_losses 번 손실이면 그날 종료. 같은 시각 신호가 여럿이면 상승률 높은 종목.
    s.rt_top > 0 이면 매수 순간 장중 누적 거래대금 순위(rank_pool 종목 중)가 rt_top 이내인 신호만."""
    trades = []
    for d in sorted(days):
        events = []
        series = {}
        ranks = realtime_amount_rank(minute_dir, d, rank_pool.get(d, [])) if s.rt_top and rank_pool else None
        for c in days[d]:
            day = load_minute(minute_dir, c["code"], d)
            if not day:
                continue
            warm: list[MinuteBar] = []
            for wd in c.get("warm_days") or ([c["warm_day"]] if c.get("warm_day") else []):
                w = load_minute(minute_dir, c["code"], wd)
                if w is None:
                    w = load_minute(minute_dir, c["code"], wd, tail=True)
                warm += w or []
            series[c["code"]] = day
            finder = vwma_touch_events if s.vwma else pullback_break_events
            for ev in finder(warm, day, c["prev_close"], s):
                if ranks is not None:
                    k = _minutes(ev["time"]) - 540
                    rk = ranks.get(c["code"], [0] * 391)[min(max(k, 1), 390)]
                    if not rk or rk > s.rt_top:
                        continue
                    ev["rank"] = rk
                events.append({**ev, "code": c["code"], "name": c["name"]})
        events.sort(key=lambda e: (_minutes(e["time"]), -e["chg"]))
        free_at = -1
        n_trades = losses = 0
        for ev in events:
            if n_trades >= s.max_trades or losses >= s.max_consec_losses:
                break
            if _minutes(ev["time"]) <= free_at:
                continue
            day = series[ev["code"]]
            i, t, px, reason = _exit_trade(day, ev, s)
            tax = SELL_TAX_BY_YEAR.get(d.year, 0.0020)
            ret = px * (1 - s.commission - tax) / (ev["price"] * (1 + s.commission)) - 1
            trades.append({"trade_day": d.isoformat(), "code": ev["code"], "name": ev["name"],
                           "entry_time": ev["time"], "entry_price": round(ev["price"], 1),
                           "pivot": ev["pivot"], "stop": ev["stop"], "chg_pct": round(ev["chg"], 2),
                           "rt_rank": ev.get("rank", ""),
                           "exit_time": t, "exit_price": round(px, 1), "reason": reason,
                           "ret_pct": round(ret * 100, 3)})
            n_trades += 1
            losses = losses + 1 if ret <= 0 else 0
            free_at = _minutes(t)
    return trades


def pullback_break_days(data: dict, start: date, end: date | None, min_change: float,
                        min_day_amount: float, min_avg_amount: float = 0, top_amount: int = 0,
                        top_basis: str = "same") -> dict[date, list[dict]]:
    """일봉으로 대상 (종목, 날짜) 고르기: 그날 고가가 전일 대비 min_change% 이상, 거래대금 min_day_amount 이상,
    직전 20일 평균 거래대금 min_avg_amount 이상 (장중 조건의 상위 집합. 실제 매수 조건은 분봉에서 다시 확인)."""
    out: dict[date, list[dict]] = defaultdict(list)
    tops: dict[date, set[str]] = {}
    if top_amount:
        # 날짜별 거래대금 상위 top_amount. same: 매매일 당일(장 마감 뒤에야 확정 → 미래 정보 포함), prev: 전날
        by_day: dict[date, list[tuple[float, str]]] = defaultdict(list)
        for code, (_, bars) in data.items():
            for b in bars:
                by_day[b.day].append((b.close * b.volume, code))
        tops = {d: {c for _, c in sorted(v, reverse=True)[:top_amount]} for d, v in by_day.items()}
    for code, (name, bars) in data.items():
        amounts = [b.close * b.volume for b in bars]
        for i in range(1, len(bars)):
            b, p = bars[i], bars[i - 1]
            if b.day < start or (end and b.day > end) or b.volume <= 0:
                continue
            if min_avg_amount and (i < 20 or sum(amounts[i - 20:i]) / 20 < min_avg_amount):
                continue
            if top_amount and code not in tops.get(b.day if top_basis == "same" else p.day, ()):
                continue
            if b.high >= p.close * (1 + min_change / 100) and amounts[i] >= min_day_amount:
                out[b.day].append({"code": code, "name": name, "prev_close": p.close, "warm_day": p.day,
                                   "warm_days": [x.day for x in bars[max(0, i - 2):i]]})
    return out


# ---------------------------------------------------------------- 전일 급등주 다음 날 전일 종가/고점 돌파 (1분봉)
def surge_break_days(data: dict, start: date, end: date | None, min_prev_change: float = 15.0,
                     min_avg_amount: float = 3e9, max_price: float = 100_000,
                     max_prev_change: float = 0.0, min_prev_amount: float = 0.0,
                     cap_universe: dict | None = None) -> dict[date, list[dict]]:
    """전일(D) 상승률 min_prev_change% 이상(max_prev_change > 0 이면 그 미만까지) → D+1 이 매매일.
    직전 20일 평균 거래대금·1주 가격(D 종가)·D 거래대금(min_prev_amount) 조건."""
    out: dict[date, list[dict]] = defaultdict(list)
    for code, (name, bars) in data.items():
        amounts = [b.close * b.volume for b in bars]
        for i in range(21, len(bars)):
            p, d = bars[i - 1], bars[i]
            if d.close < p.close * (1 + min_prev_change / 100) or d.volume <= 0:
                continue
            if max_prev_change and d.close >= p.close * (1 + max_prev_change / 100):
                continue
            if sum(amounts[i - 20:i]) / 20 < min_avg_amount or (max_price and d.close > max_price):
                continue
            if amounts[i] < min_prev_amount:
                continue
            if cap_universe is not None and code not in cap_universe.get(d.day, ()):
                continue  # 기준봉 날 시가총액 조건 (--min-marcap)
            trade_day = bars[i + 1].day if i + 1 < len(bars) else _next_weekday(d.day)
            if trade_day < start or (end and trade_day > end):
                continue
            out[trade_day].append({"code": code, "name": name, "prev_close": d.close, "prev_high": d.high,
                                   "prev_open": d.open, "prev_low": d.low,
                                   "prev_change": (d.close / p.close - 1) * 100})
    return out


def quarter_levels(c: dict, entry_frac: float = 0.75, stop_frac: float = 0.5, basis: str = "body") -> tuple[float, float]:
    """기준봉(전일)을 4등분한 매수가·손절가. body = 시가~종가, range = 저가~고가. frac 은 아래에서부터 비율."""
    lo, hi = (c["prev_low"], c["prev_high"]) if basis == "range" else (c["prev_open"], c["prev_close"])
    return lo + (hi - lo) * entry_frac, lo + (hi - lo) * stop_frac


def _gap_skip(open_: float, level: float, stop: float, skip_gap: str) -> bool:
    """skip_gap: stop = 시가가 손절가 이하면 매수 안 함 / entry = 시가가 매수가 아래(갭하락으로 이미 밑)면 매수 안 함."""
    return (skip_gap == "stop" and open_ <= stop) or (skip_gap == "entry" and open_ < level)


def quarter_event(day: list[MinuteBar], c: dict, s: "PullBreakSettings", entry_frac: float = 0.75,
                  stop_frac: float = 0.5, basis: str = "body", skip_gap: str = "", tp_frac: float = 0.0) -> dict | None:
    """기준봉 다음 날 entry_frac 지점 지정가 매수 (시가가 이미 아래면 시가에 체결), 손절가 = stop_frac 지점.
    tp_frac > 0 이면 익절가 = 그 지점 (아니면 매수가 +take_profit%). skip_gap 은 _gap_skip 참고."""
    level, stop = quarter_levels(c, entry_frac, stop_frac, basis)
    if day and _gap_skip(day[0].open, level, stop, skip_gap):
        return None
    target = quarter_levels(c, tp_frac, 0, basis)[0] if tp_frac else 0.0
    for j, m in enumerate(day):
        if m.t > s.buy_until:
            return None
        if m.low <= level:
            price = min(m.open, level)
            return {"time": m.t, "idx": j, "price": price, "stop": stop, "pivot": level, "target": target,
                    "chg": (price / c["prev_close"] - 1) * 100}
    return None


def quarter_daily_trade(bar: "Bar", c: dict, s: "PullBreakSettings", entry_frac: float = 0.75,
                        stop_frac: float = 0.5, basis: str = "body", optimistic: bool = False,
                        skip_gap: str = "", tp_frac: float = 0.0) -> dict | None:
    """1분봉 없이 일봉으로 근사. 저가가 손절가에 닿으면 손절(익절보다 먼저로 가정).
    익절: 시가 체결이면 고가가 익절가 이상일 때. 장중 체결이면 고가가 매수 전에 나왔을 수 있어서
    optimistic 일 때만 고가로, 아니면 종가가 익절가 이상일 때만 인정. 나머지는 종가 청산(15:00 대신).
    익절 없이(take_profit 아주 크게) 종가 청산이면 일봉으로도 정확함 (매수가를 지나야 손절가에 닿으므로)."""
    level, stop = quarter_levels(c, entry_frac, stop_frac, basis)
    if bar.low > level or _gap_skip(bar.open, level, stop, skip_gap):
        return None
    at_open = bar.open <= level
    price = bar.open if at_open else level
    target = quarter_levels(c, tp_frac, 0, basis)[0] if tp_frac else price * (1 + s.take_profit / 100)
    if bar.low <= stop:
        return {"price": price, "exit": min(bar.open, stop) * (1 - s.slippage), "reason": "STOP_LOSS"}
    if bar.high >= target and (at_open or optimistic or bar.close >= target):
        return {"price": price, "exit": target, "reason": "TAKE_PROFIT"}
    return {"price": price, "exit": bar.close * (1 - s.slippage), "reason": "TIME_EXIT"}


def surge_break_event(day: list[MinuteBar], c: dict, mode: str, s: "PullBreakSettings",
                      stop_daylow: bool = False, retest: bool = False) -> dict | None:
    """mode prevclose: 시가가 전일 종가 아래(갭상승 제외)에서 시작해 전일 종가를 넘는 순간 매수.
    mode prevhigh: 시가가 전일 고가 아래에서 시작해 전일 고가를 넘는 순간 매수.
    가격 = max(그 1분봉 시가, 기준가) + 슬리피지. stop_daylow 면 손절가 = 매수 전까지 당일 최저가.
    retest: 돌파 순간이 아니라, 돌파 뒤 1분봉 종가가 기준가 위에서 끝난 다음 다시 기준가까지 내려오면
    기준가 지정가 매수 (시가가 이미 아래면 시가)."""
    if not day:
        return None
    level = c["prev_close"] if mode == "prevclose" else c["prev_high"]
    if day[0].open >= level:
        return None  # 갭상승으로 이미 넘은 날 제외
    low = day[0].low
    if retest:
        broke = held = False
        for j, m in enumerate(day):
            if m.t > s.buy_until:
                return None
            if held and m.low <= level:
                price = min(m.open, level)
                return {"time": m.t, "idx": j, "price": price, "stop": low if stop_daylow else 0.0, "pivot": level,
                        "chg": (price / c["prev_close"] - 1) * 100}
            if broke and m.close > level:
                held = True
            if m.high > level:
                broke = True
                held = held or m.close > level
            low = min(low, m.low)
        return None
    for j, m in enumerate(day):
        if m.t > s.buy_until:
            return None
        if m.high > level and (j > 0 or m.open < level):
            price = max(m.open, level) * (1 + s.slippage)
            return {"time": m.t, "idx": j, "price": price, "stop": low if stop_daylow else 0.0, "pivot": level,
                    "chg": (price / c["prev_close"] - 1) * 100}
        low = min(low, m.low)
    return None


def run_surge_break(days: dict[date, list[dict]], minute_dir: Path, mode: str, s: "PullBreakSettings",
                    stop_daylow: bool = False, retest: bool = False, q: dict | None = None,
                    daily: dict | None = None) -> tuple[list[dict], int]:
    """종목·날짜마다 한 번씩 (서로 독립). (거래 목록, 분봉 없는 대상 수).
    mode quarter: q = quarter_event 인자. daily = {(code, day): Bar} 를 주면 1분봉 대신 일봉 근사(quarter_daily_trade)."""
    trades, missing = [], 0
    q = q or {}
    for d in sorted(days):
        for c in days[d]:
            if daily is not None:
                bar = daily.get((c["code"], d))
                if bar is None:
                    missing += 1
                    continue
                r = quarter_daily_trade(bar, c, s, **q)
                if r:
                    tax = SELL_TAX_BY_YEAR.get(d.year, 0.0020)
                    ret = r["exit"] * (1 - s.commission - tax) / (r["price"] * (1 + s.commission)) - 1
                    trades.append({"trade_day": d.isoformat(), "code": c["code"], "name": c["name"],
                                   "prev_change": round(c["prev_change"], 1), "level": round(r["price"], 1),
                                   "entry_time": "", "entry_price": round(r["price"], 1), "exit_time": "",
                                   "exit_price": round(r["exit"], 1), "reason": r["reason"],
                                   "ret_pct": round(ret * 100, 3)})
                continue
            day = load_minute(minute_dir, c["code"], d)
            if day is None:
                missing += 1
                continue
            if mode == "quarter":
                ev = quarter_event(day, c, s, **q)
            else:
                ev = surge_break_event(day, c, mode, s, stop_daylow, retest)
            if not ev:
                continue
            i, t, px, reason = _exit_trade(day, ev, s)
            tax = SELL_TAX_BY_YEAR.get(d.year, 0.0020)
            ret = px * (1 - s.commission - tax) / (ev["price"] * (1 + s.commission)) - 1
            trades.append({"trade_day": d.isoformat(), "code": c["code"], "name": c["name"],
                           "prev_change": round(c["prev_change"], 1), "level": round(ev["pivot"], 1),
                           "entry_time": ev["time"], "entry_price": round(ev["price"], 1),
                           "exit_time": t, "exit_price": round(px, 1), "reason": reason,
                           "ret_pct": round(ret * 100, 3)})
    trades.sort(key=lambda t: (t["trade_day"], t["entry_time"]))
    return trades, missing


def download_toss_minute(days_file: Path, out_dir: Path, env: str = ".env", request_interval: float = 0.12) -> None:
    """days_file(code,date) 의 정규장(09:00~15:30) 1분봉을 토스 API 로 받는다. 이미 받은 날은 건너뜀. 주문 없음."""
    import time
    from datetime import datetime

    from .client import TossClient
    from .config import KST

    load_dotenv(env)
    cfg = Config.from_env()
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(days_file, encoding="utf-8") as f:
        # pages 열(선택): 1 이면 그날 끝 200분만 (이평선 계산용 전날), 파일 이름 끝에 _tail
        need = [(r["code"], r["date"], int(r.get("pages") or 4)) for r in csv.DictReader(f)]
    today = datetime.now(KST).date().isoformat()

    def path_of(c: str, d: str, pages: int) -> Path:
        return out_dir / (f"{c}_{d}.csv" if pages >= 4 else f"{c}_{d}_tail.csv")

    todo = [(c, d, p) for c, d, p in need if d < today and not path_of(c, d, p).exists()
            and not (out_dir / f"{c}_{d}.csv").exists()]
    print(f"분봉 받기: {len(need)}개 중 {len(todo)}개 (종목×날짜)", flush=True)

    def candles(symbol: str, before: str) -> list[dict]:
        time.sleep(request_interval)
        params = {"symbol": symbol, "interval": "1m", "count": 200, "adjusted": "true", "before": before}
        return (client._request("GET", "/api/v1/candles", params=params) or {}).get("candles", [])

    failed = []
    for n, (code, d, pages) in enumerate(todo, 1):
        rows: dict[str, tuple] = {}
        before = f"{d}T15:30:00.000+09:00"
        try:
            for _ in range(pages):  # 200분씩 최대 4번 (정규장 390분)
                raw = candles(code, before)
                for c in raw:
                    ts = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).astimezone(KST)
                    if ts.date().isoformat() == d and "09:00" <= ts.strftime("%H:%M") <= "15:30":
                        rows[ts.strftime("%H:%M")] = (ts.strftime("%H:%M"), c["openPrice"], c["highPrice"],
                                                       c["lowPrice"], c["closePrice"], c["volume"])
                if not raw or "09:00" in rows:
                    break
                before = min(c["timestamp"] for c in raw)
        except Exception as exc:
            failed.append(f"{code}_{d}")
            log.warning("%s %s 실패: %s", code, d, exc)
            continue
        with open(path_of(code, d, pages), "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["time", "open", "high", "low", "close", "volume"])
            w.writerows(rows[k] for k in sorted(rows))
        if n % 20 == 0 or n == len(todo):
            print(f"  {n} / {len(todo)}", flush=True)
    if failed:
        print(f"실패 {len(failed)}개: {failed[:20]} (다시 실행하면 이어받음)")
    # 이 목록의 파일만 4MB 이하 zip 여러 개로 (구글 드라이브 연결은 큰 파일을 받다가 끊김, 10MB 한도)
    files = sorted({path_of(c, d, p) for c, d, p in need if path_of(c, d, p).exists()}
                   | {out_dir / f"{c}_{d}.csv" for c, d, _ in need if (out_dir / f"{c}_{d}.csv").exists()})
    parts = split_zip(files, out_dir.parent / f"{out_dir.name}_{days_file.stem}")
    print(f"완료. 묶음 파일 {len(parts)}개: " + ", ".join(str(p) for p in parts))
    print("이 파일들을 모두 구글 드라이브에 올려 주세요.")


def split_zip(files: list[Path], prefix: Path, limit: int = 4_000_000) -> list[Path]:
    """files 를 압축 후 크기 limit 이하 zip 여러 개(prefix_1.zip, prefix_2.zip ...)로 묶는다."""
    import zipfile

    for old in prefix.parent.glob(prefix.name + "_*.zip"):
        old.unlink()
    parts: list[Path] = []
    zf = None
    size = 0
    for f in files:
        data = f.read_bytes()
        if zf is None or size > limit - 200_000:
            if zf:
                zf.close()
            parts.append(prefix.parent / f"{prefix.name}_{len(parts) + 1}.zip")
            zf = zipfile.ZipFile(parts[-1], "w", zipfile.ZIP_DEFLATED)
            size = 0
        zf.writestr(f.name, data)
        size += zf.getinfo(f.name).compress_size + 100
    if zf:
        zf.close()
    return parts


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
    ds = sub.add_parser("download-sector", help="업종 확인용 종목 정보 받기 (토스 + 네이버, .env 필요, 주문 없음)")
    ds.add_argument("--days", type=Path, default=Path("reports/sector_gap/minute_days.csv"),
                    help="이 목록(code,date)에 나오는 종목")
    ds.add_argument("--out", type=Path, default=MINUTE_DIR / "_sector_raw.json")
    ds.add_argument("--env", default=".env")
    sg = sub.add_parser("sectorgap", help="1분봉: 전일 거래대금 상위·업종 동반 상승 → 다음 날 갭상승 시가/전일 종가 눌림 매수")
    sg.add_argument("--start", default="2026-06-30")
    sg.add_argument("--end", default=None)
    sg.add_argument("--cache", type=Path, default=Path("data/ohlcv_long"))
    sg.add_argument("--minute-dir", type=Path, default=MINUTE_DIR)
    sg.add_argument("--sector-file", type=Path, default=Path("reports/sector_gap/sectors.csv"),
                    help="code,sector CSV. 없으면 업종 조건 없이")
    sg.add_argument("--no-sector", action="store_true", help="업종 조건 없이 (상위 N 상승 마감 전체)")
    sg.add_argument("--top", type=int, default=50)
    sg.add_argument("--min-sector-up", type=int, default=3)
    sg.add_argument("--pick", choices=["sector", "breadth"], default="sector",
                    help="breadth: 상위 N 중 절반 이상 상승 마감한 날, 상승 종목의 상승률 상위·거래대금 상위 --pick-n 개씩")
    sg.add_argument("--min-up-ratio", type=float, default=0.5)
    sg.add_argument("--pick-n", type=int, default=3)
    sg.add_argument("--take-profit", type=float, default=5.0)
    sg.add_argument("--stop-loss", type=float, default=0.0)
    sg.add_argument("--first-weight", type=float, default=0.5, help="1.0 이면 시가 1차만")
    sg.add_argument("--max-gap", type=float, default=0.0, help="갭상승 이 %% 이하인 날만")
    sg.add_argument("--stop-prev-close", action="store_true", help="전일 종가 아래로 빠지면 손절")
    sg.add_argument("--buy-until", default="15:00")
    sg.add_argument("--exit-time", default="15:15")
    sg.add_argument("--trades-out", type=Path, default=None)
    sg.add_argument("--write-days", type=Path, default=None, help="받아야 할 (종목, 날짜) 목록만 쓰고 끝냄 (업종 조건 없이)")
    pb = sub.add_parser("pullbreak", help="1분봉: 5%%↑ 종목 n분봉 이평선 눌림 뒤 전고점 돌파 매수")
    pb.add_argument("--start", default="2026-07-01")
    pb.add_argument("--end", default=None)
    pb.add_argument("--cache", type=Path, default=Path("data/ohlcv_long"))
    pb.add_argument("--minute-dir", type=Path, default=MINUTE_DIR)
    pb.add_argument("--min-change", type=float, default=5.0)
    pb.add_argument("--max-price", type=float, default=100_000, help="1주 가격 상한 (0: 제한 없음)")
    pb.add_argument("--start-capital", type=float, default=1e6, help="시작 금액 (비중·정수 주 계산)")
    pb.add_argument("--min-amount", type=float, default=0, help="장중 누적 거래대금 하한 (일봉 사전 필터도 같은 값)")
    pb.add_argument("--min-avg-amount", type=float, default=3e9, help="직전 20일 평균 거래대금 하한")
    pb.add_argument("--top-amount", type=int, default=50, help="거래대금 상위 N 종목만 (0: 제한 없음)")
    pb.add_argument("--rt-top", type=int, default=0,
                    help="매수 순간 장중 누적 거래대금 순위 N 이내만 (순위는 그날 상위 --pool-top 종목 안에서 계산)")
    pb.add_argument("--pool-top", type=int, default=50, help="--rt-top 순위 계산에 쓰는 종목 수 (그날 거래대금 상위)")
    pb.add_argument("--top-basis", choices=["same", "prev"], default="same",
                    help="same: 매매일 당일 순위(마감 뒤 확정, 미래 정보) / prev: 전날 순위")
    pb.add_argument("--ma", type=int, default=20)
    pb.add_argument("--band", type=int, nargs=2, default=None, metavar=("FAST", "SLOW"),
                    help="예: 5 22 → 5이평 위에 있다가 저가가 5~22이평 사이에 닿으면 눌림 (--ma 대신)")
    pb.add_argument("--band-allow-below", action="store_true", help="--band 아래 이평선 밑으로 빠져도 눌림 인정")
    pb.add_argument("--aligned", type=int, nargs="+", default=[], help="예: 5 10 20 → 눌림 봉에서 이평선 정배열일 때만")
    pb.add_argument("--vwma", type=int, default=0, help="N>0: 전고점 돌파 대신 n분봉 거래량가중이동평균 N 터치 시 매수")
    pb.add_argument("--bar-minutes", type=int, default=5)
    pb.add_argument("--take-profit", type=float, default=5.0)
    pb.add_argument("--stop-pct", type=float, default=0.0, help="손절 = 매수가 -N%% (0: 눌림 저점 이탈)")
    pb.add_argument("--hold-bars", type=int, default=-1,
                    help="0 이상이면 손절 없이 매수 1분봉 + N 개 뒤 종가에 매도 (0 = 매수한 그 1분봉 종가)")
    pb.add_argument("--entry-bar-stop", choices=["low", "close"], default="low",
                    help="매수한 1분봉 안 손절: low(저가가 닿으면, 보수적) / close(그 봉 종가가 손절가 이하일 때만)")
    pb.add_argument("--buy-until", default="14:59")
    pb.add_argument("--exit-time", default="15:00")
    pb.add_argument("--max-trades", type=int, default=5)
    pb.add_argument("--max-consec-losses", type=int, default=3)
    pb.add_argument("--trades-out", type=Path, default=None)
    pb.add_argument("--write-days", type=Path, default=None,
                    help="받아야 할 목록만 쓰고 끝냄 (매매일은 하루 전체, 전날은 끝 200분)")
    sb = sub.add_parser("surgebreak", help="1분봉: 전일 급등주가 다음 날 전일 종가(아래에서)·전일 고가를 넘을 때 매수")
    sb.add_argument("--mode", choices=["prevclose", "prevhigh", "quarter"], default="prevclose",
                    help="quarter = 기준봉(전일)을 4등분해 3/4 지점 지정가 매수, 1/2 지점 이탈 손절 (--stop-pct 0 과 함께)")
    sb.add_argument("--min-prev-amount", type=float, default=0.0, help="전일(기준봉) 거래대금 하한 (원)")
    sb.add_argument("--q-entry", type=float, default=0.75, help="quarter: 매수 지점 (아래에서부터 비율)")
    sb.add_argument("--q-stop", type=float, default=0.5, help="quarter: 손절 지점 (아래에서부터 비율)")
    sb.add_argument("--q-basis", choices=["body", "range"], default="body",
                    help="quarter: 4등분 기준 body = 시가~종가 / range = 저가~고가")
    sb.add_argument("--q-skip-gap", nargs="?", const="stop", default="", choices=["stop", "entry"],
                    help="quarter: 갭하락 매수 금지. stop(기본) = 시가가 손절 지점 이하 / entry = 시가가 매수 지점 아래")
    sb.add_argument("--q-tp", type=float, default=0.0,
                    help="quarter: 익절 지점 (아래에서부터 비율, 예 0.75). 0 이면 --take-profit %%")
    sb.add_argument("--daily", action="store_true",
                    help="quarter: 1분봉 대신 일봉으로 근사 (손절 먼저, 장중 체결 뒤 익절은 종가가 익절가 이상일 때만)")
    sb.add_argument("--optimistic", action="store_true", help="quarter --daily: 장중 체결이어도 고가로 익절 인정")
    sb.add_argument("--start", default="2026-07-01")
    sb.add_argument("--end", default=None)
    sb.add_argument("--cache", type=Path, default=Path("data/ohlcv_long"))
    sb.add_argument("--minute-dir", type=Path, default=MINUTE_DIR)
    sb.add_argument("--min-prev-change", type=float, default=15.0, help="전일 상승률 하한 %%")
    sb.add_argument("--max-prev-change", type=float, default=0.0, help="전일 상승률 상한 %% (미만, 0: 없음)")
    sb.add_argument("--min-marcap", type=float, default=0.0, help="기준봉 날 시가총액 하한 (예: 1e12, 0: 없음)")
    sb.add_argument("--marcap-dir", type=Path, default=Path("data/marcap/data"))
    sb.add_argument("--min-avg-amount", type=float, default=3e9)
    sb.add_argument("--max-price", type=float, default=100_000)
    sb.add_argument("--stop-pct", type=float, default=2.0, help="손절 = 매수가 -N%% (0: 없음)")
    sb.add_argument("--stop-daylow", action="store_true", help="손절 = 매수 전까지 당일 최저가 (--stop-pct 0 과 함께)")
    sb.add_argument("--retest", action="store_true", help="돌파 순간 대신, 돌파 뒤 기준가로 되돌아오면 지정가 매수")
    sb.add_argument("--take-profit", type=float, default=5.0)
    sb.add_argument("--entry-bar-stop", choices=["low", "close"], default="low")
    sb.add_argument("--buy-until", default="14:59")
    sb.add_argument("--exit-time", default="15:15")
    sb.add_argument("--trades-out", type=Path, default=None)
    sb.add_argument("--write-days", type=Path, default=None, help="받아야 할 (종목, 날짜) 목록만 추가하고 끝냄")
    dm = sub.add_parser("download-minute", help="토스 API 로 목록(code,date)의 1분봉 받기 (.env 필요, 주문 없음)")
    dm.add_argument("--days", type=Path, default=Path("reports/minute_pullback/minute_days.csv"))
    dm.add_argument("--out", type=Path, default=MINUTE_DIR)
    dm.add_argument("--env", default=".env")
    mn = sub.add_parser("minute", help="1분봉 단타 백테스트: 일봉 20일 첫 신고가 → 1분봉 이평선 눌림 매수")
    mn.add_argument("--mode", choices=["next", "same"], default="next",
                    help="next: 신고가 다음 날 매매 / same: 장중 신고가 돌파 당일 매매")
    mn.add_argument("--start", default="2026-07-01")
    mn.add_argument("--end", default=None)
    mn.add_argument("--cache", type=Path, default=Path("data/ohlcv_long"))
    mn.add_argument("--marcap-dir", type=Path, default=Path("data/marcap/data"))
    mn.add_argument("--minute-dir", type=Path, default=MINUTE_DIR)
    mn.add_argument("--top-universe", type=int, default=100, help="0: 시총 조건 없음")
    mn.add_argument("--max-price", type=float, default=99_999)
    mn.add_argument("--min-day-amount", type=float, default=2e11)
    mn.add_argument("--min-avg-amount", type=float, default=0)
    mn.add_argument("--ma", type=int, default=200)
    mn.add_argument("--stop-loss", type=float, default=1.0)
    mn.add_argument("--take-profit", type=float, default=3.0)
    mn.add_argument("--exit-time", default="15:10")
    mn.add_argument("--trades-out", type=Path, default=None, help="거래 내역 CSV")
    mn.add_argument("--write-days", type=Path, default=None,
                    help="분봉을 받아야 할 (종목, 날짜) 목록만 이 파일에 추가하고 끝냄 (next·same 둘 다)")
    pm = sub.add_parser("probe-minute", help="토스 API 로 국내 분봉을 어디까지 받을 수 있는지 확인 (.env 필요, 저장·주문 없음)")
    pm.add_argument("--out", type=Path, default=Path("data/_minute_probe.txt"))
    pm.add_argument("--env", default=".env")
    u = sub.add_parser("universe", help="실전 RSI 전략 대상 목록 (코스피 시가총액 상위 N) 파일 만들기")
    u.add_argument("--marcap-dir", type=Path, default=Path("data/marcap/data"))
    u.add_argument("--top", type=int, default=100)
    u.add_argument("--out", type=Path, default=Path("tossbot/lists/kospi_top100.txt"))
    r = sub.add_parser("run", help="연도별 백테스트 실행")
    r.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    r.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    r.add_argument("--out", type=Path, default=Path("backtest_results"))
    r.add_argument("--env", default=".env")
    r.add_argument("--strategy", choices=["pullback", "breakout", "limitup", "surgedoji", "retest", "mapullback", "volbreak", "rsi", "combo", "macross", "dbottom", "engulfretest"], default="pullback",
                   help="pullback: 급등 후 이평선 터치 / breakout: 종가 N일 신고가 매수·M일 신저가 매도 / "
                        "limitup: 전일 상한가 종목 시초가 매수 / "
                        "surgedoji: 거래대금 상위 +15%% 급등 후 거래량 급감 단봉 음봉 종가 매수 / "
                        "retest: 급등 + N일 신고가 기준봉 이후 이전 고점까지 되돌리면 지정가 매수")
    r.add_argument("--surge-pct", type=float, default=15, help="retest: 기준봉 상승률 하한 %%")
    r.add_argument("--surge-max-pct", type=float, default=0, help="retest: 기준봉 상승률 상한 %% (0 = 없음)")
    r.add_argument("--retest-level", choices=["high", "close"], default="high",
                   help="retest: 이전 고점 = 직전 N일 장중 고가 최고값(high) / 종가 최고값(close)")
    r.add_argument("--rsi-period", type=int, default=14, help="rsi: RSI 기간")
    r.add_argument("--rsi-buy", type=float, default=30, help="rsi: RSI 가 N 아래로 마감하면 매수")
    r.add_argument("--rsi-sell", type=float, default=50, help="rsi: RSI 가 N 이상으로 마감하면 매도 (0 = 없음)")
    r.add_argument("--rsi-trend-ma", type=int, default=0, help="rsi: 종가가 N일선 위인 종목만 매수 (0 = 없음)")
    r.add_argument("--rsi-exit-ma", type=int, default=0, help="rsi: 종가가 N일선 위로 마감하면 매도 (0 = 없음)")
    r.add_argument("--universe-marcap", type=float, default=0,
                   help="rsi: 날짜별 시가총액 N원 이상 종목만 (코스피·코스닥, --marcap-dir 필요, 0 = 없음)")
    r.add_argument("--combo-priority", choices=["breakout", "rsi", "mix"], default="breakout",
                   help="combo: 신고가+RSI 한 계좌 운용 시 빈 자리 채우는 순서")
    r.add_argument("--top-universe", type=int, default=0,
                   help="rsi: 코스피 시가총액 상위 N 종목만 (코스피200 근사, --marcap-dir 필요, 0 = 전체)")
    r.add_argument("--vb-k", type=float, default=0.5, help="volbreak: 목표가 = 시가 + 전일 변동폭 x k")
    r.add_argument("--vb-ma", type=int, default=0, help="volbreak: 전일 종가가 N일선 위인 종목만 (0 = 없음)")
    r.add_argument("--vb-exit", choices=["open", "close"], default="open",
                   help="volbreak: open = 다음 날 시가 매도 / close = 당일 종가 매도")
    r.add_argument("--vb-top", type=int, default=0,
                   help="volbreak: 전일 코스피 시가총액 상위 N 종목만 (코스피200 근사, --marcap-dir 필요, 0 = 전체)")
    r.add_argument("--vb-min-range", type=float, default=0.0, help="volbreak: 전일 변동폭 N%% 이상만")
    r.add_argument("--mp-entry", choices=["ma", "bear"], default="ma",
                   help="mapullback: ma = 이평선 눌림 지정가 매수 / bear = 기준봉 뒤 첫 음봉 종가 매수")
    r.add_argument("--bear-max", type=float, default=3.0, help="mapullback bear: 첫 음봉 하락폭 N%% 이내만 매수")
    r.add_argument("--bear-measure", choices=["body", "change"], default="body",
                   help="mapullback bear: 하락폭 기준 body = 시가 대비 / change = 전일 종가 대비")
    r.add_argument("--mp-pick", choices=["", "amount", "change", "both", "combo"], default="",
                   help="mapullback: 하루 기준봉 1종목만 (amount 거래대금 1위 / change 상승률 1위 / both 둘 다 1위 / combo 순위 합)")
    r.add_argument("--bear-min-ma", type=int, default=0,
                   help="mapullback bear: 첫 음봉 종가가 N일선 아래면 매수 안 함 (0 = 조건 없음)")
    r.add_argument("--no-above-ma", action="store_true", help="mapullback: 기준봉 종가 이평선 위 조건 끄기")
    r.add_argument("--rsi-second", type=int, default=0,
                   help="rsi: N > 0 이면 직전 N거래일 안에 과매도가 한 번 있었고 다시 과매도로 들어온 날만 매수")
    r.add_argument("--rsi-second-reset", type=float, default=0.0, help="rsi: 두 과매도 사이 RSI 가 이 값 이상 회복 (0 = --rsi-buy)")
    r.add_argument("--rsi-diverge", action="store_true", help="rsi: 두 번째 종가 < 첫 최저 종가, RSI > 첫 최저 RSI 일 때만")
    r.add_argument("--er-ma", type=int, default=20, help="engulfretest: 하락 추세 = 음봉 종가가 N일선 아래 + N일선 하락")
    r.add_argument("--er-close-in-body", action="store_true",
                   help="engulfretest: 음봉 시가까지 내려온 날 종가가 음봉 몸통 안일 때만 그날 종가 매수")
    r.add_argument("--er-low-in-body", action="store_true",
                   help="engulfretest: 음봉 시가까지 내려온 날 저가가 음봉 몸통 안(음봉 종가 이상)일 때만 그날 종가 매수")
    r.add_argument("--er-drop-below-body", action="store_true",
                   help="engulfretest: 대기 중 저가가 음봉 몸통 아래로 빠지면 그 종목 제외")
    r.add_argument("--er-body", type=float, default=2.0, help="engulfretest: 양봉 몸통이 음봉 몸통의 N배 이상")
    r.add_argument("--db-days", type=int, default=60, help="dbottom: 하락 추세 = N거래일 최저가 + N일선 하락")
    r.add_argument("--db-drop", type=float, default=0.0, help="dbottom: 250거래일 최고가 대비 N%% 이상 하락한 저점만 (0 = 조건 없음)")
    r.add_argument("--db-amount-mult", type=float, default=0.0,
                   help="dbottom: 반등일·매수일 거래대금이 직전 20거래일 평균의 N배 이상 (0 = 조건 없음)")
    r.add_argument("--db-bounce", type=float, default=10.0, help="dbottom: 반등 고점이 첫 저점 대비 최소 N%%")
    r.add_argument("--db-near", type=float, default=5.0, help="dbottom: 눌림이 첫 저점 ±N%% 안")
    r.add_argument("--db-pullback-wait", type=int, default=60, help="dbottom: 반등 뒤 N거래일 안에 눌림")
    r.add_argument("--db-entry-wait", type=int, default=20, help="dbottom: 눌림 저점 뒤 N거래일 안에 두 번째 거래대금")
    r.add_argument("--db-upside", type=float, default=5.0, help="dbottom: 매수가 대비 반등 고점까지 최소 여유 %%")
    r.add_argument("--mc-ma", type=int, default=50, help="macross: 이평선 기간")
    r.add_argument("--mc-slope", type=int, default=5, help="macross: 기울기 비교 거래일 수")
    r.add_argument("--mc-lookback", type=int, default=120, help="macross: 전고점 = 돌파 전 N거래일 최고 고가")
    r.add_argument("--mc-upside", type=float, default=5.0, help="macross: 매수가 대비 전고점까지 최소 여유 %%")
    r.add_argument("--mc-target", choices=["before", "since"], default="before",
                   help="macross: 전고점 before = 돌파 전 --mc-lookback 일 최고가 / since = 돌파일~매수 전날 최고가")
    r.add_argument("--mc-ma-exit", type=float, default=0.0, help="macross: 종가가 이평선보다 N%% 아래면 종가 매도 (0 = 없음)")
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
    r.add_argument("--npattern", choices=["", "A", "B", "C", "D"], default="",
                   help="breakout: N자 패턴 (A = 눌림 저점 뒤 첫 양봉 매수 / B = 1차 고점 돌파 매수 / "
                        "C = 기준봉(--np-amount·--np-rise) 뒤 --np-lookback 일 안에 기준봉 저가를 안 깨고 같은 봉 재출현 시 종가 매수 / "
                        "D = 기준봉 뒤 종가가 기준봉 종가 아래, 몸통 절반 이하 눌림 뒤 시가 안 깨고 거래량 증가 양봉 종가 매수)")
    r.add_argument("--np-dip-ref", choices=["low", "close"], default="low", help="breakout N자 D: 절반 눌림 판정 가격")
    r.add_argument("--np-break-ref", choices=["low", "close"], default="low", help="breakout N자 D: 기준봉 시가 이탈 판정 가격")
    r.add_argument("--np-volup-avg", type=int, default=0,
                   help="breakout N자 D: 거래량 증가 기준 (0 = 전날보다 많음, N = N일 평균보다 많음)")
    r.add_argument("--np-base-ref", choices=["low", "open"], default="low", help="breakout N자 C: 깨지 말아야 할 기준봉 가격")
    r.add_argument("--np-min-gap", type=int, default=2, help="breakout N자 C: 기준봉과 매수일 최소 간격 (거래일)")
    r.add_argument("--np-quiet", choices=["", "any", "all", "last"], default="",
                   help="breakout N자 C: 기준봉 뒤~매수 전날 거래량이 평균 이하인 날이 하루 이상(any)/모든 날(all)/매수 전날(last)")
    r.add_argument("--np-vol-avg", type=int, default=20, help="breakout N자 C: 거래량 평균 기간 (거래일)")
    r.add_argument("--np-vol-basis", choices=["rolling", "base"], default="rolling",
                   help="breakout N자 C: 평균 = 그날까지 이동평균(rolling) / 기준봉 전날까지 평균(base)")
    r.add_argument("--np-base-ma", type=int, default=0, help="breakout N자 C: 기준봉이 N일선 부근인 것만 (0 = 없음)")
    r.add_argument("--np-base-ma-pct", type=float, default=5.0, help="breakout N자 C: N일선 부근 범위 ±%%")
    r.add_argument("--np-base-ma-mode", choices=["open", "cross", "low"], default="open",
                   help="breakout N자 C: open = 기준봉 시가가 N일선 ±%% / cross = 시가 <= N일선(+%%) < 종가 / low = 저가가 N일선 ±%%")
    r.add_argument("--np-any-base", action="store_true",
                   help="breakout N자 C: 기준봉 앞에 같은 조건 봉이 있어도 허용 (기본: 기준봉이 첫 번째여야 함)")
    r.add_argument("--np-lookback", type=int, default=20, help="breakout N자: 1차 고점·상승 탐색 기간 (거래일)")
    r.add_argument("--np-rise", type=float, default=15.0, help="breakout N자: 1차 상승폭 하한 %%")
    r.add_argument("--np-pull", type=float, nargs=2, default=[5.0, 15.0], metavar=("MIN", "MAX"),
                   help="breakout N자: 눌림 깊이 범위 %% (고점 대비)")
    r.add_argument("--np-amount", type=float, default=2e10, help="breakout N자: 1차 상승 구간 거래대금 하한 (원)")
    r.add_argument("--engulf", action="store_true",
                   help="breakout: 상승 장악형만 (전일 음봉을 당일 양봉 몸통이 감쌈)")
    r.add_argument("--below-ma", type=int, default=0, help="breakout: 당일 종가가 N일선 아래인 종목만 (0 = 없음)")
    r.add_argument("--cross-ma", type=int, default=0,
                   help="breakout: N일선 돌파일만 (전일 종가 <= N일선, 당일 종가 > N일선, 0 = 없음)")
    r.add_argument("--max-amount-rank", type=int, default=0,
                   help="breakout: 신호 당일 거래대금 순위 N위 이내 종목만 (0 = 없음)")
    r.add_argument("--rank-by", choices=["amount", "change", "strength", "weak", "calm"], default="amount",
                   help="breakout: 신호가 많을 때 우선순위 (거래대금 / 당일 상승률 / 신고가 돌파폭)")
    r.add_argument("--delay-max-rise", type=float, default=0, help="breakout: 대기 중 신고가 종가 대비 N%% 이상 상승 시 매수 취소")
    r.add_argument("--delay-hold-open", action="store_true", help="breakout: 대기 중 신고가 봉 시가 아래로 내려가면 매수 취소")
    r.add_argument("--delay-intraday", action="store_true", help="breakout: 대기 조건을 종가 대신 고가·저가로 판정")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.command == "universe":
        write_universe_file(args.marcap_dir, args.top, args.out)
        return
    if args.command == "download-sector":
        with open(args.days, encoding="utf-8") as f:
            codes = sorted({r["code"] for r in csv.DictReader(f)})
        download_sector_info(codes, args.out, args.env)
        return
    if args.command == "sectorgap":
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else None
        data = load_cache(args.cache)
        if args.write_days:
            sigs = sector_gap_candidates(data, start, end, args.top)
            n = write_minute_days([[{"code": s["code"], "warm_day": s["trade_day"], "trade_day": s["trade_day"]}
                                    for s in sigs]], args.write_days)
            print(f"후보 {len(sigs)}건 → 받을 종목×날짜 {n}개: {args.write_days}")
            return
        sectors = None if args.no_sector or args.pick == "breadth" or not args.sector_file.exists() \
            else load_sector_map(args.sector_file)
        if sectors is None and args.pick == "sector":
            print("업종 조건 없음")
        g = SectorGapSettings(top_n=args.top, min_sector_up=args.min_sector_up, pick=args.pick,
                              min_up_ratio=args.min_up_ratio, pick_n=args.pick_n, take_profit=args.take_profit,
                              stop_loss=args.stop_loss, first_weight=args.first_weight, buy_until=args.buy_until,
                              max_gap=args.max_gap, stop_prev_close=args.stop_prev_close,
                              exit_time=args.exit_time)
        sigs, trades, missing = run_sector_gap(data, sectors, start, end, g, args.minute_dir)
        print_sector_gap_report(sigs, trades, missing)
        if args.trades_out and trades:
            args.trades_out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.trades_out, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0]))
                w.writeheader()
                w.writerows(trades)
        return
    if args.command == "pullbreak":
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else None
        data = load_cache(args.cache)
        rank_pool = None
        if args.rt_top:
            # 후보 = 그날 거래대금 상위 pool_top (실시간 상위 rt_top 은 거의 여기에 포함), 순위는 분봉으로 매 분 계산
            days = pullback_break_days(data, start, end, args.min_change, args.min_amount, args.min_avg_amount,
                                       args.pool_top, "same")
            by_day: dict[date, list[tuple[float, str]]] = defaultdict(list)
            for code, (_, bars) in data.items():
                for b in bars:
                    if b.day >= start and (not end or b.day <= end):
                        by_day[b.day].append((b.close * b.volume, code))
            rank_pool = {d: [c for _, c in sorted(v, reverse=True)[:args.pool_top]] for d, v in by_day.items()}
        else:
            days = pullback_break_days(data, start, end, args.min_change, args.min_amount, args.min_avg_amount,
                                       args.top_amount, args.top_basis)
        if args.write_days:
            need: dict[tuple[str, str], int] = {}
            if args.write_days.exists():  # 기존 목록에 합침
                with open(args.write_days, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        need[(r["code"], r["date"])] = int(r.get("pages") or 4)
            for d, codes in (rank_pool or {}).items():
                for c in codes:
                    need[(c, d.isoformat())] = 4
            for d, rows in days.items():
                for r in rows:
                    need[(r["code"], d.isoformat())] = 4
                    key = (r["code"], r["warm_day"].isoformat())
                    # VWMA 100(5분봉 500분)은 전날 하루치 + 그 전날 끝부분이 필요
                    need[key] = max(need.get(key, 1), 4 if args.vwma else 1)
                    if args.vwma:
                        for wd in r["warm_days"][:-1]:
                            k2 = (r["code"], wd.isoformat())
                            need[k2] = max(need.get(k2, 1), 1)
            args.write_days.parent.mkdir(parents=True, exist_ok=True)
            with open(args.write_days, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["code", "date", "pages"])
                w.writerows((c, d, p) for (c, d), p in sorted(need.items()))
            print(f"대상 {sum(len(v) for v in days.values())}건 → 받을 종목×날짜 {len(need)}개 "
                  f"(하루 전체 {sum(p == 4 for p in need.values())}, 전날 끝부분 {sum(p == 1 for p in need.values())})")
            return
        s = PullBreakSettings(min_change=args.min_change, max_price=args.max_price, min_cum_amount=args.min_amount, ma=args.ma,
                              band=tuple(args.band) if args.band else None, band_allow_below=args.band_allow_below,
                              aligned=tuple(args.aligned), rt_top=args.rt_top, stop_pct=args.stop_pct,
                              entry_bar_stop=args.entry_bar_stop, hold_bars=args.hold_bars, vwma=args.vwma,
                              bar_minutes=args.bar_minutes, take_profit=args.take_profit, buy_until=args.buy_until,
                              exit_time=args.exit_time, max_trades=args.max_trades,
                              max_consec_losses=args.max_consec_losses)
        trades = run_pullback_break(days, args.minute_dir, s, rank_pool)
        rets = [t["ret_pct"] for t in trades]
        print(f"매수 {len(trades)}건", end="")
        if rets:
            reasons = Counter(t["reason"] for t in trades)
            print(f"  승률 {sum(r > 0 for r in rets) / len(rets) * 100:.1f}%  거래당 평균 {sum(rets) / len(rets):+.2f}%  "
                  f"합계 {sum(rets):+.1f}%p  ({', '.join(f'{k} {v}' for k, v in reasons.most_common())})")
        else:
            print()
        if trades:
            for label, kw in (("매번 전액", {"fixed_pct": 100}), ("고정 10%", {"fixed_pct": 10}),
                              ("풀 켈리", {"scale": 1.0}), ("하프 켈리", {"scale": 0.5})):
                e = kelly_equity(trades, kw.get("scale", 1.0), start=args.start_capital, fixed_pct=kw.get("fixed_pct", 0))
                months = " / ".join(f"{m[5:]}월 {r:+.1f}%" for m, r in e["month_ret"].items())
                print(f"  {label}: 계좌 {e['ret_pct']:+.1f}% (MDD {e['mdd_pct']:.1f}%, 평균 비중 {e['avg_pct']:.0f}%, "
                      f"실제 매수 {e['traded']}건) | {months}")
        if args.trades_out and trades:
            args.trades_out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.trades_out, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0]))
                w.writeheader()
                w.writerows(trades)
        return
    if args.command == "surgebreak":
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else None
        data = load_cache(args.cache)
        caps = (load_marcap_universe(args.marcap_dir, (start - timedelta(days=10)).isoformat(), args.min_marcap)
                if args.min_marcap else None)
        days = surge_break_days(data, start, end, args.min_prev_change, args.min_avg_amount, args.max_price,
                                args.max_prev_change, args.min_prev_amount, caps)
        q = {"entry_frac": args.q_entry, "stop_frac": args.q_stop, "basis": args.q_basis, "skip_gap": args.q_skip_gap,
             "tp_frac": args.q_tp}
        if args.write_days:
            need: dict[tuple[str, str], int] = {}
            if args.write_days.exists():
                with open(args.write_days, encoding="utf-8") as f:
                    for r in csv.DictReader(f):
                        need[(r["code"], r["date"])] = int(r.get("pages") or 4)
            # 일봉으로 그날 기준가를 넘을 수 있었던 날만 (시가 < 기준가 < 고가). 일봉이 없는 날(최근)은 포함
            daily = {(c, b.day): b for c, (_, bars) in data.items() for b in bars}
            n_add = 0
            for d, rows in days.items():
                for r in rows:
                    b = daily.get((r["code"], d))
                    if args.mode == "quarter":
                        if b is not None and b.low > quarter_levels(r, **q)[0]:
                            continue  # 매수가까지 안 내려온 날
                    else:
                        level = r["prev_close"] if args.mode == "prevclose" else r["prev_high"]
                        if b is not None and not (b.open < level < b.high):
                            continue
                    need[(r["code"], d.isoformat())] = 4
                    n_add += 1
            args.write_days.parent.mkdir(parents=True, exist_ok=True)
            with open(args.write_days, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(["code", "date", "pages"])
                w.writerows((c, d, p) for (c, d), p in sorted(need.items()))
            print(f"대상 {sum(len(v) for v in days.values())}건 중 {args.mode} 가능 {n_add}건 → 목록 {len(need)}개: "
                  f"{args.write_days}")
            return
        s = PullBreakSettings(take_profit=args.take_profit, stop_pct=args.stop_pct, buy_until=args.buy_until,
                              exit_time=args.exit_time, entry_bar_stop=args.entry_bar_stop)
        daily = None
        if args.daily:
            daily = {(c, b.day): b for c, (_, bars) in data.items() for b in bars}
        qq = dict(q, optimistic=args.optimistic) if args.daily else q
        trades, missing = run_surge_break(days, args.minute_dir, args.mode, s, args.stop_daylow, args.retest,
                                          qq if args.mode == "quarter" else None, daily)
        rets = [t["ret_pct"] for t in trades]
        print(f"대상 {sum(len(v) for v in days.values())}건 (분봉 없음 {missing}) → 매수 {len(trades)}건", end="")
        if rets:
            reasons = Counter(t["reason"] for t in trades)
            print(f"  승률 {sum(r > 0 for r in rets) / len(rets) * 100:.1f}%  거래당 평균 {sum(rets) / len(rets):+.2f}%  "
                  f"({', '.join(f'{k} {v}' for k, v in reasons.most_common())})")
        else:
            print()
        if args.trades_out and trades:
            args.trades_out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.trades_out, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0]))
                w.writeheader()
                w.writerows(trades)
        return
    if args.command == "download-minute":
        download_toss_minute(args.days, args.out, args.env)
        return
    if args.command == "minute":
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end) if args.end else None
        data = load_cache(args.cache)
        top_uni = load_top_universe_all(args.marcap_dir, args.start, args.top_universe) if args.top_universe else None
        m = MinuteSettings(mode=args.mode, top_universe=args.top_universe, max_price=args.max_price,
                           min_day_amount=args.min_day_amount, min_avg_amount=args.min_avg_amount, ma=args.ma,
                           stop_loss=args.stop_loss, take_profit=args.take_profit, exit_time=args.exit_time)
        if args.write_days:
            lists = [minute_signals(data, top_uni, start, end, dataclasses.replace(m, mode=mode))
                     for mode in ("next", "same")]
            if args.write_days.exists():
                with open(args.write_days, encoding="utf-8") as f:
                    old = [{"code": r["code"], "warm_day": date.fromisoformat(r["date"]),
                            "trade_day": date.fromisoformat(r["date"])} for r in csv.DictReader(f)]
                lists.append(old)
            n = write_minute_days(lists, args.write_days)
            print(f"신호 next {len(lists[0])} / same {len(lists[1])}건 → 받을 종목×날짜 누적 {n}개: {args.write_days}")
            return
        sigs, trades, missing = run_minute(data, top_uni, start, end, m, args.minute_dir)
        print_minute_report(sigs, trades, missing, m)
        if args.trades_out:
            args.trades_out.parent.mkdir(parents=True, exist_ok=True)
            with open(args.trades_out, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0]) if trades else ["trade_day"])
                w.writeheader()
                w.writerows(trades)
        return
    if args.command == "probe-minute":
        probe_toss_minute(args.out, args.env)
        return
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
                              entry_delay=args.entry_delay, delay_max_rise_pct=args.delay_max_rise, rank_by=args.rank_by, max_amount_rank=args.max_amount_rank, cross_ma=args.cross_ma, engulf=args.engulf, below_ma=args.below_ma,
                              npattern=args.npattern, np_lookback=args.np_lookback, np_rise_pct=args.np_rise,
                              np_min_pull=args.np_pull[0], np_max_pull=args.np_pull[1], np_amount=args.np_amount,
                              np_base_ref=args.np_base_ref, np_min_gap=args.np_min_gap, np_first_base=not args.np_any_base,
                              np_quiet=args.np_quiet, np_vol_avg=args.np_vol_avg, np_vol_basis=args.np_vol_basis,
                              np_base_ma=args.np_base_ma, np_base_ma_pct=args.np_base_ma_pct, np_base_ma_mode=args.np_base_ma_mode,
                              np_dip_ref=args.np_dip_ref, np_break_ref=args.np_break_ref, np_volup_avg=args.np_volup_avg,
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
    elif args.strategy == "combo":
        # 신고가(실전 규칙, 종목당 예산 고정) + RSI(시총 상위 --top-universe, 기본 100) 를 한 계좌로
        cb = BreakoutSettings(stop_loss_pct=args.stop_loss or 4.7, take_profit_pct=args.take_profit or 20.0,
                              exit_on_low=False, first_in_days=args.first_in_days or 20,
                              min_day_amount=args.min_day_amount or 2e10, skip_touched_limit_up=True)
        cr = RsiSettings(period=args.rsi_period, buy_below=args.rsi_buy, sell_above=args.rsi_sell, stop_loss_pct=10,
                         max_hold_days=args.max_hold or 20, max_price=args.max_price)
        cr.universe, _ = load_top_universe(args.marcap_dir, f"{min(args.years) - 1}-12-01", top_n=args.top_universe or 100)
        runner = lambda y0, y1: run_shared(data, y0, y1, settings, cb, cr, args.combo_priority)  # noqa: E731
    elif args.strategy == "rsi":
        rs = RsiSettings(period=args.rsi_period, buy_below=args.rsi_buy, sell_above=args.rsi_sell,
                         trend_ma=args.rsi_trend_ma, exit_ma=args.rsi_exit_ma, stop_loss_pct=args.stop_loss,
                         max_hold_days=args.max_hold, min_amount=args.min_day_amount, max_price=args.max_price,
                         second_within=args.rsi_second, second_reset=args.rsi_second_reset,
                         second_diverge=args.rsi_diverge)
        if args.top_universe:
            rs.universe, _ = load_top_universe(args.marcap_dir, f"{min(args.years) - 1}-12-01", top_n=args.top_universe)
        elif args.universe_marcap:
            rs.universe = load_marcap_universe(args.marcap_dir, f"{min(args.years) - 1}-12-01", args.universe_marcap)
        runner = lambda y0, y1: run_rsi(data, y0, y1, settings, rs)  # noqa: E731
    elif args.strategy == "volbreak":
        vb = VolBreakoutSettings(k=args.vb_k, min_amount=args.min_day_amount or 2e10, ma_filter=args.vb_ma,
                                 stop_loss_pct=args.stop_loss, exit=args.vb_exit, min_range_pct=args.vb_min_range)
        if args.vb_top:
            vb.universe, _ = load_top_universe(args.marcap_dir, f"{min(args.years) - 1}-12-01", top_n=args.vb_top)
        runner = lambda y0, y1: run_vol_breakout(data, y0, y1, settings, vb)  # noqa: E731
    elif args.strategy == "engulfretest":
        ers = EngulfRetestSettings(ma_period=args.er_ma, body_mult=args.er_body, min_amount=args.min_day_amount,
                                   watch_days=args.watch_days or 10, take_profit_pct=args.take_profit or 20.0,
                                   max_hold_days=args.max_hold, close_in_body=args.er_close_in_body,
                                   low_in_body=args.er_low_in_body, drop_below_body=args.er_drop_below_body)
        runner = lambda y0, y1: run_engulf_retest(data, y0, y1, settings, ers)  # noqa: E731
    elif args.strategy == "dbottom":
        dbs = DoubleBottomSettings(downtrend_days=args.db_days, drop_pct=args.db_drop,
                                   min_amount=args.min_day_amount or 1e10, min_change_pct=args.min_change or 0.0,
                                   amount_mult=args.db_amount_mult, min_bounce_pct=args.db_bounce, near_pct=args.db_near,
                                   pullback_wait=args.db_pullback_wait, entry_wait=args.db_entry_wait,
                                   min_upside_pct=args.db_upside, max_hold_days=args.max_hold)
        runner = lambda y0, y1: run_double_bottom(data, y0, y1, settings, dbs)  # noqa: E731
    elif args.strategy == "macross":
        mc = MaCrossSettings(ma_period=args.mc_ma, slope_days=args.mc_slope, min_amount=args.min_day_amount or 1e10,
                             watch_days=args.watch_days, high_lookback=args.mc_lookback, min_upside_pct=args.mc_upside,
                             max_break_pct=args.max_break, stop_loss_pct=args.stop_loss, max_hold_days=args.max_hold,
                             ma_exit_pct=args.mc_ma_exit, target=args.mc_target)
        runner = lambda y0, y1: run_ma_cross(data, y0, y1, settings, mc)  # noqa: E731
    elif args.strategy == "mapullback":
        mp = MaPullbackSettings(surge_pct=args.surge_pct, min_amount=args.min_day_amount or 2e11,
                                first_in_days=args.first_in_days, ma_period=args.pullback_ma,
                                watch_days=args.watch_days, max_break_pct=args.max_break,
                                stop_loss_pct=args.stop_loss if args.ma_exit else (args.stop_loss or 5.0),
                                take_profit_pct=args.take_profit if args.ma_exit else (args.take_profit or 20.0),
                                pick=args.mp_pick, ma_exit_days=args.ma_exit, bear_min_ma=args.bear_min_ma,
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
