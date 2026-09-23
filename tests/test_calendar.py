import unittest
from datetime import date

from tossbot.market_calendar import TradingDay, trading_day_from_api


def td(today, prev, nxt, is_open=True):
    return TradingDay(date.fromisoformat(today), is_open, date.fromisoformat(prev), date.fromisoformat(nxt))


class TradingDayTest(unittest.TestCase):
    def test_normal_week(self):
        # 2026-09-14(월) ~ 18(금), 휴일 없음
        self.assertTrue(td("2026-09-15", "2026-09-14", "2026-09-16").is_buy_day)  # 화
        self.assertFalse(td("2026-09-15", "2026-09-14", "2026-09-16").is_liquidation_day)
        self.assertFalse(td("2026-09-16", "2026-09-15", "2026-09-17").is_buy_day)  # 수: 이미 화요일 매수
        self.assertFalse(td("2026-09-14", "2026-09-11", "2026-09-15").is_buy_day)  # 월
        fri = td("2026-09-18", "2026-09-17", "2026-09-21")
        self.assertTrue(fri.is_liquidation_day)
        self.assertFalse(fri.is_buy_day)

    def test_pre_holiday_liquidation(self):
        # 목요일이 공휴일 → 수요일이 청산일
        self.assertTrue(td("2026-10-07", "2026-10-06", "2026-10-09").is_liquidation_day)

    def test_tuesday_holiday_buys_wednesday(self):
        # 화요일 휴장 → 수요일 매수
        wed = td("2026-09-23", "2026-09-21", "2026-09-24")
        self.assertTrue(wed.is_buy_day)

    def test_tuesday_is_pre_holiday_buys_after_holiday(self):
        # 화요일이 연휴 전날(수 휴장)이면 화요일은 청산일이라 매수 안 함 → 목요일 매수
        tue = td("2026-09-22", "2026-09-21", "2026-09-24")
        self.assertTrue(tue.is_liquidation_day)
        self.assertFalse(tue.is_buy_day)
        thu = td("2026-09-24", "2026-09-22", "2026-09-25")
        self.assertTrue(thu.is_buy_day)

    def test_closed_day(self):
        self.assertFalse(td("2026-09-19", "2026-09-18", "2026-09-21", is_open=False).is_buy_day)

    def test_parse_api(self):
        result = {
            "today": {
                "date": "2026-09-22",
                "integrated": {
                    "regularMarket": {
                        "startTime": "2026-09-22T09:00:00+09:00",
                        "singlePriceAuctionStartTime": "2026-09-22T15:20:00+09:00",
                        "endTime": "2026-09-22T15:30:00+09:00",
                    }
                },
            },
            "previousBusinessDay": {"date": "2026-09-21"},
            "nextBusinessDay": {"date": "2026-09-23"},
        }
        day = trading_day_from_api(result)
        self.assertTrue(day.is_open)
        self.assertEqual(day.closing_auction_start.hour, 15)
        self.assertEqual(day.closing_auction_start.minute, 20)
        closed = trading_day_from_api({**result, "today": {"date": "2026-09-22", "integrated": None}})
        self.assertFalse(closed.is_open)


if __name__ == "__main__":
    unittest.main()
