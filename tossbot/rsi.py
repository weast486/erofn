"""RSI 평균회귀 종목 선정·매도 판정 (실전 봇과 백테스트 공용 RSI 계산).

매수 조건 (15:10~15:20 현재가를 오늘 종가로 보고 판정)
  1. 코스피 시가총액 상위 100 (tossbot/lists/kospi_top100.txt)
  2. RSI(14) < 30 (과매도)
  3. 가격 <= 종목당 예산(10만원), 하한가 아님, 직전 20일 평균 거래대금 >= 30억원
여러 종목이면 RSI 낮은 순.
매도: RSI(14) >= 50 이면 15:10~15:20 에 시장가 / 손절 -10% 조건주문 / 20거래일째 시장가.

※ 백테스트(2023~2026.9, 100만원·종목당 10만원): 연 +18.2%, +16.5%, +10.0%, +17.1%.
   신고가 전략과 한 계좌(신고가 먼저, 남는 자리 RSI): +29.7%, +21.2%, +43.1%, +30.1%.
   과거 성과는 미래 수익을 보장하지 않는다.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .selector import Bar, parse_candles

log = logging.getLogger(__name__)

DEFAULT_UNIVERSE_FILE = Path(__file__).parent / "lists" / "kospi_top100.txt"
CANDLE_COUNT = 200  # Wilder RSI 는 시작점 영향이 있어 넉넉히 받는다


def rsi_series(closes: list[float], period: int) -> list[float | None]:
    """Wilder RSI. 앞쪽 period 개는 None."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = [max(closes[k] - closes[k - 1], 0.0) for k in range(1, period + 1)]
    losses = [max(closes[k - 1] - closes[k], 0.0) for k in range(1, period + 1)]
    ag, al = sum(gains) / period, sum(losses) / period
    for k in range(period, len(closes)):
        if k > period:
            ch = closes[k] - closes[k - 1]
            ag = (ag * (period - 1) + max(ch, 0.0)) / period
            al = (al * (period - 1) + max(-ch, 0.0)) / period
        out[k] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


@dataclass
class RsiParams:
    price_cap: float = 100_000
    period: int = 14
    buy_below: float = 30.0
    sell_above: float = 50.0
    min_avg_trading_amount: float = 3_000_000_000
    limit_down_ratio: float = 0.705  # 전일 종가 대비 이 비율 이하면 하한가로 보고 제외


@dataclass
class RsiCandidate:
    symbol: str
    name: str
    price: float
    rsi: float


def load_universe(path: str | Path = DEFAULT_UNIVERSE_FILE) -> dict[str, str]:
    """'종목코드 종목명' 한 줄씩. # 은 주석."""
    out: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        code, _, name = line.partition(" ")
        out[code] = name.strip() or code
    return out


def today_rsi(bars: list[Bar], today: date, price: float, period: int) -> float | None:
    """완성된 일봉(오늘 제외) 종가 + 오늘 현재가로 계산한 오늘 RSI."""
    closes = [b.close for b in bars if b.day < today] + [price]
    return rsi_series(closes, period)[-1]


def evaluate(symbol: str, name: str, bars: list[Bar], today: date, price: float, p: RsiParams
             ) -> tuple[RsiCandidate | None, str]:
    done = [b for b in bars if b.day < today]
    if len(done) < max(p.period * 3, 21):
        return None, "데이터 부족"
    if price > p.price_cap:
        return None, f"1주 가격 {p.price_cap:,.0f}원 초과"
    if price <= done[-1].close * p.limit_down_ratio:
        return None, "하한가"
    if sum(b.close * b.volume for b in done[-20:]) / 20 < p.min_avg_trading_amount:
        return None, "20일 평균 거래대금 부족"
    rsi = today_rsi(done, today, price, p.period)
    if rsi is None or rsi >= p.buy_below:
        return None, f"RSI {rsi:.1f}" if rsi is not None else "RSI 계산 불가"
    return RsiCandidate(symbol, name, price, rsi), ""


def select_rsi(client, universe: dict[str, str], exclude: set[str], top_n: int, p: RsiParams, today: date,
               request_interval: float = 0.1) -> list[RsiCandidate]:
    """대상 종목 중 RSI 과매도 후보를 RSI 낮은 순으로 반환."""
    if top_n <= 0:
        return []
    symbols = sorted(set(universe) - exclude)
    if not symbols:
        return []
    active: dict[str, str] = {}
    for info in client.get_stocks(symbols):
        kr = info.get("koreanMarketDetail") or {}
        if info.get("status") != "ACTIVE" or kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        active[info["symbol"]] = info.get("name") or universe.get(info["symbol"], info["symbol"])
    prices = {r["symbol"]: float(r["lastPrice"]) for r in client.get_prices(sorted(active))}
    cands: list[RsiCandidate] = []
    for symbol, name in active.items():
        price = prices.get(symbol)
        if not price or price > p.price_cap:
            continue  # 비싼 종목은 캔들 조회 생략
        try:
            bars = parse_candles(client.get_candles(symbol, "1d", CANDLE_COUNT))
        except Exception as exc:
            log.warning("%s 캔들 조회 실패: %s", symbol, exc)
            continue
        cand, reason = evaluate(symbol, name, bars, today, price, p)
        if cand:
            cands.append(cand)
        else:
            log.debug("  제외 %s %s: %s", symbol, name, reason)
        time.sleep(request_interval)
    cands.sort(key=lambda c: c.rsi)
    log.info("RSI 과매도 후보 %d종목: %s", len(cands),
             ", ".join(f"{c.name}(RSI {c.rsi:.1f})" for c in cands) or "없음")
    return cands[:top_n]
