"""신고가 돌파 종목 선정 (실전 봇과 백테스트 공용 판정).

매수 조건 (모두 만족, 판정은 일봉 종가 / 실전은 15:10~15:20 현재가)
  1. 20일 신고가: 오늘 가격 > 직전 20거래일 종가 최고값
  2. 한 달 내 첫 신고가: 직전 20거래일 동안 1번 조건을 만족한 날이 없음
  3. 당일 거래대금 >= 200억원
  4. 직전 20일 평균 거래대금 >= 30억원
  5. 가격 <= 종목당 예산(10만원), 상한가(전일 대비 +29.5% 이상) 아님
  6. 코스피·코스닥 보통주, 거래정지·정리매매 아님
여러 종목이면 당일 거래대금이 큰 순서로 빈 자리만큼.

※ 백테스트(2024~2026.9): 손절 -4.7% / 익절 +20% 에서 연 -0.4%, +54.5%, +7.5%.
   과거 성과는 미래 수익을 보장하지 않는다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date

from .selector import Bar, parse_candles

log = logging.getLogger(__name__)


@dataclass
class BreakoutParams:
    slot_budget: float = 100_000
    entry_days: int = 20  # N일 신고가
    first_in_days: int = 20  # 직전 N거래일 안에 신고가가 없던 첫 신고가만
    min_day_amount: float = 20_000_000_000  # 당일 거래대금 하한
    min_avg_trading_amount: float = 3_000_000_000  # 직전 20일 평균 거래대금 하한
    limit_up_ratio: float = 1.295  # 전일 종가 대비 이 비율 이상이면 상한가로 보고 제외
    max_price: float = 0  # 1주 가격 상한 (0 이면 종목당 예산)
    min_price: float = 10_000  # 1주 가격 하한
    skip_touched_limit_up: bool = True  # 장중 상한가를 찍고 내려온 종목 제외

    @property
    def price_cap(self) -> float:
        return min(self.slot_budget, self.max_price) if self.max_price > 0 else self.slot_budget


@dataclass
class BreakoutCandidate:
    symbol: str
    name: str
    price: float
    day_amount: float  # 당일 거래대금 (우선순위)
    change: float  # 전일 대비 상승률
    prev_high: float  # 직전 20일 최고 종가


def new_high_flags(closes: list[float], n: int) -> list[bool]:
    """각 날의 종가가 직전 n거래일 종가 최고값보다 높은지 (슬라이딩 최대값, O(len))."""
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


def evaluate(
    symbol: str, name: str, bars: list[Bar], today: date, price: float, today_volume: float, p: BreakoutParams,
    today_high: float | None = None,
) -> tuple[BreakoutCandidate | None, str]:
    """완성된 일봉(오늘 제외) + 오늘 현재가·거래량·고가로 매수 조건 판정. (후보, 탈락 사유)."""
    done = [b for b in bars if b.day < today]
    if len(done) < p.entry_days + p.first_in_days:
        return None, "데이터 부족"
    closes = [b.close for b in done]
    prev_high = max(closes[-p.entry_days:])
    if price <= prev_high:
        return None, f"{p.entry_days}일 신고가 아님"
    if any(new_high_flags(closes, p.entry_days)[-p.first_in_days:]):
        return None, f"최근 {p.first_in_days}일 안에 이미 신고가"
    if price > p.price_cap:
        return None, f"1주 가격 {p.price_cap:,.0f}원 초과"
    if price < p.min_price:
        return None, f"1주 가격 {p.min_price:,.0f}원 미만"
    if price >= closes[-1] * p.limit_up_ratio:
        return None, "상한가 (체결 불가)"
    if p.skip_touched_limit_up and today_high and today_high >= closes[-1] * p.limit_up_ratio:
        return None, "장중 상한가를 찍고 내려옴"
    day_amount = price * today_volume
    if day_amount < p.min_day_amount:
        return None, f"당일 거래대금 {day_amount / 1e8:,.0f}억"
    avg_amount = sum(b.close * b.volume for b in done[-20:]) / 20
    if avg_amount < p.min_avg_trading_amount:
        return None, "20일 평균 거래대금 부족"
    return BreakoutCandidate(symbol, name, price, day_amount, price / closes[-1] - 1, prev_high), ""


def select_breakouts(
    client, exclude: set[str], top_n: int, p: BreakoutParams, today: date, request_interval: float = 0.1
) -> list[BreakoutCandidate]:
    """시장 거래대금 상위 + 당일 상승률 상위 종목에서 신고가 돌파 후보를 골라 거래대금 순으로 반환."""
    if top_n <= 0:
        return []
    pool: set[str] = set()
    for ranking_type, duration in (("MARKET_TRADING_AMOUNT", "realtime"), ("TOP_GAINERS", "1d")):
        try:
            for r in client.get_rankings(ranking_type, duration):
                price = float((r.get("price") or {}).get("lastPrice") or 0)
                if p.min_price <= price <= p.price_cap and 0 < price and float(r.get("tradingAmount") or 0) >= p.min_day_amount:
                    pool.add(r["symbol"])
        except Exception as exc:
            log.warning("랭킹(%s/%s) 조회 실패: %s", ranking_type, duration, exc)
    pool -= exclude
    if not pool:
        return []

    names: dict[str, str] = {}
    for info in client.get_stocks(sorted(pool)):
        kr = info.get("koreanMarketDetail") or {}
        if info.get("status") != "ACTIVE" or info.get("securityType") != "STOCK":
            continue
        if not info.get("isCommonShare", True) or info.get("market") not in (None, "KOSPI", "KOSDAQ"):
            continue
        if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        names[info["symbol"]] = info.get("name", info["symbol"])

    cands: list[BreakoutCandidate] = []
    for symbol, name in names.items():
        try:
            bars = parse_candles(client.get_candles(symbol, "1d", 60))
        except Exception as exc:
            log.warning("%s 캔들 조회 실패: %s", symbol, exc)
            continue
        today_bar = next((b for b in bars if b.day == today), None)
        if today_bar is None:
            continue
        cand, reason = evaluate(symbol, name, bars, today, today_bar.close, today_bar.volume, p, today_bar.high)
        if cand:
            cands.append(cand)
        else:
            log.debug("  제외 %s %s: %s", symbol, name, reason)
        time.sleep(request_interval)
    cands.sort(key=lambda c: c.day_amount, reverse=True)
    log.info(
        "신고가 돌파 후보 %d종목 (검사 %d): %s", len(cands), len(names),
        ", ".join(f"{c.name}({c.change:+.1%}, {c.day_amount / 1e8:,.0f}억)" for c in cands) or "없음",
    )
    return cands[:top_n]
