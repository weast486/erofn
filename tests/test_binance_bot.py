import tempfile
import unittest
from datetime import date, datetime, timedelta

from binancebot.backtest import ET
from binancebot.bot import BotConfig, FvgTrader, next_start, prev_trading_day
from binancebot.client import filters_of, round_step, round_tick

DAY = date(2026, 9, 1)  # 화요일
PREV = date(2026, 8, 31)


def k(t: datetime, o, h, l, c, qv=1000.0):
    ms = int(t.timestamp() * 1000)
    return [ms, o, h, l, c, 1.0, ms + 59_999, qv]


class FakeClient:
    """1분봉 목록으로 klines·현재가를 흉내 낸다 (현재가 = 지금 분의 봉 종가)."""

    def __init__(self, bars: dict[str, list[list]], clock):
        self.bars, self.clock = bars, clock
        self.orders = []

    def exchange_info(self):
        f = [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}, {"filterType": "LOT_SIZE", "stepSize": "0.001"},
             {"filterType": "MIN_NOTIONAL", "notional": "5"}]
        return {"symbols": [{"symbol": s, "status": "TRADING", "quoteAsset": "USDT", "underlyingType": "EQUITY",
                             "filters": f} for s in self.bars]}

    def klines(self, symbol, start_ms, limit=500, interval="1m"):
        rows = [r for r in self.bars[symbol] if r[0] >= start_ms]
        if interval == "30m":  # 1분봉 30개씩 묶음
            out = []
            for i in range(0, len(rows), 30):
                g = rows[i:i + 30]
                out.append([g[0][0], g[0][1], max(r[2] for r in g), min(r[3] for r in g), g[-1][4], 1.0, g[-1][6], 0.0])
            return out[:limit]
        return rows[:limit]

    def prices(self):
        now = int(self.clock.t.timestamp() * 1000)
        out = {}
        for s, rows in self.bars.items():
            cur = [r for r in rows if r[0] <= now]
            if cur:
                out[s] = cur[-1][4]
        return out


class Clock:
    def __init__(self, t):
        self.t = t

    def now(self):
        return self.t

    def sleep(self, sec):
        self.t += timedelta(seconds=sec)


def day_bars(day, rows, start=(9, 30)):
    t0 = datetime(day.year, day.month, day.day, *start, tzinfo=ET)
    return [k(t0 + timedelta(minutes=i), *r) for i, r in enumerate(rows)]


class BotTest(unittest.TestCase):
    def _setup(self, today_a):
        prev_a = day_bars(PREV, [(100, 100, 100, 100, 5000.0)] * 390)
        prev_b = day_bars(PREV, [(50, 50, 50, 50, 10.0)] * 390)
        flat_b = day_bars(DAY, [(50, 50.1, 49.9, 50)] * 60)
        clock = Clock(datetime(2026, 9, 1, 9, 0, tzinfo=ET))
        client = FakeClient({"AUSDT": prev_a + day_bars(DAY, today_a), "BUSDT": prev_b + flat_b}, clock)
        tmp = tempfile.mkdtemp()
        cfg = BotConfig(top_n=1, state_dir=tmp, dry_equity=1000, poll_seconds=1)
        return FvgTrader(client, cfg, now_fn=clock.now, sleep_fn=clock.sleep), clock

    def test_long_fvg_entry_and_target(self):
        first = [(100, 101, 99, 100.5)] * 5                                       # 첫 5분봉 고가 101 · 저가 99
        fvg = [(101, 101.5, 100.8, 101.4), (101.4, 103, 101.3, 102.9), (102.9, 103.5, 102.2, 103.2)]
        after = [(103.2, 103.3, 102.0, 102.5), (102.5, 102.6, 101.3, 101.4)]     # 9:39 봉 종가 101.4 → 101.5 에 진입
        up = [(101.4, 107, 101.4, 107)] * 3                                         # 익절 101.5 + 2.5 x 2 = 106.5
        trader, clock = self._setup(first + fvg + after + up)
        st = trader.run(DAY)
        self.assertEqual(st.candidates, ["AUSDT"])
        self.assertEqual(st.ranges["AUSDT"], [101, 99])
        self.assertEqual(st.signals["AUSDT"][:2], [1, 101.5])
        pos = st.positions["AUSDT"]
        self.assertEqual(pos["entry"], 101.5)
        self.assertAlmostEqual(pos["qty"], round_step(5000 / 101.5, 0.001))
        self.assertEqual((st.phase, pos["result"]["reason"]), ("done", "target"))
        self.assertAlmostEqual(pos["result"]["exit"], 106.5)
        self.assertEqual(st.result["trades"], 1)
        # 같은 날 다시 켜도 다시 매매하지 않음
        self.assertEqual(trader.run(DAY).result["trades"], 1)

    def test_short_stop_and_time_exit(self):
        first = [(100, 101, 99, 99.5)] * 5
        fvg = [(99, 98.8, 98.5, 98.6), (98.6, 98.6, 97, 97.1), (97.1, 97.8, 96.8, 97.0)]  # 숏 진입가 98.5 (갭 다 메움)
        back = [(97.0, 98.7, 96.9, 98.6), (98.6, 101.5, 98.5, 101.4)]                   # 98.6 체결 → 101 넘어 손절
        trader, _ = self._setup(first + fvg + back + [(101.4, 101.5, 101.3, 101.4)] * 3)
        st = trader.run(DAY)
        pos = st.positions["AUSDT"]
        self.assertEqual(pos["side"], -1)
        self.assertEqual(pos["result"]["reason"], "stop")
        self.assertAlmostEqual(pos["result"]["exit"], 101)

    def test_no_fvg_no_trade(self):
        trader, clock = self._setup([(100, 101, 99, 100)] * 400)
        trader.cfg.exit_minutes = 40  # 테스트 시간을 줄임
        st = trader.run(DAY)
        self.assertEqual(st.result["reason"], "no_trade")


