"""장 운영일 판단: 매수일(화요일), 청산일(금요일 또는 휴장 전날)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

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

    @property
    def is_liquidation_day(self) -> bool:
        """다음 날이 휴장이면 청산일. 금요일(→주말)과 공휴일 전날을 모두 포괄한다."""
        return self.is_open and self.next_business_day != self.today + timedelta(days=1)

    @property
    def is_buy_day(self) -> bool:
        """이번 주 매수 실행일인지.

        - 기본: 화요일.
        - 화요일이 휴장이거나 연휴 전날(청산일)이면, 같은 주의 다음 거래일 중
          청산일이 아닌 첫 날(수/목)에 매수한다.
        - 청산일에는 매수하지 않는다 (당일 매수 후 곧바로 전량 매도하게 되므로).
        """
        if not self.is_open or self.is_liquidation_day:
            return False
        weekday = self.today.weekday()  # 월=0, 화=1
        if weekday < 1:
            return False
        if weekday == 1:
            return True
        tuesday = self.today - timedelta(days=weekday - 1)
        prev = self.previous_business_day
        # 직전 거래일이 이번 주 화요일 이후이고, 그 날의 다음 거래일이 바로 오늘이라면
        # 직전 거래일은 청산일이 아니었으므로(= 정상 매수 가능일) 이미 매수 기회가 있었다.
        prev_was_buyable = prev >= tuesday and self.today == prev + timedelta(days=1)
        return not prev_was_buyable


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
