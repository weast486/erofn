import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tossbot.backtest import BacktestSettings, SELL_TAX_BY_YEAR, load_cache, run_backtest, _write_bars
from tossbot.broker import round_down_to_tick, round_up_to_tick
from tossbot.selector import Bar, PullbackParams, analyze, touch_zone

START = date(2024, 1, 1)
# 엔진(체결·청산·비용) 검증용 시나리오는 7일선 기준으로 만들어져 있으므로 명시적으로 고정
P7 = PullbackParams(ma_period=7)


def weekdays(n, start=date(2023, 11, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def make_stock(after_entry):
    """40일 보합(1만원) → +15% 급등(거래량 3배) → 3일 거래량 줄며 눌림 → 4일째 7일선 터치 → after_entry(o,h,l,c)."""
    days = weekdays(60)
    bars = [Bar(days[i], 10_000, 10_050, 9_950, 10_000, 1_000_000) for i in range(40)]
    for c in (11_500, 11_300, 11_100, 10_950):  # 급등일 + 눌림 3일
        vol = 3_000_000 if c == 11_500 else 500_000
        bars.append(Bar(days[len(bars)], c, c * 1.01, c * 0.99, c, vol))
    # 진입일: 7일선 터치 구간 상단(hi) 위에서 시작해 hi 아래까지 내려옴
    item, reason = analyze("X", "X", bars, days[len(bars)], P7)
    assert item, reason
    lo, hi = touch_zone(item, P7)
    bars.append(Bar(days[len(bars)], hi * 1.01, hi * 1.01, hi * 0.995, hi, 600_000))
    for o, h, l, c in after_entry(hi):
        bars.append(Bar(days[len(bars)], o, h, l, c, 600_000))
    return bars, hi


class BacktestEngineTest(unittest.TestCase):
    def run_one(self, after_entry, **kw):
        bars, hi = make_stock(after_entry)
        s = BacktestSettings(params=P7, slippage=0.0, **kw)
        res = run_backtest({"000010": ("테스트", bars)}, date(2023, 12, 1), date(2024, 3, 31), s)
        return res, hi

    def test_take_profit(self):
        res, hi = self.run_one(lambda hi: [(hi, hi * 1.2, hi * 0.99, hi * 1.18)])
        (t,) = res.trades
        self.assertEqual(t.entry_price, hi)
        self.assertEqual(t.qty, 100_000 // round_up_to_tick(hi * 1.005))
        self.assertEqual(t.reason, "TAKE_PROFIT")
        self.assertEqual(t.exit_price, round_up_to_tick(hi * 1.15))
        tax = SELL_TAX_BY_YEAR[t.exit_date.year]
        expected = t.qty * t.exit_price * (1 - 0.00015 - tax) - t.qty * hi * 1.00015
        self.assertAlmostEqual(t.pnl, expected, places=6)
        self.assertAlmostEqual(res.final_equity, 1_000_000 + t.pnl, places=6)

    def test_stop_loss_gap_down_fills_at_open(self):
        res, hi = self.run_one(lambda hi: [(hi * 0.9, hi * 0.92, hi * 0.88, hi * 0.9)])
        (t,) = res.trades
        self.assertEqual((t.reason, t.exit_price), ("STOP_LOSS", hi * 0.9))

    def test_stop_loss_intraday(self):
        res, hi = self.run_one(lambda hi: [(hi, hi * 1.01, hi * 0.9, hi * 0.95)])
        (t,) = res.trades
        self.assertEqual((t.reason, t.exit_price), ("STOP_LOSS", round_down_to_tick(hi * 0.953)))

    def test_time_exit_after_10_days_and_cooldown(self):
        flat = lambda hi: [(hi, hi * 1.01, hi * 0.99, hi * 1.001 + i) for i in range(12)]
        res, hi = self.run_one(flat)
        (t,) = res.trades  # 청산 후 쿨다운 동안 재매수 없음
        self.assertEqual((t.reason, t.hold_days), ("TIME_EXIT", 10))

    def test_no_entry_when_rules_disabled_by_params(self):
        s = PullbackParams(ma_period=7, min_days_after_surge=5)  # 급등 후 4일째 진입이므로 막혀야 함
        bars, _ = make_stock(lambda hi: [])
        res = run_backtest({"000010": ("테스트", bars)}, date(2023, 12, 1), date(2024, 3, 31),
                           BacktestSettings(params=s))
        self.assertEqual(res.trades, [])

    def test_cache_roundtrip(self):
        bars, _ = make_stock(lambda hi: [])
        with tempfile.TemporaryDirectory() as tmp:
            _write_bars(Path(tmp) / "000010.csv",
                        [(b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume) for b in bars])
            data = load_cache(Path(tmp))
        self.assertEqual(data["000010"][1], bars)


if __name__ == "__main__":
    unittest.main()


class BreakoutEngineTest(unittest.TestCase):
    def test_20d_high_entry_and_10d_low_exit(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(60)
        closes = [10_000] * 30 + [10_500, 11_000, 11_500] + [11_400, 11_300] + [9_000] + [9_000] * 24
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0), BreakoutSettings())
        # 첫 신고가(10,500) 에 매수 → 9,000 으로 10일 신저가 이탈한 날 종가에 매도
        (t,) = res.trades
        self.assertEqual((t.entry_date, t.entry_price), (days[30], 10_500))
        self.assertEqual((t.exit_date, t.exit_price, t.reason), (days[35], 9_000, "LOW_10D"))

    def test_engulf_below_ma(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(40)
        closes = [10_000] * 30 + [9_000] * 5
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        bars[32] = Bar(days[32], 9_200, 9_250, 8_800, 8_900, 1_000_000)  # 음봉
        bars[33] = Bar(days[33], 8_850, 9_300, 8_800, 9_250, 1_000_000)  # 감싸는 양봉 (20일선 아래)
        b = BreakoutSettings(require_new_high=False, engulf=True, below_ma=20, exit_on_low=False)
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0), b)
        self.assertEqual([t.entry_date for t in res.trades], [days[33]])
        b.below_ma = 3  # 3일선 위라서 제외
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0), b)
        self.assertEqual(res.trades, [])

    def test_breakout_stop_loss(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(40)
        closes = [10_000] * 30 + [10_500, 10_400]
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        bars[31] = Bar(days[31], 10_400, 10_450, 9_900, 10_400, 1_000_000)  # 저가가 손절가(10,000) 이하
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0), BreakoutSettings(stop_loss_pct=4.7))
        (t,) = res.trades
        self.assertEqual((t.reason, t.exit_price), ("STOP_LOSS", round_down_to_tick(10_500 * 0.953)))

    def test_breakout_skips_limit_up_close(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(40)
        closes = [10_000] * 30 + [13_000, 13_500]  # 신고가 날 +30% 상한가 마감
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        run = lambda b: run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),  # noqa: E731
                                     BacktestSettings(slippage=0.0), b)
        # 상한가 날은 건너뛰고, 다음 날(13,500 신고가, +3.8%)에 매수
        (t,) = run(BreakoutSettings()).trades
        self.assertEqual(t.entry_date, days[31])
        self.assertEqual(run(BreakoutSettings(skip_limit_up=False)).trades[0].entry_date, days[30])


