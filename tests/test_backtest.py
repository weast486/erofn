import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from tossbot.backtest import BacktestSettings, SELL_TAX_BY_YEAR, load_cache, run_backtest, _write_bars
from tossbot.broker import round_down_to_tick, round_up_to_tick
from tossbot.selector import Bar, PullbackParams, analyze, touch_zone

START = date(2024, 1, 1)


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
    item, reason = analyze("X", "X", bars, days[len(bars)], PullbackParams())
    assert item, reason
    lo, hi = touch_zone(item, PullbackParams())
    bars.append(Bar(days[len(bars)], hi * 1.01, hi * 1.01, hi * 0.995, hi, 600_000))
    for o, h, l, c in after_entry(hi):
        bars.append(Bar(days[len(bars)], o, h, l, c, 600_000))
    return bars, hi


class BacktestEngineTest(unittest.TestCase):
    def run_one(self, after_entry, **kw):
        bars, hi = make_stock(after_entry)
        s = BacktestSettings(slippage=0.0, **kw)
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
        s = PullbackParams(min_days_after_surge=5)  # 급등 후 4일째 진입이므로 막혀야 함
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