class TwoTradesTest(unittest.TestCase):
    def test_two_symbols_same_day_then_third_skipped(self):
        first = [(100, 101, 99, 100.5)] * 5
        fvg = [(101, 101.5, 100.8, 101.4), (101.4, 103, 101.3, 102.9), (102.9, 103.5, 102.2, 103.2)]
        dip = [(103.2, 103.3, 102.0, 102.5), (102.5, 102.6, 101.3, 101.4)]
        hold = [(101.4, 102, 101.4, 101.8)] * 30
        rows = first + fvg + dip + hold
        prev = lambda qv: day_bars(PREV, [(100, 100, 100, 100, qv)] * 390)
        clock = Clock(datetime(2026, 9, 1, 9, 0, tzinfo=ET))
        bars = {s: prev(qv) + day_bars(DAY, rows) for s, qv in [("AUSDT", 3000.0), ("BUSDT", 2000.0), ("CUSDT", 1000.0)]}
        cfg = BotConfig(top_n=3, max_trades=2, state_dir=tempfile.mkdtemp(), exit_minutes=60)
        st = FvgTrader(FakeClient(bars, clock), cfg, now_fn=clock.now, sleep_fn=clock.sleep).run(DAY)
        self.assertEqual(sorted(st.positions), ["AUSDT", "BUSDT"])   # ADR 정보가 없으면 순위 순으로 2종목
        self.assertTrue(all(p["result"]["reason"] == "time" for p in st.positions.values()))
        self.assertEqual(st.result["trades"], 2)


class AdrTest(unittest.TestCase):
    def test_adr_breaks_ties(self):
        first = [(100, 101, 99, 100.5)] * 5
        fvg = [(101, 101.5, 100.8, 101.4), (101.4, 103, 101.3, 102.9), (102.9, 103.5, 102.2, 103.2)]
        dip = [(103.2, 103.3, 102.0, 102.5), (102.5, 102.6, 101.3, 101.4)]
        rows = first + fvg + dip + [(101.4, 102, 101.4, 101.8)] * 30
        hist = lambda rng: sum((day_bars(date(2026, 8, d), [(100, 100 + rng, 100, 100, 1000.0 * (10 - rng))] * 390)
                                for d in (24, 25, 26, 27, 28, 31)), [])
        clock = Clock(datetime(2026, 9, 1, 9, 0, tzinfo=ET))
        # A 는 거래대금 1위지만 변동폭 1%, C 는 거래대금 3위지만 변동폭 5%
        bars = {s: hist(r) + day_bars(DAY, rows) for s, r in [("AUSDT", 1), ("BUSDT", 3), ("CUSDT", 5)]}
        cfg = BotConfig(top_n=3, max_trades=1, state_dir=tempfile.mkdtemp(), exit_minutes=60)
        st = FvgTrader(FakeClient(bars, clock), cfg, now_fn=clock.now, sleep_fn=clock.sleep).run(DAY)
        self.assertEqual(st.candidates, ["AUSDT", "BUSDT", "CUSDT"])
        self.assertAlmostEqual(st.adr["CUSDT"], 5.0)
        self.assertEqual(list(st.positions), ["CUSDT"])