class LimitUpEngineTest(unittest.TestCase):
    def _bars(self, next_day):
        days = weekdays(40)
        closes = [10_000] * 30 + [13_000]  # 30번째 날 상한가 마감
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        bars.append(Bar(days[31], *next_day, 1_000_000))
        return days, bars

    def run_one(self, next_day):
        from tossbot.backtest import LimitUpSettings, run_limit_up_next_open

        days, bars = self._bars(next_day)
        res = run_limit_up_next_open({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                                     BacktestSettings(slippage=0.0), LimitUpSettings())
        return days, res

    def test_buy_next_open_and_take_profit_same_day(self):
        days, res = self.run_one((13_500, 15_600, 13_400, 15_000))
        (t,) = res.trades
        self.assertEqual((t.entry_date, t.entry_price), (days[31], 13_500))
        self.assertEqual((t.reason, t.exit_price), ("TAKE_PROFIT", round_up_to_tick(13_500 * 1.15)))

    def test_both_hit_same_day_counts_as_stop(self):
        _, res = self.run_one((13_500, 15_600, 12_800, 13_000))
        self.assertEqual(res.trades[0].reason, "STOP_LOSS")

    def test_skip_when_opens_at_limit_up(self):
        _, res = self.run_one((16_900, 16_900, 16_900, 16_900))
        self.assertEqual(res.trades, [])


class SurgeDojiEngineTest(unittest.TestCase):
    def test_buy_on_low_volume_small_bearish_candle_after_surge(self):
        from tossbot.backtest import SurgeDojiSettings, run_surge_doji

        days = weekdays(45)
        bars = [Bar(d, 10_000, 10_050, 9_950, 10_000, 1_000_000) for d in days[:30]]
        bars.append(Bar(days[30], 10_000, 11_800, 10_000, 11_700, 5_000_000))  # +17% 급등 (거래대금 1위)
        bars.append(Bar(days[31], 12_000, 12_100, 11_300, 11_400, 3_000_000))  # 음봉이지만 몸통 5% → 제외
        bars.append(Bar(days[32], 11_500, 11_550, 11_250, 11_300, 2_000_000))  # 단봉 음봉 + 거래량 40% → 매수
        bars.append(Bar(days[33], 11_400, 13_100, 11_350, 13_000, 2_000_000))  # 익절
        other = [Bar(d, 1_000, 1_010, 990, 1_000, 10_000) for d in days[:34]]  # 거래대금 작은 종목
        res = run_surge_doji({"000010": ("테스트", bars), "000020": ("작은종목", other)},
                             date(2023, 11, 1), date(2024, 3, 31), BacktestSettings(slippage=0.0),
                             SurgeDojiSettings())
        (t,) = res.trades
        self.assertEqual((t.entry_date, t.entry_price, t.surge_date), (days[32], 11_300, days[30]))
        self.assertEqual((t.reason, t.exit_price), ("TAKE_PROFIT", round_up_to_tick(11_300 * 1.15)))


class FirstHighTest(unittest.TestCase):
    def test_only_first_20d_high_within_month(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(70)
        closes = [10_000] * 40 + [10_300]  # 40번째 날: 첫 신고가 → 매수
        closes += [10_100] * 5 + [10_400]  # 46번째 날: 또 신고가지만 직전 20일 안에 신고가 있었음
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 5_000_000) for d, c in zip(days, closes)]
        b = BreakoutSettings(stop_loss_pct=7.0, take_profit_pct=20.0, exit_on_low=False,
                             first_in_days=20, min_day_amount=10_000_000_000)
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0, num_slots=10), b)
        self.assertEqual([t.entry_date for t in res.trades], [days[40]])
        # 거래대금 100억 미만이면 매수 없음
        small = [Bar(x.day, x.open, x.high, x.low, x.close, 100_000) for x in bars]
        res2 = run_breakout({"000010": ("테스트", small)}, date(2023, 11, 1), date(2024, 3, 31),
                            BacktestSettings(slippage=0.0), b)
        self.assertEqual(res2.trades, [])

    def test_entry_delay_buys_three_days_after_signal(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(70)
        closes = [10_000] * 40 + [10_300, 10_200, 10_250, 10_280, 10_290]  # 40번째 날 첫 신고가
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 5_000_000) for d, c in zip(days, closes)]
        b = BreakoutSettings(stop_loss_pct=7.0, take_profit_pct=20.0, exit_on_low=False,
                             first_in_days=20, min_day_amount=10_000_000_000, entry_delay=3)
        res = run_breakout({"000010": ("테스트", bars)}, date(2023, 11, 1), date(2024, 3, 31),
                           BacktestSettings(slippage=0.0), b)
        (t,) = res.trades
        self.assertEqual((t.surge_date, t.entry_date, t.entry_price), (days[40], days[43], 10_280))

    def test_entry_delay_conditions(self):
        from tossbot.backtest import BreakoutSettings, run_breakout

        days = weekdays(70)

        def run(after, intraday=False):
            base = [Bar(d, 10_000, 10_050, 9_950, 10_000, 5_000_000) for d in days[:40]]
            sig = Bar(days[40], 10_100, 10_350, 10_050, 10_300, 5_000_000)  # 신고가 날: 시가 10,100 / 종가 10,300
            rest = [Bar(days[41 + k], *ohlc, 5_000_000) for k, ohlc in enumerate(after)]
            b = BreakoutSettings(stop_loss_pct=7.0, take_profit_pct=20.0, exit_on_low=False, first_in_days=20,
                                 min_day_amount=10_000_000_000, entry_delay=3, delay_max_rise_pct=5.0,
                                 delay_hold_signal_open=True, delay_intraday=intraday)
            return run_breakout({"000010": ("테스트", base + [sig] + rest)}, date(2023, 11, 1), date(2024, 3, 31),
                                BacktestSettings(slippage=0.0), b).trades

        calm = [(10_300, 10_400, 10_200, 10_250)] * 3
        self.assertEqual(len(run(calm)), 1)  # 조건 통과 → 3일째 매수
        risen = calm[:1] + [(10_300, 10_900, 10_250, 10_850)] + calm[:1]  # 종가 +5.3%
        self.assertEqual(run(risen), [])
        broke = calm[:1] + [(10_200, 10_250, 10_000, 10_050)] + calm[:1]  # 종가 10,050 < 시가 10,100
        self.assertEqual(run(broke), [])
        wick = calm[:1] + [(10_200, 10_300, 10_000, 10_200)] + calm[:1]  # 저가만 시가 아래
        self.assertEqual(len(run(wick)), 1)  # 종가 기준이면 통과
        self.assertEqual(run(wick, intraday=True), [])  # 장중 기준이면 탈락


