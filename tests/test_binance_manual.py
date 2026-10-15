import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from binancebot.manual import ManualTrader, Store, moving_average, new_order

T0 = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = T0

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


class Fake:
    """가격을 차례로 돌려주는 가짜 시세. klines 는 종가가 일정한 1시간봉."""

    def __init__(self, seq, close=100.0):
        self.seq, self.i, self.close = list(seq), 0, close

    def prices(self):
        p = self.seq[min(self.i, len(self.seq) - 1)]
        self.i += 1
        return {"AUSDT": p}

    def klines(self, symbol, start_ms, limit=500, interval="1m"):
        step = 3_600_000
        return [[start_ms + k * step, self.close, self.close, self.close, self.close, 10.0] for k in range(limit)]


def make(seq, order, close=100.0):
    clock = Clock()
    tmp = tempfile.mkdtemp()
    Store(tmp).add(order)
    tr = ManualTrader(Fake(seq, close), tmp, 1000.0, now_fn=clock.now, sleep_fn=clock.sleep, poll=1)
    for _ in seq:
        tr.tick()
        clock.sleep(1)
    return Store(tmp).load()[0]


class ManualTest(unittest.TestCase):
    def test_moving_average(self):
        rows = [[0, 0, 0, 0, c, v] for c, v in [(10, 1), (20, 3)]]
        self.assertEqual(moving_average(rows, "sma", 2), 15)
        self.assertEqual(moving_average(rows, "vwma", 2), 17.5)
        self.assertIsNone(moving_average(rows, "sma", 3))

    def test_price_long_touch_then_target(self):
        o = new_order("A", "long", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, {"type": "r", "r": 2}, lev=2, now=T0)
        o = make([101, 100.5, 99.9, 101, 104.2], o)
        self.assertEqual(o["status"], "done")
        self.assertEqual(o["pos"]["entry"], 99.9)
        self.assertAlmostEqual(o["pos"]["stop"], 99.9 * 0.98)
        self.assertEqual(o["result"]["reason"], "target")
        self.assertAlmostEqual(o["result"]["exit"], 99.9 + 2 * (99.9 - 99.9 * 0.98))
        self.assertAlmostEqual(o["pos"]["qty"], 2000 / 99.9)

    def test_not_armed_when_already_past(self):
        # 등록할 때 이미 위치 아래 → 위로 올라갔다가 다시 닿아야 진입
        o = new_order("A", "long", {"type": "price", "price": 100}, {"type": "price", "price": 95}, now=T0)
        self.assertEqual(make([99, 98, 99.5], o)["status"], "waiting")
        o = new_order("A", "long", {"type": "price", "price": 100}, {"type": "price", "price": 95}, now=T0)
        self.assertEqual(make([99, 101, 100], o)["status"], "position")

    def test_ma_short_touch_then_stop(self):
        o = new_order("A", "short", {"type": "ma", "tf": "1h", "kind": "vwma", "len": 100}, {"type": "pct", "pct": 1}, None, now=T0)
        o = make([98, 99, 100.1, 100.5, 101.2], o, close=100.0)
        self.assertEqual((o["status"], o["result"]["reason"]), ("done", "stop"))
        self.assertEqual(o["pos"]["entry"], 100.1)
        self.assertLess(o["result"]["pnl"], 0)

    def test_mid_auto_short(self):
        # 고점 22, 저점 20 → 중간 21. 저점이 19 로 내려가면 중간 20.5 로 따라감 → 20.5 에서 숏, 익절 = 마지막 저점 19
        o = new_order("A", "short", {"type": "mid", "high": 22, "low": 20, "ratio": 0.5, "auto": True}, {"type": "anchor"}, {"type": "far"}, now=T0)
        o = make([20.2, 19.0, 19.5, 20.6, 19.8, 18.9], o)
        self.assertEqual(o["entry"]["low"], 19.0)
        self.assertEqual(o["pos"]["entry"], 20.6)
        self.assertAlmostEqual(o["pos"]["stop"], 22 * 1.001)
        self.assertEqual(o["pos"]["tp"], 19.0)
        self.assertEqual(o["result"]["reason"], "target")

    def test_mid_broken_and_expiry_and_cancel(self):
        o = new_order("A", "short", {"type": "mid", "high": 22, "low": 20}, {"type": "anchor"}, now=T0)
        self.assertEqual(make([20.2, 22.1], o)["result"]["reason"], "broken")
        o = new_order("A", "long", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, hours=1 / 3600, now=T0)
        self.assertEqual(make([101, 101, 101], o)["status"], "expired")
        o = new_order("A", "long", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, now=T0)
        o["cancel"] = True
        self.assertEqual(make([101], o)["status"], "cancelled")

    def test_ma_stop_ma_target_and_close_time(self):
        # 숏 진입 100. 손절 = 끝난 1시간봉 시가·종가가 모두 이평선 위 / 익절 = 가격이 이평선에 닿으면 / 종료 시각
        def order(**kw):
            return new_order("A", "short", {"type": "price", "price": 100}, {"type": "ma_close", "tf": "1h", "kind": "sma", "len": 20},
                             {"type": "ma", "tf": "1h", "kind": "sma", "len": 20}, now=T0, **kw)
        o = make([99, 100, 99.5, 98.0], order(), close=98.0)          # 이평선 98 (진입가 아래) → 98 에 닿아 익절
        self.assertEqual((o["result"]["reason"], o["result"]["exit"]), ("target", 98.0))
        o = make([99, 100, 100.5, 101], order(), close=98.0)          # 진입 전에 끝난 봉은 손절 판단에 안 씀 → 보유 유지
        self.assertEqual((o["status"], o["pos"]["stop"]), ("position", 0.0))
        o = make([99, 100, 100.5, 100.2], order(close_at=T0 + timedelta(seconds=3)), close=98.0)
        self.assertEqual((o["status"], o["result"]["reason"]), ("done", "time"))
        o = make([99, 99, 99], order(close_at=T0 + timedelta(seconds=2)), close=98.0)
        self.assertEqual(o["status"], "expired")
        with self.assertRaises(ValueError):
            new_order("A", "short", {"type": "price", "price": 100}, {"type": "ma_close", "tf": "1h", "kind": "sma", "len": 20}, {"type": "r", "r": 2})

    def test_ma_close_stop_triggers_after_new_bar(self):
        clock = Clock()
        tmp = tempfile.mkdtemp()
        Store(tmp).add(new_order("A", "short", {"type": "price", "price": 100}, {"type": "ma_close", "tf": "1h", "kind": "sma", "len": 20}, now=T0))
        fake = Fake([99, 100, 103, 103], close=98.0)
        tr = ManualTrader(fake, tmp, 1000.0, now_fn=clock.now, sleep_fn=clock.sleep, poll=1)
        for _ in range(3):
            tr.tick(); clock.sleep(1)
        self.assertEqual(Store(tmp).load()[0]["status"], "position")
        base = fake.klines                                              # 새 봉이 끝남: 시가 102·종가 103, 이평선은 그 아래
        fake.klines = lambda symbol, start_ms, limit=500, interval="1m": base(symbol, start_ms, limit, interval)[:-1] + [
            [int(clock.t.timestamp() * 1000) - 3_601_000, 102.0, 103.0, 102.0, 103.0, 10.0]]
        clock.sleep(3600); tr.ma_cache.clear(); tr.tick()
        o = Store(tmp).load()[0]
        self.assertEqual((o["status"], o["result"]["reason"]), ("done", "stop"))

    def test_void_when_ma_crosses_price_before_entry(self):
        v = {"tf": "1h", "kind": "sma", "len": 20}
        o = new_order("A", "short", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, now=T0, void_ma=v)
        self.assertEqual(make([99, 99.5], o, close=98.0)["result"]["reason"], "void")      # 이평선 98 이 가격 아래 → 폐기
        o = new_order("A", "short", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, now=T0, void_ma=v)
        self.assertEqual(make([99, 100], o, close=101.0)["status"], "position")             # 이평선 101 이 가격 위 → 진입

    def test_void_when_ma_reaches_entry_level(self):
        o = new_order("A", "short", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, now=T0,
                      void_ma={"tf": "1h", "kind": "sma", "len": 20, "ref": "level"})
        self.assertEqual(make([97, 97.5], dict(o), close=99.0)["result"]["reason"], "void")   # 이평선 99 <= 진입 위치 100 → 폐기
        self.assertEqual(make([99, 100], dict(o, id="x"), close=100.5)["status"], "position")  # 이평선 100.5 는 진입 위치 위 → 진입

    def test_validation(self):
        with self.assertRaises(ValueError):
            new_order("A", "long", {"type": "price", "price": 100}, {"type": "anchor"})
        with self.assertRaises(ValueError):
            new_order("A", "long", {"type": "price", "price": 100}, {"type": "pct", "pct": 2}, lev=6)
        with self.assertRaises(ValueError):
            new_order("A", "short", {"type": "mid", "high": 20, "low": 22}, {"type": "anchor"})


if __name__ == "__main__":
    unittest.main()
