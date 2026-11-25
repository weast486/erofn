import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from binancebot.okxsplit import OkxSplit, OkxSplitConfig, PaperOkx, Store
from tossbot.config import KST


class FakePrice:
    def __init__(self, p):
        self.p = p

    def last(self, symbol):
        return self.p


def make(tmp, price=10.0, **kw):
    cfg = OkxSplitConfig(dry_run=True, **kw)
    feed = FakePrice(price)
    ex = PaperOkx(feed)
    bot = OkxSplit(cfg, ex, Store(Path(tmp) / "s.json"))
    return bot, feed, ex


NOW = datetime(2026, 11, 26, 0, 0, tzinfo=KST)


class OkxSplitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.dir.cleanup()

    def test_waits_for_start(self):
        bot, feed, ex = make(self.dir.name, start="2026-11-26 01:00")
        bot.tick(NOW)
        self.assertEqual(bot.held_qty(), 0)

    def test_first_buy_and_levels(self):
        bot, feed, ex = make(self.dir.name)
        bot.tick(NOW)
        self.assertEqual(bot.lot(1).qty, 3)
        self.assertEqual(bot.st.base, "10.000")
        self.assertEqual(bot.lot(2).price, "9.700")
        self.assertEqual(bot.lot(3).price, "9.409")
        self.assertEqual(bot.lot(10).price, str(bot.level_price(10)))

    def test_ladder_buy_sell_rebuy_and_first_never_sold(self):
        bot, feed, ex = make(self.dir.name)
        bot.tick(NOW)  # 1차 3주
        bot.tick(NOW)  # 2차 매수 걸림 (9.7 는 10 에서 3% 아래 → 허용 범위 밖이면 안 걸림)
        self.assertIsNone(bot.st.buy)
        feed.p = 9.8
        bot.tick(NOW)
        self.assertEqual(bot.st.buy["level"], 2)
        self.assertIsNone(bot.st.sell)  # 1차는 매도 없음
        feed.p = 9.69
        bot.tick(NOW)  # 2차 체결
        self.assertEqual(bot.lot(2).qty, 1)
        bot.tick(NOW)  # 3차 매수(9.409, 9.69 에서 2.9% 아래 → 범위 밖), 2차 매도 9.982 (범위 밖)
        self.assertIsNone(bot.st.sell)
        feed.p = 9.9
        bot.tick(NOW)
        self.assertEqual(bot.st.sell["level"], 2)
        self.assertEqual(bot.st.sell["price"], "9.972")  # 2차 체결가 9.69 x 1.029, 올림
        feed.p = 10.0
        bot.tick(NOW)  # 2차 익절
        self.assertEqual(bot.lot(2).qty, 0)
        self.assertEqual(bot.lot(1).qty, 3)
        self.assertAlmostEqual(bot.st.realized, 10.0 - 9.69, places=6)
        feed.p = 9.75
        bot.tick(NOW)
        bot.tick(NOW)
        feed.p = 9.6
        bot.tick(NOW)
        self.assertEqual(bot.lot(2).qty, 1)  # 다시 내려와서 또 삼

    def test_max_levels(self):
        bot, feed, ex = make(self.dir.name, levels=3, band_pct=50)
        bot.tick(NOW)
        feed.p = 1.0
        for _ in range(10):
            bot.tick(NOW)
        self.assertEqual(bot.held_qty(), 3 + 2)
        self.assertEqual([x.qty for x in bot.st.lots], [3, 1, 1])
        self.assertIsNone(bot.st.buy)

    def test_pause_on_position_mismatch(self):
        bot, feed, ex = make(self.dir.name)
        bot.tick(NOW)
        bot.cfg.dry_run = False  # 포지션 확인만 켜기
        ex.pos = 0
        bot.last_pos_check = 0
        bot.tick(NOW)
        bot.last_pos_check = 0
        bot.tick(NOW)
        self.assertTrue(bot.st.paused)

    def test_state_survives_restart(self):
        bot, feed, ex = make(self.dir.name)
        bot.tick(NOW)
        bot2, _, _ = make(self.dir.name)
        self.assertEqual(bot2.lot(1).qty, 3)
        self.assertEqual(bot2.st.base, "10.000")


if __name__ == "__main__":
    unittest.main()