class IndexFilterTest(unittest.TestCase):
    def test_index_uptrend_days(self):
        from tossbot.backtest import index_uptrend_days

        days = weekdays(10)
        closes = [100, 101, 102, 103, 104, 105, 99, 100, 106, 107]
        ix = [Bar(d, c, c, c, c, 0) for d, c in zip(days, closes)]
        # 5거래일 전 종가보다 높은 날만: 5번째(105>100), 8번째(106>104), 9번째(107>105)
        self.assertEqual(index_uptrend_days(ix, 5), {days[5], days[8], days[9]})

    def test_index_streak_up_days(self):
        from tossbot.backtest import index_streak_up_days

        days = weekdays(8)
        closes = [100, 101, 102, 103, 102, 103, 104, 105]
        ix = [Bar(d, c, c, c, c, 0) for d, c in zip(days, closes)]
        # 3일 연속 상승: 3번째 날(101→102→103), 7번째 날(102→103→104→105)
        self.assertEqual(index_streak_up_days(ix, 3), {days[3], days[7]})


class KellyFractionTest(unittest.TestCase):
    def test_kelly(self):
        from tossbot.backtest import kelly_fraction
        # 승률 50%, 손익비 2 → 0.5 - 0.5 / 2 = 0.25
        self.assertAlmostEqual(kelly_fraction([0.10, -0.05, 0.10, -0.05]), 0.25)
        self.assertEqual(kelly_fraction([-0.05, -0.05]), 0.0)
        self.assertEqual(kelly_fraction([0.1, 0.2]), 1.0)
        self.assertLess(kelly_fraction([0.10, -0.05, -0.05, -0.05]), 0)


