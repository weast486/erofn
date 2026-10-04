import csv
import gzip
import tempfile
import unittest
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from binancebot.backtest import ET, Bar, MinBar, Params, flag_setups, orb_trade, run, first5_trades, rsi_values, rsi_trades, vbreak_setups, surge_trade, swing_setups, vwap_trade, vwma_trades
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


class VwapSurgeTest(unittest.TestCase):
    def test_vwap_long_back_to_vwap(self):
        p = Params(strategy="vwap", vwap_dev=2, vwap_start=time(9, 30), stop_pct=5, rule=SizeRule(2, 5, 0, 0))
        bars = bars_from([(100, 100, 100, 100), (100, 100, 100, 100),
                          (99, 99, 97, 97.5),     # VWAP 100 의 -2% = 98 지정가 매수
                          (97.5, 99.9, 97.4, 99.5),
                          (99.5, 101, 99.4, 100.5)])  # VWAP(약 99.6) 닿음 → 청산
        t = vwap_trade("X", date(2026, 9, 1), bars, p, 0)
        self.assertEqual((t.side, t.entry, t.reason), (1, 98, "target"))
        self.assertTrue(99 < t.exit < 100)
        self.assertEqual(t.fee_entry, 0.02)

    def test_surge_requires_open_below_prev_close(self):
        p = Params(strategy="surge", surge_min_change=10, buy_until=time(10, 0), stop_pct=5, take_profit_pct=5,
                   rule=SizeRule(2, 5, 0, 0))
        bars = bars_from([(98, 99, 97, 98.5), (98.5, 100.5, 98.4, 100.2), (100.2, 105.5, 100, 105)])
        t = surge_trade("X", date(2026, 9, 1), bars, p, 0, prev_close=100, prev_change=12)
        self.assertEqual((t.entry, t.reason, t.exit), (100, "target", 105))
        self.assertIsNone(surge_trade("X", date(2026, 9, 1), bars, p, 0, prev_close=100, prev_change=8))
        self.assertIsNone(surge_trade("X", date(2026, 9, 1), bars, p, 0, prev_close=97, prev_change=12))


class VwmaTest(unittest.TestCase):
    def test_long_pullback_to_vwma(self):
        p = Params(strategy="vwma", vwma_tf=1, vwma_len=3, vwma_min_above=2, vwap_start=time(9, 30),
                   stop_pct=5, vwma_target_r=1, rule=SizeRule(2, 5, 0, 0))
        t0 = datetime.combine(date(2026, 9, 1), time(9, 30), ET)
        px = [(100, 100, 100, 100), (101, 101, 101, 101), (102, 102, 102, 102), (103, 103, 103, 103),
              (104, 104, 104, 104),
              (104, 104, 102, 103),   # 직전 VWMA(102,103,104 평균 103) 지정가 → 103 매수
              (103, 109, 103, 108)]   # 손절 5% → 위험 5.15, 1R = 108.15 익절
        mins = [MinBar(t0 + timedelta(minutes=i), o, h, l, c, 1) for i, (o, h, l, c) in enumerate(px)]
        trades = vwma_trades("X", mins, p)
        self.assertAlmostEqual(trades[0].exit, 108.15)
        self.assertEqual((trades[0].side, trades[0].entry, trades[0].reason), (1, 103, "target"))

    def test_max_touch(self):
        # 위에 자리 잡은 뒤 1번째 닿음은 1분봉 5, 2번째는 7 → 최대 1번이면 7 에서는 진입 안 함
        p = Params(strategy="vwma", vwma_tf=1, vwma_len=3, vwma_min_above=1, vwma_slope_bars=0, vwap_start=time(9, 30),
                   stop_pct=50, take_profit_pct=1, rule=SizeRule(2, 5, 0, 0))
        t0 = datetime.combine(date(2026, 9, 1), time(9, 30), ET)
        px = [(100, 100, 100, 100), (101, 101, 101, 101), (102, 102, 102, 102), (103, 103, 103, 103),
              (104, 104, 104, 104), (104, 105, 102, 104.5), (104.5, 106, 104.4, 105.5), (105, 105, 103, 104.5),
              (104.5, 107, 104.4, 106)]
        mins = [MinBar(t0 + timedelta(minutes=i), o, h, l, c, 1) for i, (o, h, l, c) in enumerate(px)]
        self.assertEqual(len(vwma_trades("X", mins, p)), 2)
        p.vwma_max_touch = 1
        self.assertEqual(len(vwma_trades("X", mins, p)), 1)


