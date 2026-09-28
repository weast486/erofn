"""눌림목 매매 종목 선정: 최근 급등(하루 +10% 이상)한 종목이 7일선 부근까지 눌리면 매수.

1) 감시 목록 (하루 한 번, 장 시작 후 첫 주기에 작성)
   후보: 상승률 상위 랭킹(1일·1주·1개월, 각 100위, 투자유의 제외) + 최근 봇이 본 급등 종목
   조건:
     - 최근 SURGE_LOOKBACK_DAYS 거래일 안에 하루 +SURGE_PCT% 이상 오른 날(급등일)이 있음
     - 급등일로부터 MIN_DAYS_AFTER_SURGE 거래일 이상 지남 (급등 직후엔 7일선이 급등 전 가격 근처라
       7일선 터치 = 급등분 전부 반납이 되므로 제외)
     - 거래량 줄어든 눌림: 급등 이후 평균 거래량 <= 급등일 거래량 x PULLBACK_VOLUME_RATIO
     - 급등분을 다 반납하지 않음: 전일 종가 > 급등 전날 종가
     - 전일 종가가 7일선 위: 오늘 위에서 내려와 7일선에 닿는 경우만 잡기 위함
     - 7일선 상승 중 (REQUIRE_MA_RISING): 전일 7일선 >= 5거래일 전 7일선
     - 20일 평균 거래대금 >= MIN_AVG_TRADING_AMOUNT (유동성)
     - 1주 가격 <= 종목당 예산, 거래 가능 보통주

2) 매수 신호 (장중 매 주기)
   실시간 7일선 = (직전 6거래일 종가 합 + 현재가) / 7
   현재가 <= 실시간 7일선 이면 '7일선 터치'로 보고 매수.
   단, 7일선보다 MA_MAX_BREAK_PCT% 넘게 아래면 이미 이탈한 것으로 보고 매수하지 않는다
   (감시 주기 사이에 선을 살짝 뚫고 내려간 경우까지만 터치로 인정).
   동시에 여러 종목이 신호를 내면 급등폭이 큰 순서로 빈 자리만큼 매수.

※ 어떤 규칙도 수익을 보장하지 않는다. DRY_RUN 으로 충분히 검증한 뒤 사용할 것.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime

from .config import KST

log = logging.getLogger(__name__)


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PullbackParams:
    slot_budget: float = 100_000
    surge_pct: float = 10.0
    surge_lookback_days: int = 10
    ma_period: int = 7
    ma_max_break_pct: float = 1.5
    min_days_after_surge: int = 3
    pullback_volume_ratio: float = 0.5
    require_ma_rising: bool = True
    min_avg_trading_amount: float = 3_000_000_000


@dataclass
class WatchItem:
    symbol: str
    name: str
    surge_date: date
    surge_pct: float
    pre_surge_close: float  # 급등 전날 종가
    prev_closes_sum: float  # 직전 (ma_period - 1) 거래일 종가 합 (실시간 이평 계산용)
    ma_period: int = 7

    def live_ma(self, price: float) -> float:
        return (self.prev_closes_sum + price) / self.ma_period


def parse_candles(raw: list[dict]) -> list[Bar]:
    bars = [
        Bar(
            day=datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).astimezone(KST).date(),
            open=float(c["openPrice"]),
            high=float(c["highPrice"]),
            low=float(c["lowPrice"]),
            close=float(c["closePrice"]),
            volume=float(c["volume"]),
        )
        for c in raw
    ]
    bars.sort(key=lambda b: b.day)
    return bars


def _sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def analyze(symbol: str, name: str, bars: list[Bar], today: date, p: PullbackParams) -> tuple[WatchItem | None, str]:
    """완성된 일봉(오늘 제외)으로 감시 목록 편입 여부 판단. (항목, 탈락 사유)."""
    bars = [b for b in bars if b.day < today]
    n = p.ma_period
    if len(bars) < max(p.surge_lookback_days, n + 5, 20) + 1:
        return None, "데이터 부족"
    closes = [b.close for b in bars]

    surge_idx = None
    for i in range(len(bars) - 1, len(bars) - 1 - p.surge_lookback_days, -1):
        if closes[i] / closes[i - 1] - 1 >= p.surge_pct / 100:
            surge_idx = i
            break
    if surge_idx is None:
        return None, f"최근 {p.surge_lookback_days}일 내 +{p.surge_pct:g}% 급등 없음"
    pre_surge_close = closes[surge_idx - 1]
    surge_pct = closes[surge_idx] / pre_surge_close - 1
    # 오늘은 급등일로부터 (완성된 급등 이후 봉 수 + 1) 거래일째
    days_after = len(bars) - surge_idx
    if days_after < p.min_days_after_surge:
        return None, f"급등 후 {days_after}일째 (최소 {p.min_days_after_surge}일)"
    after_vol = [b.volume for b in bars[surge_idx + 1:]]
    if after_vol and sum(after_vol) / len(after_vol) > bars[surge_idx].volume * p.pullback_volume_ratio:
        return None, "눌림 구간 거래량 과다"

    if closes[-1] <= pre_surge_close:
        return None, "급등분 모두 반납"
    if closes[-1] <= _sma(closes, n):
        return None, f"전일 종가가 {n}일선 아래 (이미 터치/이탈)"
    if p.require_ma_rising and _sma(closes, n) < _sma(closes[:-5], n):
        return None, f"{n}일선 하락 중"
    avg_amount = sum(b.close * b.volume for b in bars[-20:]) / 20
    if avg_amount < p.min_avg_trading_amount:
        return None, "거래대금 부족"
    if closes[-1] > p.slot_budget * 1.1:
        return None, "1주 가격이 종목당 예산 초과"

    item = WatchItem(
        symbol=symbol,
        name=name,
        surge_date=bars[surge_idx].day,
        surge_pct=surge_pct,
        pre_surge_close=pre_surge_close,
        prev_closes_sum=sum(closes[-(n - 1):]),
        ma_period=n,
    )
    return item, ""


def entry_signal(item: WatchItem, price: float, p: PullbackParams) -> tuple[bool, float]:
    """현재가가 실시간 이평선에 닿았으면(이하) True. 단 이평선보다 max_break 넘게 아래면 False. (신호, 실시간 이평)."""
    ma = item.live_ma(price)
    floor = ma * (1 - p.ma_max_break_pct / 100)
    # 부동소수점 오차로 '정확히 터치'가 빠지지 않도록 아주 작은 허용오차
    ok = floor <= price <= ma * (1 + 1e-9) and price > item.pre_surge_close and price <= p.slot_budget
    return ok, ma


def touch_zone(item: WatchItem, p: PullbackParams) -> tuple[float, float]:
    """entry_signal 이 참이 되는 현재가 구간 [lo, hi] (백테스트용).

    실시간 이평 = (S + x) / n 이므로
      x <= (S + x) / n            ⇔ x <= S / (n - 1)
      x >= (1 - b)(S + x) / n     ⇔ x >= (1 - b) S / (n - 1 + b)
    """
    n, s, b = item.ma_period, item.prev_closes_sum, p.ma_max_break_pct / 100
    return (1 - b) * s / (n - 1 + b), s / (n - 1)


def build_watchlist(
    client,
    extra_symbols: set[str],
    p: PullbackParams,
    today: date,
    request_interval: float = 0.1,
) -> dict[str, WatchItem]:
    pool: set[str] = set(extra_symbols)
    for duration in ("1d", "1w", "1mo"):
        try:
            for r in client.get_rankings("TOP_GAINERS", duration):
                pool.add(r["symbol"])
        except Exception as exc:
            log.warning("상승률 랭킹(%s) 조회 실패: %s", duration, exc)
    if not pool:
        return {}

    names: dict[str, str] = {}
    symbols = sorted(pool)
    for info in client.get_stocks(symbols):
        kr = info.get("koreanMarketDetail") or {}
        if info.get("status") != "ACTIVE" or info.get("securityType") != "STOCK":
            continue
        if not info.get("isCommonShare", True) or info.get("market") not in (None, "KOSPI", "KOSDAQ"):
            continue
        if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        names[info["symbol"]] = info.get("name", info["symbol"])

    watch: dict[str, WatchItem] = {}
    for symbol, name in names.items():
        try:
            bars = parse_candles(client.get_candles(symbol, "1d", 60))
        except Exception as exc:
            log.warning("%s 캔들 조회 실패: %s", symbol, exc)
            continue
        item, reason = analyze(symbol, name, bars, today, p)
        if item:
            watch[symbol] = item
        else:
            log.debug("  제외 %s %s: %s", symbol, name, reason)
        time.sleep(request_interval)
    log.info(
        "눌림목 감시 목록 %d 종목 (후보 %d): %s",
        len(watch), len(pool),
        ", ".join(f"{w.name}(+{w.surge_pct:.0%} {w.surge_date:%m/%d})" for w in watch.values()) or "없음",
    )
    return watch