class MaPullbackTest(unittest.TestCase):
    def test_buys_at_ma_touch(self):
        from tossbot.backtest import MaPullbackSettings, run_ma_pullback
        days, d = [], date(2026, 1, 5)
        while len(days) < 40:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        closes = [10_000.0] * 20 + [11_000.0] + [10_800.0] * 3 + [10_200.0] + [10_300.0] * 15
        bars = [Bar(dy, c, c * 1.01, c * 0.99, c, 1_000_000) for dy, c in zip(days, closes)]
        bars[24] = Bar(days[24], 10_600, 10_600, 10_050, 10_200, 1_000_000)  # 저가가 10일선 근처로
        m = MaPullbackSettings(surge_pct=10, min_amount=1e9, first_in_days=10, ma_period=10, watch_days=10)
        s = BacktestSettings(initial_cash=10_000_000)
        s.params.slot_budget = 1_000_000
        res = run_ma_pullback({"A": ("A", bars)}, days[0], days[-1], s, m)
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual(t.entry_date, days[24])
        self.assertLess(t.entry_price, 10_600)

    def test_first_bear_candle_entry(self):
        from tossbot.backtest import MaPullbackSettings, run_ma_pullback
        days, d = [], date(2026, 1, 5)
        while len(days) < 30:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        bars = [Bar(dy, 10_000, 10_100, 9_900, 10_000, 1_000_000) for dy in days[:20]]
        bars.append(Bar(days[20], 10_000, 11_100, 10_000, 11_000, 1_000_000))  # 기준봉 +10%
        bars.append(Bar(days[21], 11_000, 11_500, 10_900, 11_400, 1_000_000))  # 양봉
        bars.append(Bar(days[22], 11_400, 11_500, 11_000, 11_100, 1_000_000))  # 첫 음봉 -2.6%
        bars += [Bar(dy, 11_100, 11_200, 11_000, 11_100, 1_000_000) for dy in days[23:]]
        m = MaPullbackSettings(surge_pct=10, min_amount=1e9, first_in_days=10, entry="bear",
                               require_above_ma=False, stop_loss_pct=5, take_profit_pct=15)
        s = BacktestSettings(initial_cash=10_000_000)
        s.params.slot_budget = 1_000_000
        res = run_ma_pullback({"A": ("A", bars)}, days[0], days[-1], s, m)
        self.assertEqual([t.entry_date for t in res.trades], [days[22]])
        m.bear_max_pct = 2.0  # 첫 음봉이 2% 넘게 빠짐 → 매수 안 함
        self.assertEqual(run_ma_pullback({"A": ("A", bars)}, days[0], days[-1], s, m).trades, [])

    def test_pick_and_ma_exit(self):
        from tossbot.backtest import MaPullbackSettings, run_ma_pullback
        days, d = [], date(2026, 1, 5)
        while len(days) < 30:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)

        def make(surge_close, vol):
            bars = [Bar(dy, 10_000, 10_100, 9_900, 10_000, vol) for dy in days[:20]]
            bars.append(Bar(days[20], 10_000, surge_close, 10_000, surge_close, vol))  # 기준봉
            bars.append(Bar(days[21], surge_close, surge_close, surge_close * 0.97, surge_close * 0.98, vol))  # 첫 음봉
            bars.append(Bar(days[22], surge_close, surge_close, surge_close * 0.97, surge_close * 0.99, vol))
            bars.append(Bar(days[23], surge_close, surge_close, surge_close * 0.85, surge_close * 0.9, vol))  # 5일선 이탈
            bars += [Bar(dy, 10_000, 10_100, 9_900, 10_000, vol) for dy in days[24:]]
            return bars

        data = {"A": ("A", make(11_600, 1_000_000)), "B": ("B", make(12_000, 500_000))}
        m = MaPullbackSettings(surge_pct=15, min_amount=1e9, first_in_days=10, entry="bear", bear_max_pct=100,
                               require_above_ma=False, stop_loss_pct=0, take_profit_pct=0, pick="change", ma_exit_days=5)
        s = BacktestSettings(initial_cash=10_000_000)
        s.params.slot_budget = 1_000_000
        res = run_ma_pullback(data, days[0], days[-1], s, m)
        self.assertEqual([(t.symbol, t.entry_date, t.exit_date, t.reason) for t in res.trades],
                         [("B", days[21], days[23], "MA5_EXIT")])
        m.pick = "both"  # 거래대금 1위(A)와 상승률 1위(B)가 달라 매수 없음
        self.assertEqual(run_ma_pullback(data, days[0], days[-1], s, m).trades, [])

    def test_bear_below_ma_skipped(self):
        from tossbot.backtest import MaPullbackSettings, run_ma_pullback
        days, d = [], date(2026, 1, 5)
        while len(days) < 30:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        bars = [Bar(dy, 10_000, 10_100, 9_900, 10_000, 1_000_000) for dy in days[:20]]
        bars.append(Bar(days[20], 10_000, 11_600, 10_000, 11_600, 1_000_000))  # 기준봉
        bars.append(Bar(days[21], 11_600, 11_600, 10_000, 10_100, 1_000_000))  # 첫 음봉, 5일선(10340) 아래
        bars += [Bar(dy, 10_100, 10_200, 10_000, 10_100, 1_000_000) for dy in days[22:]]
        m = MaPullbackSettings(surge_pct=15, min_amount=1e9, first_in_days=10, entry="bear", bear_max_pct=100,
                               require_above_ma=False, stop_loss_pct=0, take_profit_pct=0, ma_exit_days=5)
        s = BacktestSettings(initial_cash=10_000_000)
        s.params.slot_budget = 1_000_000
        self.assertEqual(len(run_ma_pullback({"A": ("A", bars)}, days[0], days[-1], s, m).trades), 1)
        m.bear_min_ma = 5
        self.assertEqual(run_ma_pullback({"A": ("A", bars)}, days[0], days[-1], s, m).trades, [])


