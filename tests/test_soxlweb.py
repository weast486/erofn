import json
import unittest

from usbot import soxlweb as w

CSV = '''Direxion Daily Semiconductor Bull 3X ETF
SOXL
Shares Outstanding:1


"TradeDate","AccountTicker","StockTicker","SecurityDescription","Shares","Price","MarketValue","Cusip","HoldingsPercent"
"11/24/2026 12:00:00 AM","SOXL","AVGO","BROADCOM INC","1","1","1","x","5.0"
"11/24/2026 12:00:00 AM","SOXL","","CASH","1","1","1","x","40.0"
"11/24/2026 12:00:00 AM","SOXL","AMD","ADVANCED MICRO DEVICES","1","1","1","x","6.0"
'''


def chart(closes, start=100):
    return json.dumps({"chart": {"result": [{"timestamp": [start + i for i in range(len(closes))],
                                             "indicators": {"quote": [{"close": closes}]}}]}}).encode()


class SoxlWebTest(unittest.TestCase):
    def test_holdings_sorted_without_cash(self):
        date, rows = w.parse_holdings(CSV)
        self.assertEqual(date, "11/24/2026")
        self.assertEqual([r["symbol"] for r in rows], ["AMD", "AVGO"])

    def test_build_percent_and_basket(self):
        def fetch(url):
            if "direxion" in url:
                return CSV.encode()
            if "/SOXL?" in url:
                return chart([10, 13])
            if "/AMD?" in url:
                return chart([100, 110])
            return chart([50, None, 45])  # AVGO, 빈 값 포함·봉 하나 더

        r = w.build(w.Data(fetch), {"range": "5d", "n": "2"})
        self.assertAlmostEqual(r["stats"]["SOXL"]["chg"], 30.0)
        self.assertEqual(len(r["times"]), 3)
        self.assertAlmostEqual(r["lines"]["AMD"][-1], 10.0)   # 마지막 칸은 직전 값 유지
        self.assertAlmostEqual(r["lines"]["AVGO"][-1], -10.0)
        basket = r["lines"]["2종목 가중평균"][-1]
        self.assertAlmostEqual(basket, (10 * 6 - 10 * 5) / 11)

    def test_fallback_when_direxion_fails(self):
        def fetch(url):
            if "direxion" in url:
                raise OSError("막힘")
            return chart([1, 2])
        h = w.Data(fetch).holdings()
        self.assertIn("기본 목록", h["source"])
        self.assertEqual(h["rows"][0]["symbol"], "AMD")


if __name__ == "__main__":
    unittest.main()
