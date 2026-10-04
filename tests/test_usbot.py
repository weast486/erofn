import csv
import gzip
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from binancebot.backtest import Params
from usbot.backtest import run
from usbot.data import regular_bars


class DataTest(unittest.TestCase):
    def test_bar_time_is_end_time(self):
        def c(ts):
            return {"timestamp": ts, "openPrice": "1", "highPrice": "2", "lowPrice": "1", "closePrice": "2", "volume": "10"}
        # 2026-09-01 (서머타임) 09:30 ET = 22:30 KST. 토스 시각은 봉이 끝나는 시각
        raw = [c("2026-09-01T22:30:00.000+09:00"),   # 09:29~09:30 장 전 → 제외
               c("2026-09-01T22:31:00.000+09:00"),   # 09:30~09:31 첫 봉
               c("2026-09-02T05:00:00.000+09:00"),   # 15:59~16:00 마지막 봉
               c("2026-09-02T05:01:00.000+09:00")]   # 시간외 → 제외
        bars = regular_bars(raw, date(2026, 9, 1))
        first = int(datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(sorted(bars), [first, first + 389 * 60_000])
        self.assertEqual(bars[first][6], 20.0)  # 거래대금 = 거래량 x 종가


class RunTest(unittest.TestCase):
    def test_end_to_end_long_only_integer_shares(self):
        with tempfile.TemporaryDirectory() as tmp:
            t0 = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)  # 09:30 ET
            prices = [(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100.5, 101.5, 100.4, 101.2),
                      (101.2, 102, 101, 101.8), (101.8, 105.5, 101.5, 105)]
            with gzip.open(Path(tmp) / "AAPL_1m.csv.gz", "wt", newline="") as f:
                w = csv.writer(f)
                w.writerow(["open_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
                for i, (o, h, l, c) in enumerate(prices):
                    w.writerow([int((t0 + timedelta(minutes=i)).timestamp() * 1000), o, h, l, c, 1, 1e6, 1])
            res = run(Path(tmp), Params(orb_minutes=2, direction="long"), capital=1000, position_pct=50,
                      fee=0, slippage=0, verbose=False)
            # 500달러 / 101 = 4주, 101 → 105 익절 = +16달러 = +1.6%
            self.assertEqual(res["trades"], 1)
            self.assertAlmostEqual(res["return_pct"], 1.6)
            self.assertAlmostEqual(res["avg_pct"], (105 / 101 - 1) * 100)


if __name__ == "__main__":
    unittest.main()
