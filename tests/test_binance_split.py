import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from binancebot.client import BinanceError
from binancebot.split import SplitConfig, SplitTrader, fvg_conflict, us_weekend

META = {"symbols": [{"symbol": "SOXLUSDT", "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
    {"filterType": "MIN_NOTIONAL", "notional": "5"},
    {"filterType": "PERCENT_PRICE", "multiplierUp": "1.05", "multiplierDown": "0.95"}]}]}


class FakeExchange:
    """가격을 차례로 움직이는 가짜 바이낸스. 걸어 둔 지정가는 가격이 닿으면 그 가격에 체결,
    GTX(Post Only)는 바로 체결될 가격이면 거절, IOC·시장가는 지금 가격으로 체결, 거래소 손절은 닿으면 전량 정리."""

    def __init__(self, seq):
        self.seq, self.i = list(seq), -1
        self.p = self.seq[0]
        self.pos = 0.0
        self.orders = {}      # cid -> dict
        self.done = {}        # cid -> 끝난 주문
        self.stop = None
        self.log = []
        self.trades = []
        self.next_id = 0
        self.balance = 1000.0

    # 시세
    def exchange_info(self):
        return META

    def prices(self):
        self.i = min(self.i + 1, len(self.seq) - 1)
        self.p = self.seq[self.i]
        self._match()
        return {"SOXLUSDT": self.p}

    def _match(self):
        for cid, o in list(self.orders.items()):
            hit = self.p <= o["price"] if o["side"] == "BUY" else self.p >= o["price"]
            if hit:
                self._fill(o, o["price"])
                self.done[cid] = dict(o, status="FILLED")
                del self.orders[cid]
        if self.stop is not None and self.p <= self.stop and self.pos > 0:
            self.log.append(("stop", self.p, self.pos))
            self.next_id += 1
            self.trades.append({"orderId": self.next_id, "side": "SELL", "price": self.p, "qty": self.pos})
            self.pos = 0.0
            self.stop = None

    def _fill(self, o, px):
        o["executedQty"] = o["origQty"]
        o["avgPrice"] = px
        self.trades.append({"orderId": o["orderId"], "side": o["side"], "price": px, "qty": o["origQty"]})
        self.pos += o["origQty"] if o["side"] == "BUY" else -o["origQty"]
        self.log.append((o["side"], o.get("type"), px, o["origQty"]))

    # 계좌
    def sync_time(self):
        pass

    def hedge_mode(self):
        return False

    def set_margin_type(self, *a):
        if self.orders or self.pos:
            raise BinanceError(400, -4067, "Position side cannot be changed if there exists open orders.")

    def set_leverage(self, *a):
        pass

    def position_amt(self, symbol):
        return round(self.pos, 9), 0.0

    def equity(self):
        return self.balance

    def order(self, **q):
        qty = float(q["quantity"])
        self.next_id += 1
        o = {"side": q["side"], "type": q["type"], "origQty": qty, "executedQty": 0.0, "avgPrice": 0.0,
             "clientOrderId": q["newClientOrderId"], "orderId": self.next_id}
        if q["type"] == "MARKET":
            if q.get("reduceOnly"):
                qty = min(qty, self.pos)
                o["origQty"] = qty
            self._fill(o, self.p)
            return o
        price = float(q["price"])
        if not self.p * 0.95 <= price <= self.p * 1.05:   # 지정가 허용 범위 ±5%
            raise BinanceError(400, -4024, "Limit price out of range")
        o["price"] = price
        crosses = self.p <= price if q["side"] == "BUY" else self.p >= price
        if q["timeInForce"] == "GTX":
            if crosses:
                raise BinanceError(400, -5022, "Post Only order will be rejected")
            self.orders[q["newClientOrderId"]] = o
            return o
        if q["timeInForce"] == "IOC":
            if crosses:
                self._fill(o, self.p)
                o = dict(o, avgPrice=price)   # 실제 바이낸스처럼 응답 가격이 실제 체결가와 다를 수 있음
            return o
        raise AssertionError(q)

    def fills(self, symbol, start_ms=None, order_id=None):
        return [t for t in self.trades if order_id is None or t["orderId"] == order_id]

    def open_orders(self, symbol):
        # lag=True: 방금 낸 주문이 한 번은 미체결 목록에 안 보임 (실제 바이낸스에서 겪은 지연)
        out = [dict(o) for cid, o in self.orders.items() if not (getattr(self, "lag", False) and cid not in self.seen)]
        self.seen = getattr(self, "seen", set()) | set(self.orders)
        return out

    def query_order(self, symbol, cid):
        if cid in self.orders:
            return dict(self.orders[cid], status="NEW")
        return self.done[cid]

    def cancel_all(self, symbol):
        for cid, o in self.orders.items():
            self.done[cid] = dict(o, status="CANCELED")
        self.orders = {}
        self.stop = None

    def cancel_order(self, symbol, cid):
        if cid not in self.orders:
            raise BinanceError(400, -2011, "Unknown order sent.")
        self.done[cid] = dict(self.orders.pop(cid), status="CANCELED")

    def stop_market(self, symbol, side, stop):
        self.stop = stop
        return "algo"