class NPatternTest(unittest.TestCase):
    def test_n_pattern_signals(self):
        from tossbot.backtest import BreakoutSettings, n_pattern_signal
        days = weekdays(60)
        # 보합 → 20일 동안 10,000 → 12,000 (+20%) → 눌림 11,000 (-8.3%) → 반등 → 돌파
        closes = [10_000] * 30 + [10_000 + 100 * k for k in range(1, 21)] + [11_500, 11_000, 11_300, 11_800, 12_200]
        bars = [Bar(d, c * 0.995, c * 1.01, c * 0.99, c, 3_000_000) for d, c in zip(days, closes)]
        a = BreakoutSettings(npattern="A")
        bb = BreakoutSettings(npattern="B")
        i_a, i_b = 52, 54  # 11,300 (저점 다음 양봉) / 12,200 (고점 12,000 돌파)
        self.assertTrue(n_pattern_signal(bars, i_a, a))
        self.assertFalse(n_pattern_signal(bars, i_a, bb))
        self.assertTrue(n_pattern_signal(bars, i_b, bb))
        self.assertFalse(n_pattern_signal(bars, 53, bb))  # 11,800 은 아직 고점 아래
        a.np_min_pull = 10  # 눌림 8.3% < 10%
        self.assertFalse(n_pattern_signal(bars, i_a, a))

    def test_n_pattern_c_repeat_strong_candle(self):
        from tossbot.backtest import BreakoutSettings, n_pattern_signal
        days = weekdays(40)
        # 기준봉(25일, +12%) → 눌림 (저가 10,900 > 기준봉 저가 10,000) → 31일 다시 +12%
        closes = [10_000] * 25 + [11_200, 11_100, 11_000, 11_050, 11_100, 11_150, 12_488] + [12_500] * 8
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 3_000_000) for d, c in zip(days, closes)]
        bars[25] = Bar(days[25], 10_050, 11_300, 10_000, 11_200, 3_000_000)
        c = BreakoutSettings(npattern="C", np_rise_pct=10, np_amount=2e10)
        self.assertTrue(n_pattern_signal(bars, 31, c))
        c.np_quiet = "any"  # 기준봉 뒤 거래량이 평균(3,000,000)과 같음 → 평균 이하
        self.assertTrue(n_pattern_signal(bars, 31, c))
        for j in range(26, 31):
            x = bars[j]
            bars[j] = Bar(x.day, x.open, x.high, x.low, x.close, 5_000_000)
        self.assertFalse(n_pattern_signal(bars, 31, c))  # 거래량이 줄지 않음
        bars[29] = Bar(days[29], 11_050, 11_150, 10_900, 11_050, 1_000_000)
        self.assertTrue(n_pattern_signal(bars, 31, c))
        c.np_quiet = "all"
        self.assertFalse(n_pattern_signal(bars, 31, c))
        c.np_quiet, c.np_base_ma = "", 20  # 20일선 ≈ 10,000, 기준봉 시가 10,050
        self.assertTrue(n_pattern_signal(bars, 31, c))
        c.np_base_ma_pct = 0.1
        self.assertFalse(n_pattern_signal(bars, 31, c))
        c.np_base_ma_mode = "cross"  # 시가 10,050 > 20일선 10,000 x 1.001
        self.assertFalse(n_pattern_signal(bars, 31, c))
        c.np_base_ma_pct = 1
        self.assertTrue(n_pattern_signal(bars, 31, c))
        c.np_base_ma = 0
        c.np_quiet = "last"  # 매수 전날(30일)은 거래량이 많음
        self.assertFalse(n_pattern_signal(bars, 31, c))
        bars[30] = Bar(days[30], 11_150, 11_250, 11_000, 11_150, 1_000_000)
        self.assertTrue(n_pattern_signal(bars, 31, c))
        c.np_quiet = ""
        self.assertFalse(n_pattern_signal(bars, 25, c))  # 기준봉 자체
        bars[28] = Bar(days[28], 11_000, 11_100, 9_900, 11_050, 3_000_000)  # 기준봉 저가 이탈
        self.assertFalse(n_pattern_signal(bars, 31, c))
        c.np_base_ref = "open"  # 기준봉 시가 10,050 도 이탈
        self.assertFalse(n_pattern_signal(bars, 31, c))


class VolBreakoutTest(unittest.TestCase):
    def test_buys_at_target_sells_next_open(self):
        from tossbot.backtest import VolBreakoutSettings, run_vol_breakout
        days = weekdays(30)
        bars = [Bar(d, 10_000, 10_200, 9_800, 10_000, 3_000_000) for d in days[:25]]  # 변동폭 400
        bars.append(Bar(days[25], 10_000, 10_500, 9_950, 10_400, 3_000_000))  # 목표가 10,200 돌파
        bars += [Bar(d, 10_600, 10_700, 10_500, 10_600, 3_000_000) for d in days[26:]]
        s = BacktestSettings(slippage=0.0)
        res = run_vol_breakout({"A": ("A", bars)}, days[25], days[26], s, VolBreakoutSettings(k=0.5, min_amount=1e10))
        (t,) = res.trades
        self.assertEqual((t.entry_date, t.entry_price, t.exit_date, t.exit_price), (days[25], 10_200, days[26], 10_600))


class RsiTest(unittest.TestCase):
    def test_rsi_values(self):
        from tossbot.backtest import rsi_series
        self.assertEqual(rsi_series([1, 2, 3, 4, 5], 3)[3:], [100.0, 100.0])
        r = rsi_series([10, 9, 8, 7, 8], 3)
        self.assertEqual(r[3], 0.0)
        self.assertGreater(r[4], 0.0)

    def test_buy_oversold_sell_on_rebound(self):
        from tossbot.backtest import RsiSettings, run_rsi
        days = weekdays(40)
        closes = [10_000] * 25 + [9_700, 9_400, 9_100, 8_800] + [9_300, 9_800] + [9_800] * 9
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 1_000_000) for d, c in zip(days, closes)]
        res = run_rsi({"A": ("A", bars)}, days[0], days[-1], BacktestSettings(slippage=0.0),
                      RsiSettings(period=2, buy_below=10, sell_above=70))
        t = res.trades[0]
        self.assertEqual(t.entry_date, days[25])  # 첫 하락일 RSI(2) = 0
        self.assertEqual((t.exit_date, t.reason), (days[30], "RSI_EXIT"))  # RSI 64 → 84

    def test_shared_matches_breakout_alone(self):
        from tossbot.backtest import BreakoutSettings, RsiSettings, run_breakout, run_shared
        days = weekdays(60)
        closes = [10_000] * 30 + [10_500, 10_400, 12_700] + [12_700] * 27
        bars = [Bar(d, c, c * 1.01, c * 0.99, c, 3_000_000) for d, c in zip(days, closes)]
        data = {"000010": ("테스트", bars)}
        b = BreakoutSettings(stop_loss_pct=4.7, take_profit_pct=20, exit_on_low=False)
        alone = run_breakout(data, days[0], days[-1], BacktestSettings(), b)
        shared = run_shared(data, days[0], days[-1], BacktestSettings(), b, RsiSettings(buy_below=0))
        self.assertEqual([(t.entry_date, t.exit_date) for t in alone.trades],
                         [(t.entry_date, t.exit_date) for t in shared.trades])
        self.assertAlmostEqual(alone.final_equity, shared.final_equity)


