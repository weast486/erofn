import unittest

from binancebot.maweb import atr_pct, backtest, mtf_line, moving_average, near_line, parse_ma, pullback, run, scan


def bars_from(closes, vols=None):
    vols = vols or [1.0] * len(closes)
    return [{"t": i * 60, "o": c, "h": c, "l": c, "c": c, "v": v} for i, (c, v) in enumerate(zip(closes, vols))]


class MovingAverageTest(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(moving_average("sma", 3, [1, 2, 3, 4, 5]), [None, None, 2.0, 3.0, 4.0])

    def test_ema(self):
        v = moving_average("ema", 3, [1, 2, 3, 4])
        self.assertEqual(v[:3], [None, None, 2.0])
        self.assertAlmostEqual(v[3], 4 * 0.5 + 2.0 * 0.5)

    def test_vwma(self):
        v = moving_average("vwma", 2, [10, 20, 30], [1, 3, 1])
        self.assertAlmostEqual(v[1], (10 + 60) / 4)
        self.assertAlmostEqual(v[2], (60 + 30) / 4)

    def test_parse(self):
        self.assertEqual(parse_ma("MA:20"), ("sma", 20))
        self.assertIsNone(parse_ma("off"))
        with self.assertRaises(ValueError):
            parse_ma("wma:5")


class BacktestTest(unittest.TestCase):
    def setUp(self):
        # 내려가다 오르고 다시 내림
        self.closes = [10, 9, 8, 7, 8, 9, 10, 11, 12, 11, 10, 9, 8, 7, 7]
        self.b = bars_from(self.closes)
        self.f = moving_average("sma", 2, self.closes)
        self.s = moving_average("sma", 4, self.closes)

    def test_long_cross_next_open(self):
        r = backtest(self.b, self.f, self.s, side="long", fee_pct=0)
        self.assertEqual(len(r["trades"]), 1)
        t = r["trades"][0]
        # 골든크로스가 확인된 봉 다음 봉 시가에 진입
        up = next(i for i in range(1, len(self.b)) if self.f[i - 1] is not None and self.s[i - 1] is not None
                  and self.f[i - 1] <= self.s[i - 1] and self.f[i] > self.s[i])
        self.assertEqual(t["entry_time"], self.b[up + 1]["t"])
        self.assertEqual(t["entry"], self.closes[up + 1])
        self.assertIsNone(r["open"])

    def test_both_flips(self):
        r = backtest(self.b, self.f, self.s, side="both", fee_pct=0)
        self.assertEqual([t["dir"] for t in r["trades"]], ["long"])
        self.assertEqual(r["open"]["dir"], "short")

    def test_stop(self):
        b = bars_from(self.closes)
        r0 = backtest(b, self.f, self.s, side="long", fee_pct=0)
        e = r0["trades"][0]
        i = next(k for k, x in enumerate(b) if x["t"] == e["entry_time"])
        b[i + 1]["l"] = e["entry"] * 0.9  # 다음 봉에서 -10% 꼬리
        r = backtest(b, self.f, self.s, side="long", stop_pct=5, fee_pct=0)
        self.assertEqual(r["trades"][0]["reason"], "stop")
        self.assertAlmostEqual(r["trades"][0]["ret_pct"], -5.0)

    def test_last_bar_not_used(self):
        # 마지막(진행 중) 봉에서만 교차 → 신호 없음
        closes = [5, 5, 5, 5, 5, 9]
        f, s = moving_average("sma", 1, closes), moving_average("sma", 3, closes)
        r = backtest(bars_from(closes), f, s, fee_pct=0)
        self.assertEqual(r["trades"], [])
        self.assertIsNone(r["pending"])


class FakeMarket:
    def bars(self, symbol, interval, count):
        return bars_from([10, 9, 8, 7, 8, 9, 10, 11, 12, 11, 10, 9, 8, 7, 7])


class RunTest(unittest.TestCase):
    def test_run(self):
        d = run(FakeMarket(), {"symbol": "btcusdt", "interval": "15m", "ma1": "sma:2", "ma2": "ema:4", "ma3": "off"})
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertIsNone(d["lines"][2])
        self.assertIsNotNone(d["sim"])

    def test_bad_interval(self):
        with self.assertRaises(ValueError):
            run(FakeMarket(), {"interval": "2h"})


def ohlc(rows):
    return [{"t": i * 60, "o": o, "h": h, "l": l, "c": c, "v": 1.0} for i, (o, h, l, c) in enumerate(rows)]


class PullbackTest(unittest.TestCase):
    # 눌림선은 100 고정: 6봉 위에 있다가 7번째 봉 저가가 100 에 닿음
    def rows(self, after):
        up = [(104, 106, 103, 105)] * 6
        return ohlc(up + after)

    def test_touch_limit_fill(self):
        b = self.rows([(103, 104, 99.5, 101), (101, 107, 100.5, 106), (106, 106, 106, 106)])
        line = [100.0] * len(b)
        r = pullback(b, line, min_above=5, stop_pct=2, tp_pct=5, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["dir"], t["entry"], t["reason"]), ("long", 100.0, "take"))
        self.assertAlmostEqual(t["exit"], 105.0)
        self.assertEqual(t["entry_time"], b[6]["t"])

    def test_gap_below_fills_at_open_and_stop_same_bar(self):
        b = self.rows([(99, 99.5, 97, 98), (98, 98, 98, 98)])
        line = [100.0] * len(b)
        r = pullback(b, line, min_above=5, stop_pct=2, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual(t["entry"], 99)
        self.assertEqual(t["reason"], "stop")
        self.assertAlmostEqual(t["exit"], 99 * 0.98)

    def test_not_enough_bars_above(self):
        b = self.rows([(103, 104, 99.5, 101), (101, 101, 101, 101)])
        line = [100.0] * len(b)
        r = pullback(b, line, min_above=10, fee_pct=0)
        self.assertEqual(r["trades"], [])
        self.assertIsNone(r["open"])

    def test_trend_rising_blocks_flat_line(self):
        b = self.rows([(103, 104, 99.5, 101), (101, 101, 101, 101)])
        r = pullback(b, [100.0] * len(b), trend="rising", fee_pct=0)
        self.assertIsNone(r["open"])
        rising = [90 + i for i in range(len(b))]  # 6번째 봉 끝 = 95
        b2 = self.rows([(103, 104, 94, 101), (101, 101, 101, 101)])
        r2 = pullback(b2, rising, trend="rising", fee_pct=0)
        self.assertEqual(r2["open"]["entry"], 95)

    def test_confirm_entry_next_open(self):
        b = self.rows([(103, 104, 99, 99.5), (99.5, 102, 99, 101), (102, 103, 101, 102), (102, 102, 102, 102)])
        line = [100.0] * len(b)
        r = pullback(b, line, entry="confirm", stop_pct=0, fee_pct=0)
        self.assertEqual(r["open"]["entry"], 102)
        self.assertEqual(r["open"]["entry_time"], b[8]["t"])

    def test_line_stop_next_open(self):
        b = self.rows([(103, 104, 99.8, 101), (101, 101, 98, 98.5), (97, 97, 97, 97), (97, 97, 97, 97)])
        line = [100.0] * len(b)
        r = pullback(b, line, stop_mode="line", stop_pct=1, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["reason"], t["exit"], t["exit_time"]), ("line", 97, b[8]["t"]))

    def test_line_touch_stop_and_take(self):
        # 눌림선 100 에서 삼, 손절선 98 (닿으면), 익절선 104 (닿으면)
        b = self.rows([(103, 104, 99.5, 101), (101, 103, 98.5, 102), (102, 104.5, 101, 104), (104, 104, 104, 104)])
        line, low, high = [100.0] * len(b), [98.0] * len(b), [104.0] * len(b)
        r = pullback(b, line, stop_line=low, tp_line=high, stop_mode="touch", stop_pct=0, tp_mode="touch", fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["entry"], t["reason"], t["exit"]), (100.0, "take", 104.0))
        b2 = self.rows([(103, 104, 99.5, 101), (101, 101, 97, 97.5), (97, 97, 97, 97)])
        r2 = pullback(b2, [100.0] * len(b2), stop_line=[98.0] * len(b2), stop_mode="touch", stop_pct=0, fee_pct=0)
        self.assertEqual((r2["trades"][0]["reason"], r2["trades"][0]["exit"]), ("line", 98.0))
        # 손절선이 가격 위에 있으면 바로 손절하지 않음 (위로 올라갔다 내려와 닿아야 함)
        r4 = pullback(b2, [100.0] * len(b2), stop_line=[102.0] * len(b2), stop_mode="touch", stop_pct=0, fee_pct=0)
        self.assertEqual(r4["trades"], [])
        # 익절선이 진입가 아래면 익절로 안 씀
        r3 = pullback(b, line, tp_line=[95.0] * len(b), tp_mode="touch", stop_pct=0, fee_pct=0)
        self.assertEqual(r3["trades"], [])
        self.assertIsNotNone(r3["open"])

    def test_short_mirror_and_pending(self):
        down = [(96, 97, 94, 95)] * 6
        b = ohlc(down + [(96, 96, 96, 96)])  # 진행 중 봉, 아직 100 안 닿음
        r = pullback(b, [100.0] * len(b), side="short", fee_pct=0)
        self.assertIn("숏 지정가 대기", r["pending"])
        b2 = ohlc(down + [(97, 100.2, 96, 99), (99, 99, 94, 94.5), (94, 94, 94, 94)])
        r2 = pullback(b2, [100.0] * len(b2), side="short", tp_pct=5, stop_pct=2, fee_pct=0)
        self.assertEqual(r2["trades"][0]["dir"], "short")
        self.assertEqual(r2["trades"][0]["reason"], "take")

    def test_run_pullback(self):
        d = run(FakeMarket(), {"strat": "pullback", "ma1": "sma:2", "ma2": "sma:4", "trend": "both"})
        self.assertEqual(d["strat"], "pullback")
        self.assertIn("stats", d["sim"])


