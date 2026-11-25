import unittest

from binancebot.maweb import atr_pct, backtest, mtf_line, tf_line, moving_average, near_line, parse_ma, pullback, run, scan


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

    def test_exit_line_break(self):
        # 100 에서 삼 → 종가가 청산선 101 위(102) 에 한 번 있다가 아래(100.5)로 마감 → 다음 봉 시가 청산
        b = self.rows([(103, 104, 99.5, 100.5), (101, 103, 100.5, 102), (102, 102, 100, 100.5),
                       (100.4, 100.4, 100.2, 100.3), (100, 100, 100, 100)])
        ex = [101.0] * len(b)
        r = pullback(b, [100.0] * len(b), stop_pct=0, exit_line=ex, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["reason"], t["exit"], t["exit_time"]), ("exit", 100.4, b[9]["t"]))
        # 큰 봉이 끝나는 봉에서만 판단하면 (8번째 봉은 아님) 청산 안 됨
        ends = [False] * len(b)
        r2 = pullback(b, [100.0] * len(b), stop_pct=0, exit_line=ex, exit_ends=ends, fee_pct=0)
        self.assertEqual(r2["trades"], [])

    def test_direction_filter(self):
        # 가격이 선 100 위 → 롱 대기 조건. 빠른 선 < 느린 선(역배열)이면 롱 대기 없음
        b = self.rows([(105, 106, 104, 105)])
        n = len(b)
        r = pullback(b, [100.0] * n, side="both", dir_fast=[50.0] * n, dir_slow=[60.0] * n)
        self.assertEqual(r["wait_orders"], [])
        r2 = pullback(b, [100.0] * n, side="both", dir_fast=[60.0] * n, dir_slow=[50.0] * n)
        self.assertEqual([w["dir"] for w in r2["wait_orders"]], [1])

    def test_win_reset_needs_new_high(self):
        touch, win = (101, 102, 99.5, 101), (101, 104, 100.5, 103.5)   # 선 100 닿아 롱 → +3% 익절
        mid = [(103, 105, 102, 104)] * 6                                # 진입 전 고점 106 을 못 넘음
        b = self.rows([touch, win] + mid + [touch, (101, 101, 101, 101)])
        line = [100.0] * len(b)
        kw = dict(stop_pct=0, tp_pct=3, fee_pct=0)
        self.assertIsNotNone(pullback(b, line, **kw)["open"])            # 조건 없으면 두 번째 진입
        r = pullback(b, line, win_reset=20, **kw)
        self.assertEqual(len(r["trades"]), 1)
        self.assertIsNone(r["open"])                                     # 새 고점 없이 되돌림 → 안 삼
        mid2 = mid[:3] + [(104, 107, 103, 106)] + mid[3:5]               # 106 위로 새 고점 → 되돌림에 다시 삼
        b2 = self.rows([touch, win] + mid2 + [touch, (101, 101, 101, 101)])
        self.assertIsNotNone(pullback(b2, line, win_reset=20, **kw)["open"])

    def test_entry_pct_before_line(self):
        # 저가 100.3 은 선 100 에 못 닿지만 0.5% 앞 100.5 에는 닿음
        b = self.rows([(103, 104, 100.3, 101), (101, 101, 101, 101)])
        r = pullback(b, [100.0] * len(b), stop_pct=0, fee_pct=0)
        self.assertIsNone(r["open"])
        r2 = pullback(b, [100.0] * len(b), stop_pct=0, fee_pct=0, entry_pct=0.5)
        self.assertAlmostEqual(r2["open"]["entry"], 100.5)
        down = ohlc([(96, 97, 94, 95)] * 6 + [(97, 99.6, 96, 99)])
        r3 = pullback(down, [100.0] * len(down), side="short", stop_pct=0, fee_pct=0, entry_pct=0.5)
        self.assertAlmostEqual(r3["open"]["entry"], 99.5)

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

    def test_multi_lines_first_touched_and_stop_same(self):
        # 진입선 2개: 100 과 90. 둘 다 아래로 뚫는 봉이면 먼저 닿는 높은 선(100)으로 진입
        b = self.rows([(103, 104, 85, 95), (95, 95, 95, 95)])
        r = pullback(b, [100.0] * len(b), extra_lines=[[90.0] * len(b)], stop_pct=50, fee_pct=0)
        self.assertEqual((r["open"]["entry"], r["open"]["line"]), (100.0, 0))
        # 첫 선(120)은 가격 위라 대기 안 됨 → 90 선에서 진입, 손절은 진입한 90 선 종가 이탈
        b = self.rows([(103, 104, 89, 92), (92, 92, 88, 88), (87, 87, 87, 87), (87, 87, 87, 87)])
        r = pullback(b, [120.0] * len(b), extra_lines=[[90.0] * len(b)], stop_mode="line", stop_pct=0,
                     stop_same=True, fee_pct=0)
        t = r["trades"][0]
        self.assertEqual((t["entry"], t["line"], t["reason"], t["exit"]), (90.0, 1, "line", 87))
        # 지정가 대기는 방향마다 가격이 먼저 닿는 선 하나
        b = self.rows([(104, 106, 103, 105)])
        r = pullback(b, [100.0] * len(b), extra_lines=[[90.0] * len(b), [95.0] * len(b)], fee_pct=0)
        self.assertEqual(r["wait_orders"], [{"dir": 1, "price": 100.0, "line": 0}])

    def test_opposite_line_take_and_block(self):
        # 진입선 100(아래, 롱) + 102.5(위, 반대 방향): 100 에 롱 진입 → 위 선까지 2.5% ≤ 3% → 102.5 에서 익절
        flat = [(101, 101.5, 100.5, 101)] * 6
        b = ohlc(flat + [(101, 101, 99.5, 100.2), (100.2, 103, 100, 102.8), (102.8, 102.8, 102.8, 102.8)])
        kw = dict(stop_pct=5, fee_pct=0, opp_tp=3, opp_block=1)
        r = pullback(b, [100.0] * len(b), extra_lines=[[102.5] * len(b)], **kw)
        t = r["trades"][0]
        self.assertEqual((t["entry"], t["exit"], t["reason"]), (100.0, 102.5, "take"))
        # 끄면 익절 없음 → 보유 중
        self.assertIsNotNone(pullback(b, [100.0] * len(b), extra_lines=[[102.5] * len(b)], stop_pct=5, fee_pct=0)["open"])
        # 위 선까지 0.8% < 1% → 진입 안 함, 지정가 대기도 없음
        flat = [(100.5, 100.7, 100.3, 100.5)] * 6
        b = ohlc(flat + [(100.5, 100.5, 99.5, 100.2), (100.2, 100.6, 100, 100.4)])
        r = pullback(b, [100.0] * len(b), extra_lines=[[100.8] * len(b)], **kw)
        self.assertEqual(r["trades"], [])
        self.assertIsNone(r["open"])
        self.assertEqual(pullback(ohlc(flat), [100.0] * 6, extra_lines=[[100.8] * 6], **kw)["wait_orders"], [])

    def test_far_reverse_entry(self):
        # 숏 대기(가격이 진입선 100 아래) 중 종가가 선에서 5% 넘게 아래(94.5) → 다음 봉 시가 롱, 손절 -1% · 익절 +3%
        flat = [(98, 98.5, 97.5, 98)] * 6
        cross = [(98, 98, 94, 94.5)]
        kw = dict(side="short", stop_mode="line", stop_pct=0.5, fee_pct=0, far_pct=5, far_sl=1, far_tp=3)
        b = ohlc(flat + cross + [(94.5, 95, 94.2, 94.8), (94.8, 97.5, 94.6, 97), (97, 97, 97, 97)])
        r = pullback(b, [100.0] * len(b), **kw)
        t = r["trades"][0]
        self.assertEqual((t["dir"], t["entry"], t["reason"], t["far"]), ("long", 94.5, "take", True))
        self.assertAlmostEqual(t["exit"], 94.5 * 1.03)
        # 넘어가는 봉이 끝난 직후: 진행 중인 봉 시가에 롱 (실거래 봇은 far_open 을 보고 시장가)
        b2 = ohlc(flat + cross + [(94.5, 94.6, 94.4, 94.5)])
        r2 = pullback(b2, [100.0] * len(b2), **kw)
        self.assertEqual(r2["far_open"]["dir"], "long")
        self.assertEqual(r2["far_open"]["t"], b2[-1]["t"])
        self.assertAlmostEqual(r2["open"]["sl"], 94.5 * 0.99)
        # 손절 -1%
        b3 = ohlc(flat + cross + [(94.5, 94.6, 93.0, 93.2), (93.2, 93.2, 93.2, 93.2)])
        t3 = pullback(b3, [100.0] * len(b3), **kw)["trades"][0]
        self.assertEqual((t3["reason"], t3["far"]), ("stop", True))
        self.assertAlmostEqual(t3["exit"], 94.5 * 0.99)
        # 이미 5% 넘게 벗어나 있던 상태가 이어지면 (넘어가는 순간이 아니면) 진입 안 함 / 끄면 없음
        far_flat = [(94, 94.5, 93.5, 94)] * 8
        self.assertEqual(pullback(ohlc(far_flat), [100.0] * 8, **kw)["trades"], [])
        self.assertEqual(pullback(b, [100.0] * len(b), **{**kw, "far_pct": 0})["trades"], [])
        # 마지막 자리에서만: 아래에 진입선(90)이 더 있으면 100 에서 5% 벗어나도 반대 진입 안 함
        kw2 = {**kw, "side": "both"}
        r4 = pullback(b, [100.0] * len(b), extra_lines=[[90.0] * len(b)], **kw2)
        self.assertFalse(any(t.get("far") for t in r4["trades"]))

    def test_run_multi_lines_same_stop(self):
        q = {"strat": "pullback", "interval": "5m", "pb_ma": "sma:2", "pb2_ma": "sma:4", "pb3_ma": "sma:6",
             "stop_mode": "line", "stop_ma": "same", "sl": "0", "side": "both", "_live": "1"}
        d = run(FakeMarket(), q)
        self.assertEqual(len(d["live"]["pb_lines"]), 3)
        self.assertTrue(d["live"]["stop_same"])
        self.assertIn("pb3", d["rule_lines"])
        self.assertNotIn("stop", d["rule_lines"])


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


