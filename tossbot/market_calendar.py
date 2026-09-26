"""장 운영 정보 (`GET /api/v1/market-calendar/KR`)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import KST


@dataclass
class TradingDay:
    today: date
    is_open: bool
    previous_business_day: date
    next_business_day: date
    market_open: datetime | None = None  # 정규장 시작
    closing_auction_start: datetime | None = None  # 종가 단일가 시작 (KRX 15:20)
    market_close: datetime | None = None  # 정규장 종료


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(KST)


def trading_day_from_api(result: dict) -> TradingDay:
    """GET /api/v1/market-calendar/KR 응답(result) → TradingDay."""
    today = result["today"]
    integrated = today.get("integrated") or {}
    regular = integrated.get("regularMarket") or {}
    return TradingDay(
        today=date.fromisoformat(today["date"]),
        is_open=bool(regular),
        previous_business_day=date.fromisoformat(result["previousBusinessDay"]["date"]),
        next_business_day=date.fromisoformat(result["nextBusinessDay"]["date"]),
        market_open=_parse_dt(regular.get("startTime")),
        closing_auction_start=_parse_dt(regular.get("singlePriceAuctionStartTime")),
        market_close=_parse_dt(regular.get("endTime")),
    )