class MtfTest(unittest.TestCase):
    def test_bigger_tf_steps(self):
        chart = [{"t": i * 300, "c": 1, "v": 1} for i in range(6)]       # 5분봉 6개
        big = [{"t": 0, "c": 10, "v": 1}, {"t": 900, "c": 20, "v": 1}]  # 15분봉 2개
        self.assertEqual(mtf_line(chart, 300, big, 900, "sma", 1), [10, 10, 10, 20, 20, 20])

    def test_smaller_tf_last_inside(self):
        chart = [{"t": 0, "c": 1, "v": 1}, {"t": 300, "c": 1, "v": 1}]
        small = [{"t": i * 60, "c": i, "v": 1} for i in range(10)]     # 1분봉 10개
        self.assertEqual(mtf_line(chart, 300, small, 60, "sma", 1), [4, 9])
        self.assertEqual(mtf_line(chart, 300, small[6:], 60, "sma", 1), [None, 9])


class ScanMarket:
    """BTC 는 선 바로 위, ETH 는 5분봉만 가까움, AAA 는 멀리."""
    def symbols(self):
        return [{"symbol": s, "group": "x", "qv": 5e6} for s in ("BTCUSDT", "ETHUSDT", "AAAUSDT")]

    def bars(self, symbol, interval, count):
        last = {"BTCUSDT": 100.5, "ETHUSDT": 100.5 if interval == "5m" else 103, "AAAUSDT": 110}[symbol]
        return bars_from([100.0] * (count - 1) + [last])


