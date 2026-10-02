import unittest
from datetime import date

from tossbot.backtest import Bar, etf_overnight


class EtfOvernightTest(unittest.TestCase):
    def bars(self):
        return [Bar(date(2026, 1, 1), 100, 101, 99, 100, 1), Bar(date(2026, 1, 2), 100, 100, 95, 98, 1),
                Bar(date(2026, 1, 5), 99, 103, 99, 102, 1), Bar(date(2026, 1, 6), 103, 104, 100, 101, 1)]

    def test_buys_only_on_down_days_and_sells_next_open(self):
        tr = etf_overnight(self.bars(), commission=0.0)
        self.assertEqual([t["buy_day"] for t in tr], ["2026-01-02"])  # 98 < 100 만 하락
        self.assertAlmostEqual(tr[0]["ret_pct"], (99 / 98 - 1) * 100, places=2)

    def test_every_day_and_open_reference(self):
        self.assertEqual(len(etf_overnight(self.bars(), max_prev_change=1000)), 2)
        tr = etf_overnight(self.bars(), ref="open")  # 시가 대비: 1/2 (100→98) 만 하락
        self.assertEqual([t["buy_day"] for t in tr], ["2026-01-02"])


if __name__ == "__main__":
    unittest.main()
