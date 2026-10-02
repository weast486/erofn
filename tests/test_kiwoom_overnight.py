import unittest
from datetime import datetime

from kiwoombot.config import KiwoomConfig
from kiwoombot.overnight import EtfOvernight
from tossbot.config import KST


class FakeClient:
    def __init__(self, rows, held=0, cash=10_000_000):
        self.rows = rows
        self.held = held
        self.cash = cash
        self.orders = []

    def daily_chart(self, code, base_dt):
        return self.rows

    def holdings(self):
        return {"229200": {"qty": self.held, "avg_price": 10000}} if self.held else {}

    def orderable_cash(self):
        return self.cash

    def buy(self, code, qty, price=None):
        self.orders.append(("buy", code, qty, price))
        return "1"

    def sell(self, code, qty, price=None):
        self.orders.append(("sell", code, qty, price))
        return "2"


NOW = datetime(2026, 10, 2, 15, 21, tzinfo=KST)


def rows(today, prev, ymd="20261002"):
    return [{"dt": ymd, "cur_prc": str(today)}, {"dt": "20261001", "cur_prc": str(prev)}]


class EtfOvernightTest(unittest.TestCase):
    def cfg(self, **kw):
        return KiwoomConfig(app_key="x", secret_key="y", **{"dry_run": False, **kw})

    def test_buys_half_of_equity_market_order_on_down_day(self):
        c = FakeClient(rows(9900, 10000))
        qty = EtfOvernight(c, self.cfg()).evening_buy(NOW, 2_000_000)
        self.assertEqual(c.orders, [("buy", "229200", qty, None)])
        self.assertEqual(qty, int(1_000_000 // (9900 * 1.01)))

    def test_no_buy_on_up_day_or_holiday(self):
        c = FakeClient(rows(10100, 10000))
        self.assertEqual(EtfOvernight(c, self.cfg()).evening_buy(NOW, 2_000_000), 0)
        c = FakeClient(rows(9900, 10000, ymd="20261001"))  # 오늘 일봉 없음
        self.assertEqual(EtfOvernight(c, self.cfg()).evening_buy(NOW, 2_000_000), 0)
        self.assertEqual(c.orders, [])

    def test_buy_capped_by_orderable_cash(self):
        c = FakeClient(rows(9900, 10000), cash=300_000)
        qty = EtfOvernight(c, self.cfg()).evening_buy(NOW, 2_000_000)
        self.assertEqual(qty, int(300_000 * 0.98 // (9900 * 1.01)))

    def test_morning_sell_all_held(self):
        c = FakeClient(rows(9900, 10000), held=37)
        self.assertEqual(EtfOvernight(c, self.cfg()).morning_sell(), 37)
        self.assertEqual(c.orders, [("sell", "229200", 37, None)])
        c = FakeClient(rows(9900, 10000))
        self.assertEqual(EtfOvernight(c, self.cfg()).morning_sell(), 0)

    def test_dry_run_sends_nothing(self):
        c = FakeClient(rows(9900, 10000), held=5)
        e = EtfOvernight(c, self.cfg(dry_run=True))
        e.morning_sell()
        e.evening_buy(NOW, 2_000_000)
        self.assertEqual(c.orders, [])


if __name__ == "__main__":
    unittest.main()
