import unittest

from binancebot.obweb import engulfing, order_blocks, overlaps, run


def bar(t, o, c, v, h=None, l=None):
    return {"t": t, "o": o, "h": h if h is not None else max(o, c), "l": l if l is not None else min(o, c), "c": c, "v": v}


class EngulfTest(unittest.TestCase):
    def test_bull_needs_more_volume(self):
        p = bar(0, 10, 9, 100)
        self.assertEqual(engulfing(p, bar(1, 8.9, 10.5, 150)), 1)
        self.assertEqual(engulfing(p, bar(1, 8.9, 10.5, 100)), 0)  # 같은 거래량은 아님
        self.assertEqual(engulfing(p, bar(1, 9.2, 10.5, 150)), 0)  # 몸통을 못 감쌈

    def test_bear(self):
        self.assertEqual(engulfing(bar(0, 9, 10, 100), bar(1, 10.1, 8.5, 200)), -1)
        self.assertEqual(engulfing(bar(0, 9, 10, 100), bar(1, 10.1, 9.5, 200)), 0)


class OrderBlockTest(unittest.TestCase):
    def test_zone_and_broken(self):
        bars = [bar(0, 10, 9, 100, h=10.2, l=8.8), bar(1, 8.9, 10.5, 150), bar(2, 10.5, 9.5, 50),
                bar(3, 9.5, 8.5, 50), bar(4, 8.5, 8.6, 10)]
        zs = order_blocks(bars)
        self.assertEqual(len(zs), 1)
        z = zs[0]
        self.assertEqual((z["dir"], z["top"], z["bottom"]), ("bull", 10, 9))
        self.assertEqual(z["touches"], 1)  # 2번 봉이 자리 안으로 들어옴
        self.assertEqual(z["broken_t"], 3)  # 3번 봉 종가 8.5 < 9
        self.assertEqual(order_blocks(bars, "wick")[0]["bottom"], 8.8)

    def test_last_bar_not_used(self):
        bars = [bar(0, 10, 9, 100), bar(1, 8.9, 10.5, 150)]
        self.assertEqual(order_blocks(bars), [])


class OverlapTest(unittest.TestCase):
    def z(self, d, bot, top, t=0, broken=None):
        return {"dir": d, "bottom": bot, "top": top, "t": t, "broken_t": broken}

    def test_overlap_two_tfs(self):
        zones = {"1h": [self.z("bull", 100, 110, t=5)], "15m": [self.z("bull", 105, 120, t=9)],
                 "5m": [self.z("bull", 108, 109, broken=10), self.z("bear", 100, 120)]}
        ov = overlaps(zones)
        self.assertEqual(len(ov), 1)
        self.assertEqual((ov[0]["bottom"], ov[0]["top"], ov[0]["tfs"], ov[0]["start"]), (105, 110, ["1h", "15m"], 9))
        mixed = overlaps(zones, same_dir=False)
        self.assertEqual([(o["bottom"], o["top"], o["tfs"]) for o in mixed],
                         [(100, 105, ["1h", "5m"]), (105, 110, ["1h", "15m", "5m"]), (110, 120, ["15m", "5m"])])
        self.assertEqual([o["tfs"] for o in overlaps(zones, min_tfs=3, same_dir=False)], [["1h", "15m", "5m"]])

    def test_same_tf_not_counted(self):
        self.assertEqual(overlaps({"1h": [self.z("bull", 1, 3), self.z("bull", 2, 4)]}), [])


class RunTest(unittest.TestCase):
    def test_run(self):
        class FakeMarket:
            def bars(self, symbol, tf, count):
                return [bar(0, 10, 9, 100), bar(1, 8.9, 10.5, 150), bar(2, 10.5, 10.2, 50), bar(3, 10.2, 10.3, 50)]
        d = run(FakeMarket(), {"symbol": "btcusdt", "interval": "15m"})
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertEqual(len(d["zones"]["1d"]), 1)
        self.assertEqual(d["overlaps"][0]["tfs"], ["1d", "4h", "1h", "15m", "5m"])
        self.assertFalse(d["overlaps"][0]["inside"])


if __name__ == "__main__":
    unittest.main()


class SimulateTest(unittest.TestCase):
    def zone(self, d, bot, top, t=0, te=1, broken=None):
        return {"dir": d, "bottom": bot, "top": top, "t": t, "t_engulf": te, "broken_t": broken}

    def test_long_take_profit(self):
        from binancebot.obweb import simulate
        # 1h 봉(3600초) 위에 1h 자리 9~10 (장악 봉 시각 0 → 3600 부터 씀)
        H = 3600
        bars = [bar(0, 11, 11, 1), bar(H, 11, 11.5, 1), bar(2 * H, 11.5, 10.5, 1, l=9.8), bar(3 * H, 10.5, 12, 1),
                bar(4 * H, 12, 12.5, 1, h=12.6), bar(5 * H, 12.5, 12.5, 1)]
        z = {"1h": [self.zone("bull", 9, 10, t=0, te=0)]}
        r = simulate(bars, "1h", z, use_tfs=["1h"], tp_r=2, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (10, 12, "take"))  # 위험 1 x 2R
        # 처음 닿을 때만 → 두 번째 매수 없음
        self.assertEqual(len(r["trades"]), 1)

    def test_stop_and_not_ready(self):
        from binancebot.obweb import simulate
        H = 3600
        bars = [bar(0, 11, 11, 1), bar(H, 11, 10.5, 1, l=9.5), bar(2 * H, 10.5, 11, 1), bar(3 * H, 11, 9.5, 1, l=8.5),
                bar(4 * H, 9, 9, 1)]
        # 장악 봉이 1h 에 끝나는 4시간 자리는 아직 모르는 자리 → 매매 없음
        late = {"4h": [self.zone("bull", 9, 10, t=0, te=0)]}
        self.assertEqual(simulate(bars, "1h", late, use_tfs=["4h"])["trades"], [])
        ok = {"1h": [self.zone("bull", 9, 10, t=0, te=0)]}
        r = simulate(bars, "1h", ok, use_tfs=["1h"], tp_r=5, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["entry_time"], t["entry"], t["exit"], t["reason"]), (H, 10, 9, "stop"))