def cfg(dry, **kw):
    c = SplitConfig(dry_run=dry, capital=1000.0, splits=5, leverage=1.0, box_pct=10.0, stop_steps=2.0, weekend_first=False,
                    state_dir=tempfile.mkdtemp(), api_key="k" if not dry else "", api_secret="s" if not dry else "")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def run(seq, dry=True, now=None, **kw):
    ex = FakeExchange(seq)
    tr = SplitTrader(ex, cfg(dry, **kw), now_fn=(lambda: now) if now else None, sleep_fn=lambda s: None)
    tr.last_check = -1e9
    tr.run(max_ticks=len(seq))
    return tr, ex


class SplitTest(unittest.TestCase):
    # 1차 100, 간격 2 → 98, 96, 94, 92, 손절 = 92 - 4 = 88. 익절 = 매수가 + 2 + 매수가 x 0.1%
    def test_dry_grid_tp_and_reentry(self):
        seq = [100, 99, 97.9, 95.9, 97, 98.2, 100.1, 102.2, 103, 104, 101.5]
        tr, _ = run(seq)
        st = tr.st
        self.assertEqual(st.tps, 3)          # 3차(96 → 98.1)·2차(98 → 100.1)·1차(100 → 102.1)
        self.assertEqual(st.cycle, 2)        # 고점 104 의 -2% = 101.92 아래에서 재진입
        self.assertAlmostEqual(st.start_price, 101.5)
        self.assertGreater(st.stake, 1000.0 - 1)  # 수수료 빼고도 이익

    def test_dry_stop_then_restart_at_stop_price(self):
        seq = [100, 98, 96, 94, 92, 90, 87.5, 87]
        tr, _ = run(seq)
        st = tr.st
        self.assertEqual(st.stops, 1)
        self.assertEqual(st.cycle, 2)
        self.assertAlmostEqual(st.start_price, 87.5)
        self.assertLess(st.stake, 1000.0)
        rows = (Path(tr.cfg.state_dir) / "trades.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sum(1 for r in rows if ",stop," in r), 5)

    def test_compounding_only_on_new_cycle(self):
        seq = [100, 97.9, 100.5, 102.2, 104, 101.9]
        tr, _ = run(seq)
        st = tr.st
        self.assertEqual(st.cycle, 2)
        self.assertAlmostEqual(st.notional, st.stake / 5, delta=0.5)  # 새 사이클 금액 = 장부 / N

    def test_immediate_reentry_and_first_tp_two_steps(self):
        # 1차 익절 = 100 + 2 x 2 + 0.1 = 104.1, 2차(98) 익절 = 100.098
        seq = [100, 97.9, 100.2, 102.5, 104.2, 104.0]
        tr, _ = run(seq, reentry_drop_pct=0.0, first_tp_steps=2.0)
        st = tr.st
        self.assertEqual(st.tps, 2)
        self.assertEqual(st.cycle, 2)                 # 고점 하락을 기다리지 않고 바로 재진입
        self.assertAlmostEqual(st.start_price, 104.2)
        self.assertIsNone(st.waiting_high)

    def test_live_immediate_reentry(self):
        seq = [100, 97.9, 100.2, 102.5, 104.2, 104.2, 104.0]
        tr, ex = run(seq, dry=False, reentry_drop_pct=0.0, first_tp_steps=2.0)
        st = tr.st
        self.assertEqual(st.cycle, 2)
        held = sum(h["qty"] for h in st.held if h)
        self.assertAlmostEqual(ex.pos, held, places=6)
        self.assertIsNotNone(ex.stop)

    def test_stops_when_money_too_small(self):
        tr, _ = run([100, 99], capital=20.0)
        self.assertIn("최소 주문", tr.st.ended)
        self.assertEqual(tr.st.cycle, 0)

    def test_live_resting_post_only_orders(self):
        seq = [100, 99.5, 98.5, 97.9, 96.5, 95.9, 97, 98.3, 99, 100.3, 101, 102.2, 103]
        tr, ex = run(seq, dry=False)
        st = tr.st
        self.assertTrue(any(e[0] == "BUY" and e[1] == "MARKET" for e in ex.log))   # 1차 시장가
        self.assertTrue(any(e[0] == "BUY" and e[1] == "LIMIT" for e in ex.log))    # 2·3차 지정가
        self.assertEqual(st.tps, 3)
        self.assertIsNotNone(st.waiting_high)                 # 다 팔고 재진입 대기
        self.assertAlmostEqual(ex.pos, 0.0, places=6)
        self.assertTrue(all("entry" in cid for cid in ex.orders))   # 남은 주문은 재진입 지정가뿐
        self.assertEqual(st.paused, "")

    def test_live_reentry_by_limit_order(self):
        # 다 익절 → 고점 103 → 105 로 올라 재진입 지정가 100.94 취소 → 102.90 에 다시 걸고 102.5 에서 체결
        seq = [100, 99.5, 98.5, 97.9, 96.5, 95.9, 97, 98.3, 99, 100.3, 101, 102.2, 103, 103, 105, 104, 102.5, 102.5]
        tr, ex = run(seq, dry=False)
        st = tr.st
        self.assertEqual(st.cycle, 2)
        self.assertAlmostEqual(st.start_price, 102.9)
        self.assertEqual(sum(1 for e in ex.log if e[0] == "BUY" and e[1] == "MARKET"), 1)   # 첫 진입만 시장가
        self.assertTrue(any("entry" in cid and o["status"] == "CANCELED" for cid, o in ex.done.items()))
        held = sum(h["qty"] for h in st.held if h)
        self.assertAlmostEqual(ex.pos, held, places=6)
        self.assertIsNotNone(ex.stop)
        self.assertEqual(st.paused, "")

    def test_live_size_by_account_balance(self):
        ex = FakeExchange([100, 100])
        ex.balance = 1500.0  # 입금으로 계좌가 장부(1000)보다 많음
        tr = SplitTrader(ex, cfg(False), sleep_fn=lambda s: None)
        tr.last_check = -1e9
        tr.run(max_ticks=2)
        self.assertAlmostEqual(tr.st.notional, 1500.0 / 5)
        self.assertAlmostEqual(tr.st.stake, 1000.0, delta=1)  # 장부는 그대로 (손익 기록용)
        ex2 = FakeExchange([100, 100])
        ex2.balance = 1500.0
        tr2 = SplitTrader(ex2, cfg(False, size_by_account=False), sleep_fn=lambda s: None)
        tr2.last_check = -1e9
        tr2.run(max_ticks=2)
        self.assertAlmostEqual(tr2.st.notional, 1000.0 / 5)

    def test_us_weekend(self):
        utc = timezone.utc
        self.assertFalse(us_weekend(datetime(2026, 10, 23, 19, 59, tzinfo=utc)))  # 금 15:59 동부
        self.assertTrue(us_weekend(datetime(2026, 10, 23, 20, 0, tzinfo=utc)))    # 금 16:00
        self.assertTrue(us_weekend(datetime(2026, 10, 25, 23, 0, tzinfo=utc)))    # 일 19:00
        self.assertTrue(us_weekend(datetime(2026, 10, 26, 13, 29, tzinfo=utc)))   # 월 09:29
        self.assertFalse(us_weekend(datetime(2026, 10, 26, 13, 30, tzinfo=utc)))  # 월 09:30

    def test_live_weekend_reentry_at_previous_first(self):
        # 20분할: 간격 0.5, 1차 100 → 익절 100.6. 고점 -2% = 98.7 지만 미국 주말엔 직전 1차 100.00 에 다시 삼
        seq = [100, 100.7, 100.7, 100.3, 99.95, 99.95]
        sat = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)
        tr, ex = run(seq, dry=False, now=sat, splits=20, weekend_first=True)
        self.assertEqual(tr.st.cycle, 2)
        self.assertAlmostEqual(tr.st.start_price, 100.0)
        self.assertEqual(sum(1 for e in ex.log if e[0] == "BUY" and e[1] == "MARKET"), 1)
        wed = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
        tr, _ = run(seq, dry=False, now=wed, splits=20, weekend_first=True)
        self.assertEqual(tr.st.cycle, 1)                    # 평일엔 고점 -2%(98.7) 까지 기다림
        self.assertIsNotNone(tr.st.waiting_high)

    def test_live_lagging_open_orders_no_double_buy(self):
        seq = [100, 99.5, 99.4, 99.3, 98.5, 97.9, 97.5, 96.5, 95.9, 95.5, 97, 98.3, 99, 100.3, 101, 102.2, 103]
        ex = FakeExchange(seq)
        ex.lag = True
        ex.seen = set()
        tr = SplitTrader(ex, cfg(False), sleep_fn=lambda s: None)
        tr.last_check = -1e9
        tr.run(max_ticks=len(seq))
        buys = [e for e in ex.log if e[0] == "BUY" and e[1] == "LIMIT"]
        self.assertEqual(len(buys), 2)                 # 2·3차 한 번씩만
        self.assertAlmostEqual(ex.pos, 0.0, places=6)
        self.assertEqual(tr.st.paused, "")

    def test_live_restart_with_open_orders(self):
        seq = [100, 99.5, 99.0, 99.2]
        tr, ex = run(seq, dry=False)
        self.assertTrue(ex.orders)
        tr2 = SplitTrader(ex, tr.cfg, sleep_fn=lambda s: None)   # 주문이 걸린 채 다시 켜도 멈추지 않음
        tr2.run(max_ticks=2)
        self.assertEqual(tr2.st.cycle, 1)

    def test_live_far_passed_tp_sells_within_band(self):
        ex = FakeExchange([100, 100, 100])
        tr = SplitTrader(ex, cfg(False), sleep_fn=lambda s: None)
        tr.prepare()
        tr.tick()                                   # 1차 100 매수
        ex.pos += 0.11
        tr.st.held[3] = {"price": 80.0, "qty": 0.11}  # 익절가 82.08 이 현재가보다 훨씬 아래인 차수
        tr.last_check = -1e9
        tr.tick()
        self.assertIsNone(tr.st.held[3])            # 범위 안 가격(95.5) IOC 로 팔림
        rows = (Path(tr.cfg.state_dir) / "trades.csv").read_text(encoding="utf-8").splitlines()
        self.assertIn(",tp,4,100.0000,", rows[-1])  # 장부에는 실제 체결가(100)로
        self.assertAlmostEqual(ex.pos, tr.st.held[0]["qty"], places=6)

    def test_live_exchange_stop_fires_first(self):
        seq = [100, 98, 96, 94, 92, 90.5, 87.9, 87.9, 87.5]
        tr, ex = run(seq, dry=False)
        st = tr.st
        self.assertTrue(any(e[0] == "stop" for e in ex.log))  # 거래소 손절이 먼저 정리
        self.assertEqual(st.stops, 1)
        self.assertEqual(st.cycle, 2)
        held = sum(h["qty"] for h in st.held if h)
        self.assertAlmostEqual(ex.pos, held, places=6)        # 새 1차만 들고 있음
        self.assertIsNotNone(ex.stop)                         # 새 사이클 손절 다시 걸림

    def test_live_exchange_stop_while_bot_off(self):
        # 봇이 꺼져 있는 사이 거래소 손절이 체결된 뒤 다시 켜짐: 첫 확인에서 포지션 0 ≠ 장부 → 멈추지 말고 손절로 처리
        ex = FakeExchange([100, 98, 96, 94, 92, 90.5, 87.0, 86.0, 86.0])
        tr = SplitTrader(ex, cfg(False), sleep_fn=lambda s: setattr(tr, "last_check", -1e9))
        tr.last_check = -1e9
        tr.run(max_ticks=9)
        st = tr.st
        self.assertEqual(st.paused, "")
        self.assertEqual(st.stops, 1)
        self.assertEqual(st.cycle, 2)
        rows = (Path(tr.cfg.state_dir) / "trades.csv").read_text(encoding="utf-8").splitlines()
        stop_rows = [r for r in rows if ",stop," in r]
        self.assertTrue(stop_rows and all(",87.0000," in r for r in stop_rows))  # 거래소 체결가로 기록
        held = sum(h["qty"] for h in st.held if h)
        self.assertAlmostEqual(ex.pos, held, places=6)

    def test_live_pause_on_position_mismatch(self):
        ex = FakeExchange([100, 100, 100, 100])
        tr = SplitTrader(ex, cfg(False), sleep_fn=lambda s: None)
        tr.prepare()
        tr.tick()
        ex.pos += 5.0  # 다른 곳에서 산 것처럼
        tr.last_check = -1e9
        tr.tick()
        self.assertIn("포지션", tr.st.paused)

    def test_fvg_conflict(self):
        d = tempfile.mkdtemp()
        p = Path(d) / ".env.binance"
        p.write_text("BINANCE_API_KEY=k\nBN_EXCLUDE=SOXSUSDT\n", encoding="utf-8")
        c = cfg(False, fvg_env=str(p))
        self.assertIn("BN_EXCLUDE", fvg_conflict(c))
        p.write_text("BINANCE_API_KEY=k\nBN_EXCLUDE=SOXSUSDT,SOXLUSDT\n", encoding="utf-8")
        self.assertEqual(fvg_conflict(c), "")
        p.write_text("BINANCE_API_KEY=other\n", encoding="utf-8")
        self.assertEqual(fvg_conflict(c), "")


if __name__ == "__main__":
    unittest.main()