class FakeLive(FakeClient):
    """실제 주문 경로: IOC 진입은 바로 체결, 익절 지정가는 가격이 닿으면 체결된 것으로."""

    def __init__(self, bars, clock):
        super().__init__(bars, clock)
        self.amt, self.tp = 0.0, None

    def equity(self):
        return 500.0

    def set_margin_type(self, symbol, margin="CROSSED"):
        self.orders.append(("margin", margin))

    def set_leverage(self, symbol, leverage):
        self.orders.append(("leverage", leverage))

    def order(self, **p):
        self.orders.append((p["type"], p["side"], p.get("price"), p.get("reduceOnly")))
        if p["type"] == "LIMIT" and p.get("timeInForce") == "IOC":
            self.amt = float(p["quantity"]) * (1 if p["side"] == "BUY" else -1)
            return {"executedQty": p["quantity"], "avgPrice": p["price"], "status": "FILLED"}
        if p["type"] == "LIMIT":
            self.tp = float(p["price"])
        if p["type"] == "MARKET":
            self.amt = 0.0
            return {"avgPrice": "0"}
        return {}

    def stop_market(self, symbol, side, stop):
        self.orders.append(("STOP", side, stop))
        return "algo"

    def position_amt(self, symbol):
        if self.tp is not None and self.prices()[symbol] >= self.tp:
            self.amt = 0.0
        return self.amt, 0.0

    def cancel_all(self, symbol):
        self.orders.append(("cancel_all",))


class LiveTest(unittest.TestCase):
    def test_live_orders(self):
        first = [(100, 101, 99, 100.5)] * 5
        fvg = [(101, 101.5, 100.8, 101.4), (101.4, 103, 101.3, 102.9), (102.9, 103.5, 102.2, 103.2)]
        after = [(103.2, 103.3, 102.0, 102.5), (102.5, 102.6, 101.3, 101.4)]
        up = [(101.4, 105, 101.4, 105)] * 2 + [(105, 107, 105, 107)] * 3   # 105 에서 익절가(106.5)가 2% 안 → 지정가
        clock = Clock(datetime(2026, 9, 1, 9, 0, tzinfo=ET))
        prev = day_bars(PREV, [(100, 100, 100, 100, 5000.0)] * 390)
        client = FakeLive({"AUSDT": prev + day_bars(DAY, first + fvg + after + up)}, clock)
        info = client.exchange_info()
        info["symbols"][0]["filters"].append({"filterType": "PERCENT_PRICE", "multiplierUp": "1.02", "multiplierDown": "0.98"})
        client.exchange_info = lambda: info
        cfg = BotConfig(top_n=1, state_dir=tempfile.mkdtemp(), dry_run=False, api_key="k", api_secret="s")
        st = FvgTrader(client, cfg, now_fn=clock.now, sleep_fn=clock.sleep).run(DAY)
        kinds = [o[0] for o in client.orders]
        self.assertEqual(kinds[:4], ["margin", "leverage", "LIMIT", "STOP"])
        self.assertEqual(client.orders[1], ("leverage", 20))   # 5배 두 종목 증거금용 설정
        self.assertIn(("LIMIT", "SELL", "106.5", "true"), client.orders)
        self.assertEqual(st.positions["AUSDT"]["result"]["reason"], "target")
        self.assertAlmostEqual(st.positions["AUSDT"]["qty"], round_step(2500 / 101.5, 0.001))


class HelperTest(unittest.TestCase):
    def test_days_and_rounding(self):
        self.assertEqual(prev_trading_day(date(2026, 9, 8)), date(2026, 9, 4))  # 9/7 노동절
        self.assertEqual(next_start(datetime(2026, 9, 5, 12, 0, tzinfo=ET), ""), datetime(2026, 9, 8, 9, 10, tzinfo=ET))
        now = datetime(2026, 9, 8, 10, 0, tzinfo=ET)
        self.assertEqual(next_start(now, ""), now)
        self.assertEqual(next_start(now, "2026-09-08").date(), date(2026, 9, 9))
        self.assertAlmostEqual(round_step(1.23456, 0.001), 1.234)
        self.assertAlmostEqual(round_tick(101.506, 0.01), 101.51)
        f = filters_of({"filters": [{"filterType": "PERCENT_PRICE", "multiplierUp": "1.0200", "multiplierDown": "0.9800"}]})
        self.assertAlmostEqual(f["pct_up"], 0.02)


if __name__ == "__main__":
    unittest.main()


class RetryTest(unittest.TestCase):
    def test_get_retries_on_408_but_post_does_not(self):
        from unittest import mock
        from binancebot.client import BinanceClient, BinanceError

        class R:
            def __init__(self, status, body):
                self.status_code, self._b, self.headers, self.text = status, body, {}, ""

            def json(self):
                return self._b

        busy = R(408, {"code": -1007, "msg": "Timeout waiting for response from backend server."})
        ok = R(200, [[1, "1", "1", "1", "1", "1", 2, "5"]])
        s = mock.Mock()
        s.request.side_effect = [busy, ok]
        c = BinanceClient(session=s)
        with mock.patch("binancebot.client.time.sleep"):
            self.assertEqual(len(c.klines("AUSDT", 0)), 1)
            s.request.side_effect = [busy, ok]
            with self.assertRaises(BinanceError):
                c._req("POST", "/fapi/v1/order", {"symbol": "AUSDT"})
