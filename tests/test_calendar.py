import unittest

from tossbot.market_calendar import trading_day_from_api


class TradingDayTest(unittest.TestCase):
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
        self.assertEqual((day.closing_auction_start.hour, day.closing_auction_start.minute), (15, 20))
        closed = trading_day_from_api({**result, "today": {"date": "2026-09-22", "integrated": None}})
        self.assertFalse(closed.is_open)


if __name__ == "__main__":
    unittest.main()
