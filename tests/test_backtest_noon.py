import unittest

from tossbot.backtest import MinuteBar, noon_entry, noon_exit, noon_pick


def bar(t, o, h, l, c, v=100):
    return MinuteBar(t, o, h, l, c, v)


class NoonTest(unittest.TestCase):
    def day(self):
        return [bar("09:01", 100, 106, 100, 105), bar("11:59", 105, 107, 104, 106),
                bar("12:00", 106, 106, 105, 105), bar("12:01", 105, 108, 105, 108), bar("12:02", 108, 109, 99, 100),
                bar("15:15", 100, 100, 100, 100)]

    def test_pick_uses_bars_before_noon_only(self):
        info = noon_pick(self.day(), 100)
        self.assertAlmostEqual(info["chg"], 6.0)
        self.assertEqual(info["high"], 107)
        self.assertEqual(info["amount"], 105 * 100 + 106 * 100)

    def test_dayhigh_entry_and_stop(self):
        d = self.day()
        info = noon_pick(d, 100)
        j, price = noon_entry(d, info, "dayhigh", "12:00", "14:30", 0.0)
        self.assertEqual((d[j].t, price), ("12:01", 107))
        t, px, reason = noon_exit(d, j, price, 3, 5, "15:15", 0.0)
        self.assertEqual((t, reason), ("12:02", "STOP_LOSS"))
        self.assertAlmostEqual(px, 107 * 0.97)

    def test_noon_entry_buys_first_bar_open(self):
        d = self.day()
        j, price = noon_entry(d, noon_pick(d, 100), "noon", "12:00", "14:30", 0.0)
        self.assertEqual((d[j].t, price), ("12:00", 106))


if __name__ == "__main__":
    unittest.main()
