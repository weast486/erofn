import tempfile
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
        if not hasattr(self, "_tmp"):  # 매매 내역 파일(etf_trades.json)이 실제 폴더에 남지 않게
            self._tmp = tempfile.TemporaryDirectory()
            self.addCleanup(self._tmp.cleanup)
        return KiwoomConfig(app_key="x", secret_key="y", **{"dry_run": False, "state_dir": self._tmp.name, **kw})

    def test_ledger_records_buy_then_sell_prices(self):
        """15:21 매수 주문 → 장 마감 뒤 매수가(평균 매입가) → 다음 날 아침 매도 주문 → 9시 뒤 매도가(시가)와 손익."""
        cfg = self.cfg()
        c = FakeClient(rows(9900, 10000))
        etf = EtfOvernight(c, cfg)
        qty = etf.evening_buy(NOW, 2_000_000)
        self.assertEqual(etf.load_trades()[0]["status"], "ordered")
        etf.settle(NOW)                                   # 15:21 — 아직 장 마감 전이라 그대로
        self.assertEqual(etf.load_trades()[0]["status"], "ordered")
        c.held = qty                                      # 체결됨 (평균 매입가 10,000)
        etf.settle(NOW.replace(hour=15, minute=35))
        t = etf.load_trades()[0]
        self.assertEqual((t["status"], t["buy_price"], t["qty"]), ("held", 10000, qty))
        nxt = datetime(2026, 10, 5, 8, 45, tzinfo=KST)
        etf.morning_sell(nxt)
        self.assertEqual(etf.load_trades()[0]["status"], "selling")
        etf.settle(nxt)                                   # 08:45 — 아직 시가가 없음
        self.assertEqual(etf.load_trades()[0]["status"], "selling")
        c.held = 0
        c.rows = [{"dt": "20261005", "open_pric": "10150", "cur_prc": "10200"}] + c.rows
        etf.settle(nxt.replace(hour=9, minute=5))
        t = etf.load_trades()[0]
        self.assertEqual((t["status"], t["sell_day"], t["sell_price"]), ("closed", "20261005", 10150))
        self.assertEqual(t["pnl"], 150 * qty)
        self.assertAlmostEqual(t["ret_pct"], 1.5)

    def test_rejected_market_buy_retries_with_upper_limit_margin(self):
        c = FakeClient(rows(9900, 10000), cash=1_000_000)
        first = {"n": 0}
        real_buy = c.buy

        def buy(code, qty, price=None):
            first["n"] += 1
            if first["n"] == 1:
                raise RuntimeError("증거금 부족")
            return real_buy(code, qty, price)

        c.buy = buy
        etf = EtfOvernight(c, self.cfg())
        qty = etf.evening_buy(NOW, 1_000_000)
        self.assertEqual(qty, int(1_000_000 * 0.98 // (9900 * 1.30)))  # 상한가 기준으로 줄인 수량
        self.assertEqual(c.orders, [("buy", "229200", qty, None)])
        self.assertEqual(etf.load_trades()[0]["qty"], qty)

    def test_ledger_drops_unfilled_buy_and_records_untracked_holding(self):
        cfg = self.cfg()
        c = FakeClient(rows(9900, 10000))
        etf = EtfOvernight(c, cfg)
        etf.evening_buy(NOW, 2_000_000)
        etf.settle(NOW.replace(hour=15, minute=35))       # 장 마감 뒤에도 보유 없음 → 체결 안 됨
        self.assertEqual(etf.load_trades()[0]["status"], "unfilled")
        c.held = 7                                        # 기록 없이 들고 있던 ETF 를 아침에 팜
        etf.morning_sell(datetime(2026, 10, 5, 8, 45, tzinfo=KST))
        t = etf.load_trades()[-1]
        self.assertEqual((t["status"], t["qty"], t["buy_price"], t["buy_day"]), ("selling", 7, 10000, ""))

    def test_buys_half_of_equity_market_order_on_down_day(self):
        c = FakeClient(rows(9900, 10000))
        c_pct = self.cfg().etf_pct
        qty = EtfOvernight(c, self.cfg()).evening_buy(NOW, 2_000_000)
        self.assertEqual(c.orders, [("buy", "229200", qty, None)])
        self.assertEqual(qty, int(2_000_000 * c_pct / 100 // (9900 * 1.01)))

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