class MaCrossTest(unittest.TestCase):
    def test_cross_on_falling_ma_then_touch_on_rising_ma(self):
        from tossbot.backtest import MaCrossSettings, run_ma_cross
        days, d = [], date(2026, 1, 5)
        while len(days) < 50:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        closes = ([12_000.0] * 10 + [12_000 - 150 * k for k in range(1, 15)] + [10_600.0]  # 하락 이평선 돌파
                  + [10_600 + 100 * k for k in range(1, 11)] + [11_000.0] * 2 + [12_500.0] * 13)
        bars = [Bar(dy, c, c * 1.01, c * 0.99, c, 1_000_000) for dy, c in zip(days, closes)]
        m = MaCrossSettings(ma_period=10, slope_days=2, min_amount=1e9, high_lookback=20, watch_days=20)
        s = BacktestSettings(initial_cash=10_000_000)
        s.params.slot_budget = 1_000_000
        res = run_ma_cross({"A": ("A", bars)}, days[0], days[-1], s, m)
        self.assertEqual(len(res.trades), 1)
        t = res.trades[0]
        self.assertEqual((t.surge_date, t.entry_date), (days[24], days[35]))
        self.assertEqual((t.reason, t.exit_price), ("TAKE_PROFIT", 12_500))  # 전고점 12,120 위에서 시가 출발
        m.min_upside_pct = 15  # 전고점까지 여유 부족 → 매수 안 함
        self.assertEqual(run_ma_cross({"A": ("A", bars)}, days[0], days[-1], s, m).trades, [])
        m.min_upside_pct, m.target = 7, "since"  # 돌파 뒤 최고가 11,716 → 매수가 대비 7% 미만 → 매수 안 함
        self.assertEqual(run_ma_cross({"A": ("A", bars)}, days[0], days[-1], s, m).trades, [])
        m.min_upside_pct = 5
        t = run_ma_cross({"A": ("A", bars)}, days[0], days[-1], s, m).trades[0]
        self.assertEqual((t.entry_date, t.exit_price), (days[35], 12_500))


class RsiSecondTest(unittest.TestCase):
    def test_second_oversold_flags(self):
        from tossbot.backtest import RsiSettings, rsi_second_flags
        rsi = [None, 40, 25, 20, 35, 45, 28, 26, 50, 29]
        closes = [100, 100, 90, 85, 90, 95, 84, 83, 99, 98]
        r = RsiSettings(buy_below=30, second_within=10)
        self.assertEqual([i for i, f in enumerate(rsi_second_flags(closes, rsi, r)) if f], [6, 9])
        r.second_reset = 40  # 4일째 35 → 40 미만이라도 5일째 45 로 회복했으니 6일째는 두 번째
        self.assertEqual([i for i, f in enumerate(rsi_second_flags(closes, rsi, r)) if f], [6, 9])
        r.second_reset, r.second_within = 0, 2  # 첫 과매도(2~3일)가 직전 2거래일보다 앞 → 6일째 아님
        self.assertEqual([i for i, f in enumerate(rsi_second_flags(closes, rsi, r)) if f], [9])
        r.second_within, r.second_diverge = 10, True  # 6일째: 종가 84 < 85, RSI 28 > 20 → 다이버전스
        self.assertEqual([i for i, f in enumerate(rsi_second_flags(closes, rsi, r)) if f], [6])


class MinutePullbackTest(unittest.TestCase):
    def _bars(self, prices, start_min=9 * 60):
        from tossbot.backtest import MinuteBar
        out = []
        for k, (o, h, l, c) in enumerate(prices):
            t = start_min + k
            out.append(MinuteBar(f"{t // 60:02d}:{t % 60:02d}", o, h, l, c, 1000))
        return out

    def _run(self, day_prices, m=None):
        from tossbot.backtest import MinuteSettings, simulate_minute
        m = m or MinuteSettings(ma=5, mode="next")
        warm = self._bars([(100, 100, 100, 100)] * 5, start_min=14 * 60)
        sig = {"code": "000000", "name": "x", "trade_day": date(2026, 8, 3), "prev_high": 90}
        return simulate_minute(sig, warm, self._bars(day_prices), m)

    def test_pullback_to_ma_then_take_profit(self):
        # 101 로 올라 이평선(≈100) 위에 있다가 내려와 닿으면 매수, 이후 +3% 도달
        t = self._run([(101, 101, 101, 101), (101, 101, 101, 101), (101, 101, 100, 100.5), (101, 104, 101, 104)])
        self.assertEqual(t["reason"], "TAKE_PROFIT")
        self.assertEqual(t["entry_time"], "09:02")
        self.assertAlmostEqual(t["exit_price"], t["entry_price"] * 1.03, delta=0.2)

    def test_stop_on_entry_bar_is_conservative(self):
        t = self._run([(101, 101, 101, 101), (101, 101, 98, 99)])
        self.assertEqual(t["reason"], "STOP_LOSS")
        self.assertLess(t["ret_pct"], -1)

    def test_no_touch_no_trade_and_time_exit(self):
        self.assertIsNone(self._run([(102, 102, 102, 102)] * 3))
        prices = [(101, 101, 101, 101), (101, 101, 100, 100.5)] + [(100.6, 100.7, 100.5, 100.6)] * 400
        t = self._run(prices)
        self.assertEqual(t["reason"], "TIME_EXIT")
        self.assertEqual(t["exit_time"], "15:10")

    def test_same_mode_needs_breakout_and_amount_first(self):
        from tossbot.backtest import MinuteSettings
        prices = [(101, 101, 101, 101), (101, 101, 100, 100.5), (101, 104, 101, 104)]
        m = MinuteSettings(ma=5, mode="same", min_day_amount=0)
        from tossbot.backtest import simulate_minute
        warm = self._bars([(100, 100, 100, 100)] * 5, start_min=14 * 60)
        sig = {"code": "000000", "name": "x", "trade_day": date(2026, 8, 3), "prev_high": 102}
        self.assertIsNone(simulate_minute(sig, warm, self._bars(prices), m))  # 눌림 전에 102 돌파 없음
        sig["prev_high"] = 100.8
        self.assertIsNotNone(simulate_minute(sig, warm, self._bars(prices), m))