class TfLineTest(unittest.TestCase):
    def test_no_lookahead_partial_bar(self):
        # 15분봉 2개(끝난 것 1 + 진행 중) 를 5분봉 차트에: 진행 중인 15분봉 종가 대신 그때까지의 5분봉 종가를 씀
        big = [{"t": 0, "c": 10, "v": 1}, {"t": 900, "c": 99, "v": 3}]
        chart = [{"t": 900 + i * 300, "c": c, "v": 1} for i, c in enumerate([20, 30, 40])]
        got = tf_line(chart, 300, big, 900, "sma", 2)
        self.assertEqual(got, [15, 20, 25])  # (10+20)/2, (10+30)/2, (10+40)/2 — 99 는 안 씀
        vw = tf_line(chart, 300, big, 900, "vwma", 2)
        self.assertAlmostEqual(vw[1], (10 * 1 + 30 * 2) / 3)  # 진행 중 봉 거래량 = 지금까지 쌓인 2

    def test_run_with_line_tf(self):
        d = run(FakeMarket(), {"symbol": "x", "interval": "5m", "ma1": "sma:2", "tf1": "15m", "ma2": "sma:2", "tf2": "1m"})
        self.assertEqual(d["lines"][0]["tf"], "15m")
        self.assertIn("선 2", d["warn"])

    def test_rule_lines_own_kind_and_tf(self):
        q = {"symbol": "x", "interval": "5m", "strat": "pullback", "ma1": "sma:2",
             "stop_mode": "touch", "stop_ma": "vwma:3", "stop_tf": "15m",
             "tp_mode": "pct", "tp_ma": "vwma:2", "exit_ma": "sma:2", "exit_tf": "5m", "entry_pct": "0.5"}
        d = run(FakeMarket(), q)
        self.assertEqual(d["rule_lines"]["stop"]["tf"], "15m")
        self.assertEqual(d["rule_lines"]["stop"]["kind"], "vwma")
        self.assertIn("exit", d["rule_lines"])
        self.assertNotIn("tp", d["rule_lines"])          # 익절이 % 면 선 안 그림
        d2 = run(FakeMarket(), {**q, "exit_ma": "off"})
        self.assertNotIn("exit", d2["rule_lines"])
        with self.assertRaises(ValueError):
            run(FakeMarket(), {**q, "entry_pct": "2"})  # 0.1~1 밖


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


