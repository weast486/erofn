"""종가배팅 종목 선정: 장 마감 직전(15:10) '익일 크게 오를 것으로 예상되는' 종목.

후보군: 시장 전체 실시간 거래대금 상위 100 (`/api/v1/rankings`, 투자유의 종목 제외)

필터 (모두 통과해야 후보)
  1. 당일 등락률 MIN_CHANGE_PCT ~ MAX_CHANGE_PCT (기본 +2% ~ +20%, 상한가 근처 제외)
  2. 당일 거래대금 >= MIN_TRADING_AMOUNT (기본 100억)
  3. 1주 가격 <= 종목당 예산
  4. 거래 가능 보통주 (상장·거래정지·정리매매·우선주·ETF 제외)
  5. 양봉 (현재가 > 시가)
  6. 고가 근처 마감: 현재가 / 당일 고가 >= MIN_CLOSE_TO_HIGH (기본 0.97, 윗꼬리 짧음)
  7. 거래량 급증: 당일 거래량 / 20일 평균 거래량 >= MIN_VOLUME_RATIO (기본 2배)
  8. 추세: 현재가 > 5일선, 현재가 > 20일선

점수 (후보 간 백분위 순위의 가중합)
  - 당일 거래대금              30%
  - 거래량 급증 배수           25%
  - 고가 근처 마감 정도        25%
  - 20일 고가 돌파 정도        20%

통과 종목이 없으면 0종목 (그날은 매수하지 않음).
※ 어떤 규칙도 수익을 보장하지 않는다. DRY_RUN 으로 충분히 검증한 뒤 사용할 것.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import KST

log = logging.getLogger(__name__)

WEIGHTS = {"trading_amount": 0.30, "vol_ratio": 0.25, "close_to_high": 0.25, "breakout": 0.20}


@dataclass
class Bar:
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Candidate:
    symbol: str
    name: str
    close: float
    metrics: dict[str, float] = field(default_factory=dict)
    score: float = 0.0


@dataclass
class SelectionParams:
    slot_budget: float = 100_000
    min_change_pct: float = 2.0
    max_change_pct: float = 20.0
    min_trading_amount: float = 10_000_000_000
    min_volume_ratio: float = 2.0
    min_close_to_high: float = 0.97


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


def compute_metrics(bars: list[Bar], today: date) -> dict[str, float] | None:
    """당일(장중 미완성) 봉을 마지막으로 포함한 일봉으로 지표 계산."""
    if len(bars) < 21 or bars[-1].day != today:
        return None
    cur, prev = bars[-1], bars[:-1]
    closes = [b.close for b in bars]
    avg_vol20 = _sma([b.volume for b in prev], 20)
    return {
        "close": cur.close,
        "open": cur.open,
        "change": cur.close / prev[-1].close - 1,
        "close_to_high": cur.close / cur.high if cur.high else 0.0,
        "vol_ratio": cur.volume / max(avg_vol20, 1.0),
        "breakout": cur.close / max(b.high for b in prev[-20:]),
        "ma5": _sma(closes, 5),
        "ma20": _sma(closes, 20),
    }


def passes_filters(m: dict[str, float], p: SelectionParams) -> str | None:
    """통과하면 None, 탈락하면 사유 문자열."""
    if m["close"] <= m["open"]:
        return "음봉"
    if m["close_to_high"] < p.min_close_to_high:
        return f"윗꼬리 (고가 대비 {m['close_to_high']:.1%})"
    if m["vol_ratio"] < p.min_volume_ratio:
        return f"거래량 {m['vol_ratio']:.1f}배"
    if not (m["close"] > m["ma5"] and m["close"] > m["ma20"]):
        return "5일선/20일선 아래"
    if m["close"] > p.slot_budget:
        return "1주 가격이 종목당 예산 초과"
    return None


def _percentile_ranks(values: list[float]) -> list[float]:
    if len(values) == 1:
        return [1.0]
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    for rank, i in enumerate(order):
        ranks[i] = rank / (len(values) - 1)
    return ranks


def score_candidates(cands: list[Candidate]) -> list[Candidate]:
    if not cands:
        return []
    for key, weight in WEIGHTS.items():
        for cand, r in zip(cands, _percentile_ranks([c.metrics[key] for c in cands])):
            cand.score += weight * r
    return sorted(cands, key=lambda c: c.score, reverse=True)


def _ranking_pool(client, p: SelectionParams) -> dict[str, dict]:
    """거래대금 상위 랭킹에서 등락률·거래대금·가격으로 1차 필터."""
    rankings = client.get_rankings("MARKET_TRADING_AMOUNT", "realtime")
    if not rankings:
        rankings = client.get_rankings("MARKET_TRADING_AMOUNT", "1d")
    pool = {}
    for r in rankings:
        price = r.get("price") or {}
        rate = price.get("changeRate")
        if rate is None:
            continue
        rate, last, amount = float(rate), float(price["lastPrice"]), float(r["tradingAmount"])
        if not (p.min_change_pct / 100 <= rate <= p.max_change_pct / 100):
            continue
        if amount < p.min_trading_amount or last > p.slot_budget:
            continue
        pool[r["symbol"]] = {"change": rate, "trading_amount": amount, "last": last}
    return pool


def select_stocks(
    client,
    exclude: set[str],
    top_n: int,
    params: SelectionParams,
    today: date,
    request_interval: float = 0.1,
) -> list[Candidate]:
    if top_n <= 0:
        return []
    pool = {s: v for s, v in _ranking_pool(client, params).items() if s not in exclude}
    log.info("거래대금 상위 중 등락률·거래대금 조건 통과 %d 종목", len(pool))
    if not pool:
        return []

    names: dict[str, str] = {}
    for info in client.get_stocks(list(pool)):
        kr = info.get("koreanMarketDetail") or {}
        if info.get("status") != "ACTIVE" or info.get("securityType") != "STOCK":
            continue
        if not info.get("isCommonShare", True):
            continue
        if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        names[info["symbol"]] = info.get("name", info["symbol"])

    cands: list[Candidate] = []
    for symbol, name in names.items():
        try:
            bars = parse_candles(client.get_candles(symbol, "1d", 60))
        except Exception as exc:  # 한 종목 실패로 전체 선정이 멈추지 않도록
            log.warning("%s 캔들 조회 실패: %s", symbol, exc)
            continue
        m = compute_metrics(bars, today)
        if m is None:
            continue
        m["trading_amount"] = pool[symbol]["trading_amount"]
        reason = passes_filters(m, params)
        if reason:
            log.info("  탈락 %s %s: %s", symbol, name, reason)
        else:
            cands.append(Candidate(symbol, name, m["close"], m))
        time.sleep(request_interval)

    ranked = score_candidates(cands)
    log.info("최종 후보 %d 종목, %d 종목 선정", len(ranked), min(top_n, len(ranked)))
    return ranked[:top_n]