class SectorGapTest(unittest.TestCase):
    def _day(self, prices):
        from tossbot.backtest import MinuteBar
        return [MinuteBar(f"{(9 * 60 + 1 + k) // 60:02d}:{(9 * 60 + 1 + k) % 60:02d}", o, h, l, c, 100)
                for k, (o, h, l, c) in enumerate(prices)]

    def _sig(self):
        return {"code": "000000", "name": "x", "sector": "s", "trade_day": date(2026, 8, 3), "prev_close": 100}

    def test_first_fill_at_open_then_take_profit(self):
        from tossbot.backtest import SectorGapSettings, simulate_sector_gap
        day = self._day([(103, 104, 103, 104), (104, 104, 102.5, 103), (103, 108.5, 103, 108)])
        t = simulate_sector_gap(self._sig(), day, SectorGapSettings(take_profit=5))
        self.assertEqual(t["reason"], "TAKE_PROFIT")
        self.assertAlmostEqual(t["filled_weight"], 0.5)
        self.assertAlmostEqual(t["exit_price"], 103 * 1.05, delta=0.1)
        self.assertGreater(t["ret_pct"], 4.5)
        self.assertAlmostEqual(t["slot_ret_pct"], t["ret_pct"] / 2, delta=0.01)

    def test_second_fill_lowers_average_and_time_exit(self):
        from tossbot.backtest import SectorGapSettings, simulate_sector_gap
        day = self._day([(103, 103, 103, 103), (103, 103, 99, 100)] + [(101, 101, 101, 101)] * 400)
        t = simulate_sector_gap(self._sig(), day, SectorGapSettings(take_profit=5))
        self.assertEqual(t["reason"], "TIME_EXIT")
        self.assertEqual(t["exit_time"], "15:15")
        self.assertAlmostEqual(t["filled_weight"], 1.0)

    def test_no_gap_or_no_pullback_no_trade(self):
        from tossbot.backtest import SectorGapSettings, simulate_sector_gap
        self.assertIsNone(simulate_sector_gap(self._sig(), self._day([(99, 101, 98, 100)] * 3), SectorGapSettings()))
        self.assertIsNone(simulate_sector_gap(self._sig(), self._day([(103, 106, 103, 105), (105, 107, 104, 106)]),
                                              SectorGapSettings()))

    def test_gap_cap_and_prev_close_stop(self):
        from tossbot.backtest import SectorGapSettings, simulate_sector_gap
        g = SectorGapSettings(first_weight=1.0, max_gap=5, stop_prev_close=True, buy_until="10:00")
        self.assertIsNone(simulate_sector_gap(self._sig(), self._day([(106, 107, 105, 106)] * 3), g))  # 갭 6%
        day = self._day([(103, 104, 103, 104), (104, 104, 102.5, 103), (103, 103, 99.5, 100)])
        t = simulate_sector_gap(self._sig(), day, g)
        self.assertEqual(t["reason"], "STOP_PREV_CLOSE")
        self.assertAlmostEqual(t["filled_weight"], 1.0)
        self.assertAlmostEqual(t["exit_price"], 100 * 0.999, delta=0.01)


