import csv
import gzip
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from binancebot.backtest import ET, Bar, Params, orb_trade, run
from binancebot.data import keep_bar, tradfi_symbols
from binancebot.sizing import SizeRule, position_size


class SizingTest(unittest.TestCase):
    def test_risk_based_qty(self):
        rule = SizeRule(risk_pct=2, max_leverage=5, fee_pct=0, slippage_pct=0)
        # 1000달러의 2% = 20달러, 1개당 손절 폭 2달러 → 10개
        self.assertAlmostEqual(position_size(1000, 100, 98, rule), 10)

    def test_costs_reduce_qty(self):
        rule = SizeRule(2, 5, 0.05, 0.05)
        q = position_size(1000, 100, 98, rule)
        self.assertAlmostEqual(q, 20 / (2 + 100 * 0.0015))

    def test_leverage_cap(self):
        rule = SizeRule(2, 5, 0, 0)
        # 손절 폭 0.1 → 위험 기준 200개(2만 달러) 이지만 5배 = 5000달러 → 50개
        self.assertAlmostEqual(position_size(1000, 100, 99.9, rule), 50)
        # 이미 4000달러 들고 있으면 1000달러어치만
        self.assertAlmostEqual(position_size(1000, 100, 99.9, rule, open_notional=4000), 10)
        self.assertEqual(position_size(1000, 100, 99.9, rule, open_notional=5000), 0)

    def test_step_and_min_notional(self):
        rule = SizeRule(2, 5, 0, 0)
        self.assertAlmostEqual(position_size(1000, 100, 97, rule, step=1), 6)
        self.assertEqual(position_size(10, 100, 97, rule, step=0.001, min_notional=10), 0)


def bars_from(prices, d=date(2026, 9, 1)):
    t0 = datetime.combine(d, time(9, 30), ET)
    return [Bar(t0 + timedelta(minutes=i), o, h, l, c, 1e6) for i, (o, h, l, c) in enumerate(prices)]


class OrbTest(unittest.TestCase):
    def setUp(self):
        self.p = Params(orb_minutes=2, target_r=2, rule=SizeRule(2, 5, 0, 0))

    def test_long_target(self):
        bars = bars_from([(100, 101, 99, 100), (100, 101, 99.5, 100.5),  # 범위 99~101
                          (100.5, 101.5, 100.4, 101.2),                    # 101 돌파 매수
                          (101.2, 102, 101, 101.8), (101.8, 105.5, 101.5, 105)])  # 101+2x2=105 익절
        t = orb_trade("X", date(2026, 9, 1), bars, self.p, 0)
        self.assertEqual((t.side, t.entry, t.stop, t.reason, t.exit), (1, 101, 99, "target", 105))

    def test_short_stop(self):
        bars = bars_from([(100, 101, 99, 100), (100, 101, 99.5, 100.5),
                          (99.5, 99.8, 98.5, 98.8),  # 99 깨짐 → 숏
                          (98.8, 101.5, 98.7, 101.2)])  # 101 손절
        t = orb_trade("X", date(2026, 9, 1), bars, self.p, 0)
        self.assertEqual((t.side, t.entry, t.reason, t.exit), (-1, 99, "stop", 101))

    def test_both_sides_same_bar_skipped(self):
        bars = bars_from([(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100, 102, 98, 100)])
        self.assertIsNone(orb_trade("X", date(2026, 9, 1), bars, self.p, 0))

    def test_same_bar_stop_and_target_is_stop(self):
        bars = bars_from([(100, 101, 99, 100), (100, 101, 99.5, 100.5),
                          (100.5, 101.5, 100.4, 101.2), (101, 106, 98, 100)])
        self.assertEqual(orb_trade("X", date(2026, 9, 1), bars, self.p, 0).reason, "stop")


class DataTest(unittest.TestCase):
    def test_keep_bar_weekday_window(self):
        ms = lambda *a: int(datetime(*a, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertTrue(keep_bar(ms(2026, 9, 1, 13, 30)))   # 화 13:30 UTC
        self.assertFalse(keep_bar(ms(2026, 9, 1, 23, 0)))
        self.assertFalse(keep_bar(ms(2026, 9, 5, 14, 0)))   # 토요일

    def test_tradfi_symbols(self):
        info = {"symbols": [
            {"symbol": "BTCUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL", "underlyingType": "COIN"},
            {"symbol": "TSLAUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "TRADIFI_PERPETUAL", "underlyingType": "COIN"},
            {"symbol": "NVDAUSDT", "quoteAsset": "USDT", "status": "TRADING", "contractType": "PERPETUAL", "underlyingType": "EQUITY"},
        ]}
        self.assertEqual([s["symbol"] for s in tradfi_symbols(info)], ["TSLAUSDT", "NVDAUSDT"])


class RunTest(unittest.TestCase):
    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TSLAUSDT_1m.csv.gz"
            t0 = datetime(2026, 9, 1, 13, 30, tzinfo=timezone.utc)  # 09:30 ET (서머타임)
            prices = [(100, 101, 99, 100), (100, 101, 99.5, 100.5), (100.5, 101.5, 100.4, 101.2),
                      (101.2, 102, 101, 101.8), (101.8, 105.5, 101.5, 105)]
            with gzip.open(path, "wt", newline="") as f:
                w = csv.writer(f)
                w.writerow(["open_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
                for i, (o, h, l, c) in enumerate(prices):
                    w.writerow([int((t0 + timedelta(minutes=i)).timestamp() * 1000), o, h, l, c, 1, 1e6, 1])
            res = run(Path(tmp), Params(orb_minutes=2, rule=SizeRule(2, 5, 0, 0)), verbose=False)
            # 위험 20달러 → 10개, 101→105 = +40달러 = +4%
            self.assertEqual(res["trades"], 1)
            self.assertAlmostEqual(res["return_pct"], 4.0)


if __name__ == "__main__":
    unittest.main()
