import tempfile
import unittest
from datetime import datetime, time
from decimal import Decimal
from pathlib import Path

from tossbot.config import KST
from tossbot.market_calendar import TradingDay
from tossbot.split import SplitConfig, SplitStore, SplitTrader


class FakeBroker:
    """지정가 주문을 가격이 닿으면 체결하는 가짜 브로커."""

    dry_run = True

    def __init__(self):
        self.price = 12795
        self.orders = {}
        self.n = 0

    cash = 10**9

    def cash_buying_power(self):
        return self.cash

    def last_prices(self, symbols):
        return {s: Decimal(self.price) for s in symbols}

    def _new(self, side, qty, price):
        other = "SELL" if side == "BUY" else "BUY"
        if any(o["status"] == "PENDING" and o["side"] == other for o in self.orders.values()):
            raise RuntimeError("[409] opposite-pending-order-exists: 반대 포지션 미체결 주문이 존재합니다.")
        self.n += 1
        oid = f"o{self.n}"
        self.orders[oid] = {"side": side, "qty": qty, "price": price, "status": "PENDING"}
        self.fill()
        return oid

    def buy_limit(self, symbol, qty, price):
        return self._new("BUY", qty, price)

    def sell_limit(self, symbol, qty, price):
        return self._new("SELL", qty, price)

    def fill(self):
        for o in self.orders.values():
            if o["status"] != "PENDING":
                continue
            if (o["side"] == "BUY" and self.price <= o["price"]) or (o["side"] == "SELL" and self.price >= o["price"]):
                o["status"] = "FILLED"
                o["execution"] = {"filledQuantity": str(o["qty"]), "averageFilledPrice": str(o["price"])}

    def order_status(self, oid):
        self.fill()
        return self.orders[oid]

    def cancel(self, oid):
        if self.orders[oid]["status"] == "PENDING":
            self.orders[oid]["status"] = "CANCELED"

    def pending(self):
        return sorted((o["side"], o["price"]) for o in self.orders.values() if o["status"] == "PENDING")


def day():
    d = datetime(2026, 10, 29, tzinfo=KST).date()
    return TradingDay(today=d, is_open=True, previous_business_day=d, next_business_day=d,
                      market_open=datetime.combine(d, time(9, 0), KST),
                      market_close=datetime.combine(d, time(15, 30), KST))


NOW = datetime(2026, 10, 29, 10, 0, tzinfo=KST)


class SplitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.broker = FakeBroker()
        self.cfg = SplitConfig(enabled=True)
        self.t = SplitTrader(self.cfg, self.broker, SplitStore(Path(self.dir.name) / "s.json"))

    def tearDown(self):
        self.dir.cleanup()

    def step(self, price, now=NOW):
        self.broker.price = price
        self.t.tick(now, day())

    def test_initial_orders(self):
        self.assertEqual(self.t.held_qty(), 8)
        self.step(12795)
        # 토스는 매수·매도를 같이 못 걸어 가까운 쪽만: 9차 매수 12,420 (375원) < 8차 익절 13,200 (405원)
        self.assertEqual(self.broker.pending(), [("BUY", 12420)])
        self.step(13100)  # 익절가 쪽으로 가까워지면 매수를 취소하고 익절을 걺
        self.assertEqual(self.broker.pending(), [("SELL", 13200)])
        self.step(13000)  # 조금 내려와도(간격의 1/4 안) 바꾸지 않음
        self.assertEqual(self.broker.pending(), [("SELL", 13200)])

    def test_sell_then_rebuy_same_level(self):
        self.step(12795)
        self.step(13210)  # 매수 취소·8차 익절 체결 → 다음 확인 때 반영
        self.step(13210)
        self.assertEqual(self.t.held_qty(), 7)
        self.assertEqual(self.broker.pending(), [("SELL", 13590)])  # 7차 익절이 더 가까움
        self.step(12800)  # 8차 다시 삼
        self.step(12800)
        self.assertEqual(self.t.held_qty(), 8)
        self.assertAlmostEqual(self.t.st.realized, 390)

    def test_fall_to_bottom_no_stop(self):
        self.step(12795)
        for p in [12400] * 3 + [12000] * 3 + [9000] * 3:
            self.step(p)
        self.assertEqual(self.t.held_qty(), 10)
        # 손절 없음: 10차 익절 주문만 남음
        self.assertEqual(self.broker.pending(), [("SELL", 12420)])

    def test_all_sold_then_reentry(self):
        self.step(12795)
        for _ in range(12):
            self.step(16000)
        self.assertEqual(self.t.held_qty(), 0)
        self.assertEqual(self.broker.pending(), [])
        self.step(16200)  # 고점 갱신
        self.step(15900)  # 16,200 -2% = 15,876 위 → 대기
        self.assertEqual(self.t.held_qty(), 0)
        self.step(15870)  # 재진입 (체결은 다음 확인 때 반영)
        self.step(15870)
        self.assertEqual(self.t.held_qty(), 1)
        self.assertEqual(self.t.st.cycle, 2)
        self.assertEqual(self.t.st.start, 15870)
        self.assertEqual(self.t.st.gap, 390)  # 15,870 x 2.5% = 396.75 → 10원 단위 내림

    def test_no_reentry_when_off(self):
        t = SplitTrader(SplitConfig(enabled=True, reentry=False), self.broker,
                        SplitStore(Path(self.dir.name) / "s3.json"))
        self.broker.price = 12795
        t.tick(NOW, day())
        for _ in range(12):
            self.broker.price = 16000
            t.tick(NOW, day())
        self.assertEqual(t.held_qty(), 0)
        self.assertTrue(t.st.paused)
        for p in (15000, 14000, 14000):
            self.broker.price = p
            t.tick(NOW, day())
        self.assertEqual(t.held_qty(), 0)
        self.assertEqual(self.broker.pending(), [])

    def test_no_new_orders_outside_session(self):
        self.step(12795, datetime(2026, 10, 29, 15, 25, tzinfo=KST))
        self.assertEqual(self.broker.pending(), [])

    def test_state_persists(self):
        self.step(12795)
        t2 = SplitTrader(self.cfg, self.broker, SplitStore(Path(self.dir.name) / "s.json"))
        self.assertEqual(t2.held_qty(), 8)
        self.assertEqual(t2.st.buy_order["price"], 12420)

    def test_extend_levels_keeps_held(self):
        self.step(12795)
        t2 = SplitTrader(SplitConfig(enabled=True, levels=50), self.broker,
                         SplitStore(Path(self.dir.name) / "s.json"))
        self.assertEqual(len(t2.st.lots), 40)  # 40차 330원이 마지막 (0 이하는 안 만듦)
        self.assertEqual(t2.st.lots[-1].price, 330)
        self.assertEqual(t2.held_qty(), 8)

    def test_regap_below_held(self):
        from tossbot.split import regap
        n = regap(self.t.st, 150)
        lots = self.t.st.lots
        self.assertEqual([x.price for x in lots[7:10]], [12810, 12660, 12510])
        self.assertEqual(lots[-1].price, 60)
        self.assertEqual(n, len(lots) - 8)
        self.step(12795)
        # 8차 익절은 원래 폭(+390), 9차 매수는 새 간격
        self.assertEqual(self.broker.pending(), [("BUY", 12660)])
        self.step(12650)  # 9차 체결
        self.step(12650)
        self.assertEqual(self.broker.pending(), [("BUY", 12510)])
        self.step(12800)  # 9차 익절(+150 = 12,810)이 더 가까움
        self.assertEqual(self.broker.pending(), [("SELL", 12810)])

    def test_skip_buy_when_cash_short(self):
        self.broker.cash = 5000
        self.step(12795)
        self.assertEqual(self.broker.pending(), [])
        self.broker.cash = 10**9
        self.step(12795)  # 같은 날은 매수를 다시 시도하지 않고 익절 주문을 걺
        self.assertEqual(self.broker.pending(), [("SELL", 13200)])

    def test_budget_per_level(self):
        from tossbot.split import regap
        t = SplitTrader(SplitConfig(enabled=True, budget=20000), self.broker,
                        SplitStore(Path(self.dir.name) / "s4.json"))
        regap(t.st, 150)
        self.assertEqual(t.buy_qty(12660), 1)
        self.assertEqual(t.buy_qty(9960), 2)
        self.assertEqual(t.buy_qty(6600), 3)
        self.broker.price = 9900  # 크게 빠진 뒤: 9차부터 차례로 삼 (9차 12,660 은 1주)
        t.tick(NOW, day())
        self.assertEqual(self.broker.pending(), [])  # 9차 매수는 바로 체결됨
        t.tick(NOW, day())
        self.assertEqual(t.held_qty(), 9)
        held9 = t.lot(9)
        self.assertEqual(held9.qty, 1)
        # 10,000원 아래 차수는 2주씩
        lot = next(x for x in t.st.lots if x.price < 10000)
        self.assertEqual(t.buy_qty(lot.price), 2)

    def test_stale_day_order_from_yesterday(self):
        # 토스: 전날 DAY 주문이 마감으로 없어져도 조회는 PENDING, 취소는 404
        self.step(13100)
        self.assertEqual(self.broker.pending(), [("SELL", 13200)])
        oid = self.t.st.sell_order["id"]
        self.broker.orders[oid]["orderedAt"] = "2026-10-28T10:00:00+09:00"
        self.broker.orders[oid]["timeInForce"] = "DAY"
        self.broker.orders[oid]["status"] = "STALE"  # 반대 주문 검사·체결에서 빼되 조회는 PENDING
        real = self.broker.order_status
        self.broker.order_status = lambda i: dict(real(i), status="PENDING") if i == oid else real(i)
        self.step(12795)
        self.assertIsNone(self.t.st.sell_order)
        self.assertEqual(self.broker.pending(), [("BUY", 12420)])

    def test_cancel_not_found_clears_order(self):
        from tossbot.client import TossApiError
        self.step(13100)
        oid = self.t.st.sell_order["id"]

        def gone(i):
            raise TossApiError(404, "order-not-found", "주문을 찾을 수 없습니다.")
        self.broker.orders[oid]["status"] = "STALE"
        real = self.broker.order_status
        self.broker.order_status = lambda i: dict(real(i), status="PENDING") if i == oid else real(i)
        self.broker.cancel = gone
        self.step(12500)  # 매수 쪽이 훨씬 가까워져 매도를 취소하려 함
        self.assertIsNone(self.t.st.sell_order)
        self.assertEqual(self.broker.pending(), [("BUY", 12420)])

    def test_canceled_order_still_pending_in_single_lookup(self):
        # 토스: 단건 조회는 PENDING 인데 끝난 주문 목록에는 CANCELED (2026-11-04 실전)
        self.step(13100)
        oid = self.t.st.sell_order["id"]
        self.broker.orders[oid]["status"] = "CANCELED"
        real = self.broker.order_status
        self.broker.order_status = lambda i: dict(real(i), status="PENDING") if i == oid else real(i)
        self.broker.closed_order = lambda i, sym: (
            dict(self.broker.orders[i], orderId=i) if self.broker.orders[i]["status"] != "PENDING" else None)
        self.step(12500)
        self.assertIsNone(self.t.st.sell_order)
        self.assertEqual(self.broker.pending(), [("BUY", 12420)])
        self.assertEqual(self.t.held_qty(), 8)

    def test_pause_when_bot_holds_symbol(self):
        t = SplitTrader(self.cfg, self.broker, SplitStore(Path(self.dir.name) / "s2.json"),
                        bot_symbols=lambda: {"229200"})
        t.tick(NOW, day())
        self.assertTrue(t.st.paused)
        self.assertEqual(self.broker.pending(), [])


if __name__ == "__main__":
    unittest.main()