class PullBreakTest(unittest.TestCase):
    def _bars(self, prices, start="09:01"):
        from tossbot.backtest import MinuteBar
        m0 = int(start[:2]) * 60 + int(start[3:])
        return [MinuteBar(f"{(m0 + k) // 60:02d}:{(m0 + k) % 60:02d}", o, h, l, c, 1000)
                for k, (o, h, l, c) in enumerate(prices)]

    def test_to_5min_bars(self):
        from tossbot.backtest import to_bars_n
        b = to_bars_n(self._bars([(1, 2, 0.5, 1.5)] * 10), 5)
        self.assertEqual([x.t for x in b], ["09:05", "09:10"])
        self.assertEqual(b[0].volume, 5000)

    def test_pullback_then_break_prior_high(self):
        from tossbot.backtest import PullBreakSettings, pullback_break_events, _exit_trade
        s = PullBreakSettings(ma=3, bar_minutes=1, min_cum_amount=0, take_profit=5)
        warm = self._bars([(100, 100, 100, 100)] * 5, start="15:16")
        day = self._bars([(104, 105, 104, 105), (105, 107, 105, 107), (107, 107, 104, 104.5),
                          (104.5, 106, 104.5, 106), (106, 108, 106, 108), (108, 113, 108, 112)])
        ev = pullback_break_events(warm, day, 100, s)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["time"], "09:05")
        self.assertEqual(ev[0]["pivot"], 107)
        self.assertEqual(ev[0]["stop"], 104)
        i, t, px, reason = _exit_trade(day, ev[0], s)
        self.assertEqual(reason, "TAKE_PROFIT")
        # 상승률 조건 미달이면 신호 없음
        self.assertEqual(pullback_break_events(warm, day, 105, s), [])

    def test_kelly_equity_sizing(self):
        from tossbot.backtest import kelly_equity
        trades = [{"trade_day": "2026-07-01", "entry_price": 1000, "ret_pct": r} for r in [5, -2] * 15]
        full = kelly_equity(trades, 1.0, warmup=10)
        half = kelly_equity(trades, 0.5, warmup=10)
        fixed = kelly_equity(trades, 1.0, fixed_pct=100)
        # 승률 50%, 손익비 2.5 → 켈리 0.3. 하프는 비중이 더 작다
        self.assertGreater(full["avg_pct"], half["avg_pct"])
        self.assertAlmostEqual(fixed["avg_pct"], 100)
        self.assertGreater(full["ret_pct"], 0)

    def test_band_pullback_between_mas(self):
        from tossbot.backtest import PullBreakSettings, pullback_break_events
        s = PullBreakSettings(band=(2, 4), bar_minutes=1, min_cum_amount=0)
        warm = self._bars([(100, 100, 100, 100)] * 5, start="15:16")
        # 상승 → 2이평(빠른) 아래·4이평(느린) 위로 눌림 → 전고점 돌파
        day = self._bars([(104, 106, 104, 106), (106, 108, 106, 108), (108, 108, 105.5, 106),
                          (106, 107, 106, 107), (107, 109, 107, 109)])
        ev = pullback_break_events(warm, day, 100, s)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["pivot"], 108)
        # 느린 이평선 밑으로 빠지면 눌림 아님 (허용 옵션 없을 때)
        deep = self._bars([(104, 106, 104, 106), (106, 108, 106, 108), (108, 108, 100, 101),
                           (101, 107, 101, 107), (107, 109, 107, 109)])
        self.assertEqual(pullback_break_events(warm, deep, 95, s), [])

    def test_aligned_mas_required(self):
        from tossbot.backtest import PullBreakSettings, pullback_break_events
        warm = self._bars([(100, 100, 100, 100)] * 5, start="15:16")
        day = self._bars([(104, 105, 104, 105), (105, 107, 105, 107), (107, 107, 104, 104.5),
                          (104.5, 106, 104.5, 106), (106, 108, 106, 108)])
        base = dict(ma=3, bar_minutes=1, min_cum_amount=0)
        self.assertEqual(len(pullback_break_events(warm, day, 100, PullBreakSettings(**base))), 1)
        # 눌림 봉 종가들: 2이평 105.75 < 3이평 105.5? → (107+104.5)/2=105.75 > (105+107+104.5)/3=105.5 > 4이평 104.1 정배열
        self.assertEqual(len(pullback_break_events(warm, day, 100, PullBreakSettings(aligned=(2, 3, 4), **base))), 1)
        # 역배열 요구 순서면 신호 없음
        self.assertEqual(pullback_break_events(warm, day, 100, PullBreakSettings(aligned=(4, 3, 2), **base)), [])

    def test_realtime_amount_rank_uses_amount_before_minute(self):
        from tossbot.backtest import realtime_amount_rank
        with tempfile.TemporaryDirectory() as tmp:
            d = date(2026, 8, 3)
            def write(code, vols):
                with open(Path(tmp) / f"{code}_{d.isoformat()}.csv", "w", encoding="utf-8") as f:
                    f.write("time,open,high,low,close,volume\n")
                    for k, v in enumerate(vols):
                        t = 541 + k
                        f.write(f"{t // 60:02d}:{t % 60:02d},100,100,100,100,{v}\n")
            write("A", [10, 10, 10])
            write("B", [1, 100, 1])
            ranks = realtime_amount_rank(Path(tmp), d, ["A", "B", "C"])
            self.assertEqual(ranks["A"][2], 1)  # 09:02 직전: A 10 > B 1
            self.assertEqual(ranks["B"][3], 1)  # 09:03 직전: B 101 > A 20
            self.assertNotIn("C", ranks)

    def test_vwma_touch_entry(self):
        from tossbot.backtest import PullBreakSettings, vwma_touch_events
        s = PullBreakSettings(vwma=3, bar_minutes=1, min_cum_amount=0)
        warm = self._bars([(100, 100, 100, 100)] * 3, start="15:17")
        # VWMA(3) ≈ 100 → 106 위로 갔다가 다시 VWMA 근처까지 내려오면 매수
        day = self._bars([(106, 107, 106, 107), (107, 108, 107, 108), (108, 108, 104, 105)])
        ev = vwma_touch_events(warm, day, 95, s)
        self.assertEqual(len(ev), 1)
        self.assertEqual(ev[0]["time"], "09:03")
        self.assertAlmostEqual(ev[0]["price"], (100 + 107 + 108) / 3, places=3)

    def test_surge_break_prev_close_and_gap_excluded(self):
        from tossbot.backtest import PullBreakSettings, surge_break_event
        s = PullBreakSettings()
        c = {"prev_close": 100, "prev_high": 110}
        day = self._bars([(98, 99, 97, 98), (98, 101, 98, 100.5), (100.5, 112, 100, 111)])
        ev = surge_break_event(day, c, "prevclose", s, stop_daylow=True)
        self.assertEqual(ev["time"], "09:02")
        self.assertAlmostEqual(ev["price"], 100 * (1 + s.slippage))
        self.assertEqual(ev["stop"], 97)
        self.assertEqual(surge_break_event(day, c, "prevhigh", s)["time"], "09:03")
        gap = self._bars([(102, 103, 101, 102)] * 3)
        self.assertIsNone(surge_break_event(gap, c, "prevclose", s))  # 갭상승으로 이미 위

    def test_surge_break_retest(self):
        from tossbot.backtest import PullBreakSettings, surge_break_event
        s = PullBreakSettings()
        c = {"prev_close": 100, "prev_high": 110}
        # 98 → 102 돌파(종가 101 위) → 다음 봉에서 100 까지 되돌림 → 100 에 매수
        day = self._bars([(98, 99, 97, 98), (98, 102, 98, 101), (101, 101.5, 99.5, 100.5), (100.5, 104, 100, 103)])
        ev = surge_break_event(day, c, "prevclose", s, retest=True)
        self.assertEqual(ev["time"], "09:03")
        self.assertEqual(ev["price"], 100)
        # 돌파만 하고 되돌림이 없으면 매수 없음
        up = self._bars([(98, 99, 97, 98), (98, 102, 98, 101), (101, 104, 100.5, 103)])
        self.assertIsNone(surge_break_event(up, c, "prevclose", s, retest=True))
