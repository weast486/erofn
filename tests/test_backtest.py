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
