import unittest

from tossbot.backtest import RsiSettings, rsi_ma_entry_flags


class RsiMaEntryTest(unittest.TestCase):
    def test_buy_first_close_above_ma_after_oversold(self):
        closes = [10, 10, 10, 10, 10, 9, 8, 7, 7.5, 9, 9]
        rsi = [None, 40, 40, 40, 40, 30, 20, 15, 22, 35, 40]
        r = RsiSettings(buy_below=25, sell_above=50, ma_entry=5)
        out = rsi_ma_entry_flags(closes, rsi, r)
        # 8번째 날 7.5 는 5일선 아래, 9번째 날 9 > 5일선(8.1) → 매수, 그 뒤는 주목 해제
        self.assertEqual([i for i, v in enumerate(out) if v], [9])

    def test_skip_when_rsi_already_above_sell(self):
        closes = [10, 10, 10, 10, 10, 8, 12]
        rsi = [None, 40, 40, 40, 40, 20, 55]
        out = rsi_ma_entry_flags(closes, rsi, RsiSettings(buy_below=25, sell_above=50, ma_entry=5))
        self.assertFalse(any(out))

    def test_watch_limit(self):
        closes = [10] * 5 + [8, 7, 7, 7, 7, 9]
        rsi = [None, 40, 40, 40, 40, 20, 30, 30, 30, 30, 40]
        r = RsiSettings(buy_below=25, sell_above=50, ma_entry=5, ma_watch=3)
        self.assertFalse(any(rsi_ma_entry_flags(closes, rsi, r)))
        r.ma_watch = 0
        self.assertTrue(rsi_ma_entry_flags(closes, rsi, r)[10])


if __name__ == "__main__":
    unittest.main()