class PrefsTest(unittest.TestCase):
    def test_prefs_saved_and_shared(self):
        import tempfile
        from binancebot import maweb
        with tempfile.TemporaryDirectory() as d:
            old = maweb.LIVE_DIR
            maweb.LIVE_DIR = d
            try:
                self.assertEqual(maweb.load_prefs(), {})
                maweb.save_pref("maweb_live", '{"usdt": 50, "lev": 3}')
                self.assertEqual(maweb.load_prefs(), {"maweb_live": '{"usdt": 50, "lev": 3}'})
                with self.assertRaises(ValueError):
                    maweb.save_pref("other", "{}")
                with self.assertRaises(ValueError):
                    maweb.save_pref("maweb", "not json")
            finally:
                maweb.LIVE_DIR = old
        self.assertTrue(maweb.same_origin("http://100.70.1.2:8790", "100.70.1.2:8790"))
        self.assertFalse(maweb.same_origin("http://evil.example", "localhost:8790"))
        self.assertFalse(maweb.same_origin("", "localhost:8790"))


if __name__ == "__main__":
    unittest.main()


class TrustedClientTest(unittest.TestCase):
    def test_trusted(self):
        from binancebot.maweb import trusted_client
        self.assertTrue(trusted_client("127.0.0.1", "", "localhost:8790"))
        self.assertTrue(trusted_client("127.0.0.1", "http://localhost:8790", "localhost:8790"))
        self.assertFalse(trusted_client("127.0.0.1", "https://evil.example", "localhost:8790"))
        # Tailscale 폰에서 이 서버를 직접 연 화면
        self.assertTrue(trusted_client("100.74.132.1", "http://100.69.23.13:8790", "100.69.23.13:8790"))
        self.assertTrue(trusted_client("100.74.132.1", "", "100.69.23.13:8790"))
        # Tailscale 기기라도 다른 사이트에서 보낸 요청은 막음
        self.assertFalse(trusted_client("100.74.132.1", "https://evil.example", "100.69.23.13:8790"))
        # 같은 와이파이(사설망) 기기는 막음
        self.assertFalse(trusted_client("192.168.0.5", "http://192.168.0.3:8790", "192.168.0.3:8790"))
        self.assertFalse(trusted_client("100.128.0.1", "", "x"))   # 100.64.0.0/10 밖
