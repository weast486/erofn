"""매주 화요일 '이번 주 상승 가능성이 높아 보이는' 종목 선정.

단기 추세추종(모멘텀) 규칙 기반 스코어링:

필터 (모두 통과해야 후보)
  1. 정배열: 종가 > 20일선 > 60일선
  2. RSI(14) 50~75: 상승 추세이되 과열 구간은 제외
  3. 최근 5거래일 수익률 <= MAX_5D_RETURN_PCT: 이미 급등한 종목 추격 매수 방지
  4. 20일 평균 거래대금 >= MIN_AVG_TRADED_VALUE: 유동성(체결 슬리피지) 확보
  5. 종가 <= 종목당 예산: 최소 1주는 살 수 있어야 함

점수 (후보 간 백분위 순위의 가중합)
  - 20일 수익률 (중기 모멘텀)             40%
  - 거래량 증가율 (5일 평균 / 20일 평균)   30%
  - 20일 고가 근접도 (종가 / 20일 최고가)  30%

※ 어떤 규칙도 수익을 보장하지 않는다. DRY_RUN 으로 충분히 검증한 뒤 사용할 것.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from .config import KST

log = logging.getLogger(__name__)

WEIGHTS = {"ret20": 0.4, "vol_ratio": 0.3, "high_prox": 0.3}


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


def _rsi(closes: list[float], n: int = 14) -> float:
    gains, losses = [], []
    for prev, cur in zip(closes[-n - 1 : -1], closes[-n:]):
        diff = cur - prev
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain, avg_loss = sum(gains) / n, sum(losses) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_metrics(bars: list[Bar]) -> dict[str, float] | None:
    if len(bars) < 61:
        return None
    closes = [b.close for b in bars]
    volumes = [b.volume for b in bars]
    close = closes[-1]
    return {
        "close": close,
        "ma20": _sma(closes, 20),
        "ma60": _sma(closes, 60),
        "rsi14": _rsi(closes, 14),
        "ret5": close / closes[-6] - 1,
        "ret20": close / closes[-21] - 1,
        "vol_ratio": _sma(volumes, 5) / max(_sma(volumes, 20), 1.0),
        "high_prox": close / max(b.high for b in bars[-20:]),
        "avg_traded_value": sum(b.close * b.volume for b in bars[-20:]) / 20,
    }


def passes_filters(
    m: dict[str, float], slot_budget: float, min_avg_traded_value: float, max_5d_return_pct: float
) -> str | None:
    """통과하면 None, 탈락하면 사유 문자열."""
    if not (m["close"] > m["ma20"] > m["ma60"]):
        return "정배열 아님"
    if not (50 <= m["rsi14"] <= 75):
        return f"RSI {m['rsi14']:.0f}"
    if m["ret5"] * 100 > max_5d_return_pct:
        return f"5일 급등 {m['ret5']:.1%}"
    if m["avg_traded_value"] < min_avg_traded_value:
        return "거래대금 부족"
    if m["close"] > slot_budget:
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


def load_universe(path: str) -> list[str]:
    symbols = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            code = line.split("#", 1)[0].strip()
            if code:
                symbols.append(code)
    return list(dict.fromkeys(symbols))


def select_stocks(
    client,
    universe: list[str],
    top_n: int,
    slot_budget: float,
    min_avg_traded_value: float,
    max_5d_return_pct: float,
    today: date,
    request_interval: float = 0.1,
) -> list[Candidate]:
    # 1) 종목 기본정보로 거래 불가 종목 제외
    tradable: dict[str, str] = {}
    for info in client.get_stocks(universe):
        kr = info.get("koreanMarketDetail") or {}
        if info.get("status") != "ACTIVE":
            continue
        if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        if info.get("securityType") != "STOCK" or not info.get("isCommonShare", True):
            continue
        tradable[info["symbol"]] = info.get("name", info["symbol"])
    log.info("유니버스 %d 종목 중 거래 가능 보통주 %d 종목", len(universe), len(tradable))

    # 2) 일봉 기반 지표 계산 & 필터
    cands: list[Candidate] = []
    for symbol, name in tradable.items():
        try:
            bars = parse_candles(client.get_candles(symbol, "1d", 120))
        except Exception as exc:  # 한 종목 실패로 전체 선정이 멈추지 않도록
            log.warning("%s 캔들 조회 실패: %s", symbol, exc)
            continue
        # 장중 실행 시 오늘의 미완성 봉은 제외 (전일 종가 기준으로 판단)
        bars = [b for b in bars if b.day < today]
        m = compute_metrics(bars)
        if m is None:
            continue
        reason = passes_filters(m, slot_budget, min_avg_traded_value, max_5d_return_pct)
        if reason:
            log.debug("%s %s 탈락: %s", symbol, name, reason)
        else:
            cands.append(Candidate(symbol, name, m["close"], m))
        time.sleep(request_interval)

    ranked = score_candidates(cands)
    log.info("필터 통과 %d 종목, 상위 %d 종목 선정", len(ranked), min(top_n, len(ranked)))
    return ranked[:top_n]