class RsiTest(unittest.TestCase):
    def test_rsi(self):
        self.assertEqual(rsi_values([1, 2, 3, 4], 3)[3], 100.0)
        r = rsi_values([10, 11, 10, 11, 10, 11], 2)
        self.assertIsNone(r[1])
        self.assertTrue(0 < r[5] < 100)


class SwingTest(unittest.TestCase):
    def test_higher_lows_highs(self):
        t0 = datetime(2026, 9, 1, 9, 30, tzinfo=ET)
        # 저 10 → 고 20 → 저 14 → 고 24 (n=1), 고점2 는 다음 봉이 끝나야 확정
        hl = [(15, 12), (13, 10), (20, 15), (18, 14), (24, 18), (22, 19), (21, 18)]
        bars = [(t0 + timedelta(minutes=15 * i), (h + l) / 2, h, l, (h + l) / 2) for i, (h, l) in enumerate(hl)]
        out = swing_setups(bars, 1)
        self.assertEqual([(o[1], o[2]) for o in out], [(14, 24)])
        self.assertEqual(out[0][0], bars[6][0])


class FlagTest(unittest.TestCase):
    def test_bull_flag(self):
        t0 = datetime(2026, 9, 1, 9, 30, tzinfo=ET)
        # 깃대 100 → 106 (2봉), 깃발 3봉 고점 105·104·104, 저점 103 (되돌림 3/6 = 0.5)
        hl = [(101, 100), (103, 100.5), (106, 102.5), (105, 103.5), (104, 103), (104, 103.2), (104.5, 103.5)]
        bars = [(t0 + timedelta(minutes=15 * i), 0, h, l, 0, 1.0) for i, (h, l) in enumerate(hl)]
        p = Params(flag_pole_bars=3, flag_pole_pct=5, flag_min=3, flag_max=8, flag_retrace=0.5)
        out = flag_setups(bars, p)
        self.assertEqual(out[bars[6][0]], (105, 103, 6))
        self.assertNotIn(bars[5][0], out)  # 깃발 2봉뿐
        self.assertEqual(flag_setups(bars, Params(flag_pole_bars=3, flag_pole_pct=5, flag_min=3, flag_retrace=0.4)), {})
        self.assertEqual(flag_setups(bars, Params(flag_pole_bars=3, flag_pole_pct=7, flag_min=3)), {})


class VbreakTest(unittest.TestCase):
    def test_short_after_failed_recovery(self):
        t0 = datetime(2026, 9, 1, 9, 30, tzinfo=ET)
        closes = [10, 11, 12, 13, 14, 15, 16, 13.5, 13.4, 13.3, 13.2, 13.2]
        bars = [(t0 + timedelta(minutes=15 * i), c, c + 0.1, c - 0.1, c, 1.0) for i, c in enumerate(closes)]
        bars[7] = (bars[7][0], 16, 16.2, 13.4, 13.5, 1.0)  # 하향 돌파 봉, 고가 16.2 = 손절
        p = Params(vb_len=4, vb_fast=2, vb_mid=3, vb_wait=3)
        out = vbreak_setups(bars, p)
        self.assertNotIn(bars[10][0], out)  # 3봉이 다 지나기 전
        side, lvl, stop, _ = out[bars[11][0]]
        self.assertEqual((side, stop), (-1, 16.2))
        self.assertAlmostEqual(lvl, (13.5 + 13.4 + 13.3 + 13.2) / 4)
        bars[9] = (bars[9][0], 13.3, 15, 13.2, 14.9, 1.0)  # 회복 → 취소
        self.assertEqual(vbreak_setups(bars, p), {})