class ScanTest(unittest.TestCase):
    def test_near_line_uses_closed_bars(self):
        r = near_line(bars_from([100.0] * 5 + [101.0]), "vwma", 5)
        self.assertAlmostEqual(r["ma"], 100.0)
        self.assertAlmostEqual(r["dist_pct"], 1.0)
        self.assertIsNone(near_line(bars_from([1, 2]), "sma", 5))

    def test_scan_all_any_side(self):
        q = {"ma": "vwma:100", "tfs": "5m,15m", "pct": "1"}
        self.assertEqual([r["symbol"] for r in scan(ScanMarket(), q)["found"]], ["BTCUSDT"])
        got = scan(ScanMarket(), {**q, "mode": "any"})["found"]
        self.assertEqual(sorted(r["symbol"] for r in got), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(scan(ScanMarket(), {**q, "side": "below"})["found"], [])
        self.assertEqual(scan(ScanMarket(), {**q, "min_qv": "10"})["checked"], 0)

    def test_atr(self):
        b = ohlc([(100, 102, 98, 100)] * 5 + [(100, 100, 100, 100)])  # 끝난 봉마다 변동폭 4
        self.assertAlmostEqual(atr_pct(b, 3), 4.0)
        self.assertIsNone(atr_pct(b, 5))
        q = {"ma": "vwma:100", "tfs": "5m", "pct": "1"}
        found = scan(ScanMarket(), q)["found"]  # ScanMarket 봉은 변동이 없어 ATR 0
        self.assertEqual([r["atr_pct"] for r in found], [0.0, 0.0])
        self.assertEqual(scan(ScanMarket(), {**q, "atr_min": "1"})["found"], [])


if __name__ == "__main__":
    unittest.main()