class RsiIntradayTest(unittest.TestCase):
    def test_buy_oversold_sell_on_recovery(self):
        t0 = datetime(2026, 9, 1, 9, 30, tzinfo=ET)
        closes = [100 - i for i in range(10)] + [91 + 2 * i for i in range(10)]  # 계속 내리다 반등
        mins = [MinBar(t0 + timedelta(minutes=i), c, c + 0.1, c - 0.1, c, 1.0) for i, c in enumerate(closes)]
        p = Params(strategy="rsi", rsi_tf=1, rsi_period=3, rsi_buy=30, rsi_sell=50, stop_pct=0,
                   vwap_start=time(9, 30), rule=SizeRule(2, 5, 0, 0))
        trades, st = rsi_trades("X", mins, p)
        self.assertEqual(trades[0].side, 1)
        self.assertEqual(trades[0].reason, "signal")
        self.assertEqual(trades[0].entry_t, t0 + timedelta(minutes=4))  # 3번째 하락 뒤(RSI 0) 다음 봉 시가
        self.assertGreater(trades[0].exit, trades[0].entry - 5)


class First5Test(unittest.TestCase):
    def _mins(self, d, pxs, start=time(9, 30)):
        t0 = datetime.combine(d, start, ET)
        return [MinBar(t0 + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(pxs)]

    def test_breakout_close_then_targets(self):
        prev = self._mins(date(2026, 8, 31), [(100, 100, 100, 100)] * 5, start=time(15, 50))
        first = [(100, 100.5, 99.8, 100.4), (100.4, 101, 100.3, 100.9), (100.9, 101, 100.8, 101), (101, 101, 100.9, 101), (101, 101, 100.9, 101)]
        second = [(101, 101.2, 100.9, 101.1)] * 4 + [(101.1, 101.6, 101.0, 101.5)]   # 9:35~9:40 봉 종가 101.5 > 첫 봉 고가 101
        after = [(101.5, 103.6, 101.4, 103.5), (103.5, 106.7, 103.4, 106.5)]           # +2% 절반, +5% 나머지
        mins = prev + self._mins(date(2026, 9, 1), first + second + after)
        p = Params(strategy="first5", f5_entry="close", f5_min_body=0.5, f5_stop_pct=2, f5_tp1=2, f5_tp2=5,
                   exit_time=time(10, 0), rule=SizeRule(2, 5, 0, 0))
        trades, st = first5_trades("X", mins, p)
        self.assertEqual(st["entries"], 1)
        self.assertEqual([(t.reason, t.size_frac) for t in trades], [("target", 0.5), ("target", 0.5)])
        self.assertAlmostEqual(trades[0].entry, 101.5)

    def test_short_mirror(self):
        prev = self._mins(date(2026, 8, 31), [(100, 100, 100, 100)] * 5, start=time(15, 50))
        first = [(100, 100.2, 99.5, 99.6), (99.6, 99.7, 99.0, 99.1), (99.1, 99.2, 99.0, 99.0), (99, 99.1, 99, 99), (99, 99.1, 99, 99)]
        second = [(99, 99.1, 98.8, 98.9)] * 4 + [(98.9, 99.0, 98.4, 98.5)]   # 5분봉 종가 98.5 < 첫 봉 저가 99
        after = [(98.5, 98.6, 96.4, 96.5)]                                     # -2% 전량 익절
        mins = prev + self._mins(date(2026, 9, 1), first + second + after)
        p = Params(strategy="first5", f5_entry="close", f5_side="short", f5_min_body=0.5, f5_stop_pct=2, f5_tp1=2,
                   f5_split=False, exit_time=time(10, 0), rule=SizeRule(2, 5, 0, 0))
        trades, st = first5_trades("X", mins, p)
        self.assertEqual([(t.side, t.reason) for t in trades], [(-1, "target")])
        self.assertAlmostEqual(trades[0].exit, 98.5 * 0.98)


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
