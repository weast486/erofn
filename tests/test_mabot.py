import json
import tempfile
import time
import unittest
from pathlib import Path

from binancebot.mabot import MaBot, PaperExchange, Settings, read_status, write_request


def mk_bars(closes, sec=300):
    t0 = 1_700_000_000 - 1_700_000_000 % sec
    return [{"t": t0 + i * sec, "o": c, "h": c + 0.2, "l": c - 0.2, "c": c, "v": 10.0, "q": 10.0 * c}
            for i, c in enumerate(closes)]


class FakeMarket:
    def __init__(self, closes):
        self.closes = closes

    def bars(self, symbol, interval, count):
        return mk_bars(self.closes)[-count:]


FILTERS = {"tick": 0.01, "step": 0.001, "min_notional": 5.0}
Q = {"strat": "pullback", "symbol": "TESTUSDT", "interval": "5m", "bars": "100", "pb_ma": "sma:3",
     "trend_ma": "sma:3", "stop_ma": "sma:3", "tp_ma": "sma:3", "exit_ma": "off", "min_above": "3",
     "entry": "touch", "entry_pct": "0", "stop_mode": "pct", "sl": "2", "tp_mode": "pct", "tp": "0",
     "side": "long", "trend": "none", "max_hold": "0", "fee": "0.05", "lev": "1"}


class MaBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.price = 20.0
        closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 20]  # 오르는 중 → 3선 위
        self.market = FakeMarket(closes)
        self.settings = Settings(dry_run=True, pin="", max_usdt=100, max_lev=5, state_dir=self.dir)

        def make(symbol):
            return PaperExchange(symbol, lambda: self.price, FILTERS, Path(self.dir) / "paper.json", 1000.0)
        self.bot = MaBot(self.settings, market=self.market, make_exchange=make)

    def tearDown(self):
        self.tmp.cleanup()

    def start(self, **kw):
        write_request(self.dir, {"cmd": "start", "q": Q, "usdt": 50, "lev": 2, "pin": "", **kw})

    def test_limit_entry_stop_and_record(self):
        self.start()
        self.bot.step()
        orders = self.bot.st["entry_orders"]
        self.assertIn("1", orders)                       # 롱 진입 지정가 = 직전 끝난 봉의 3선 (18+19+20)/3 = 19
        self.assertAlmostEqual(orders["1"]["price"], 19.0)
        self.assertAlmostEqual(orders["1"]["qty"], round(100 / 19.0, 3), places=3)  # 50 USDT x 2배
        self.price = 18.9                                  # 선에 닿음 → 체결
        self.bot.step()
        self.assertIsNotNone(self.bot.st["pos"])
        self.assertAlmostEqual(self.bot.st["exit"]["stop"], round(18.9 * 0.98, 2))  # 지정가 19 보다 싼 18.9 에 체결
        self.price = 18.0                                  # -2% 손절가 아래
        self.bot.step()
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "stop")
        self.assertTrue((Path(self.dir) / "trades.csv").exists())
        st = read_status(self.dir)
        self.assertTrue(st["alive"])
        self.assertEqual(st["cfg"]["symbol"], "TESTUSDT")

    def test_once_stops_after_one_trade(self):
        self.start(once=True)
        self.bot.step()
        self.assertTrue(self.bot.st["once"])
        self.price = 18.9                                  # 진입
        self.bot.step()
        self.assertIsNotNone(self.bot.st["pos"])
        self.price = 18.0                                  # 손절로 정리 → 1회 끝, 멈춤
        self.bot.step()
        self.assertFalse(self.bot.st["enabled"])
        self.assertFalse(self.bot.st["once"])
        self.assertIn("1회 매매 끝", self.bot.st["msgs"][-1]["text"])
        self.price = 20.0
        self.bot.step()
        self.assertEqual(self.bot.st["entry_orders"], {})  # 다시 진입 대기 안 함

    def test_limits_and_pin(self):
        self.start(usdt=500)                              # 상한 100 넘음
        self.bot.step()
        self.assertIsNone(self.bot.st["cfg"])
        self.assertIn("상한", self.bot.st["msgs"][-1]["text"])
        self.bot.cfg.pin = "1234"
        self.start(pin="0000")
        self.bot.step()
        self.assertIsNone(self.bot.st["cfg"])
        self.assertIn("PIN", self.bot.st["msgs"][-1]["text"])
        self.start(pin="1234")
        self.bot.step()
        self.assertTrue(self.bot.st["enabled"])

    def test_manual_long_then_close(self):
        write_request(self.dir, {"cmd": "long", "q": Q, "usdt": 50, "lev": 1, "pin": ""})
        self.bot.step()
        self.bot.step()                                   # 다음 확인에서 보유로 인식, 손절 걸림
        self.assertEqual(self.bot.st["pos"]["dir"], 1)
        self.assertIsNotNone(self.bot.st["exit"]["stop"])
        self.assertFalse(self.bot.st["enabled"])         # 수동 주문은 자동 매매를 켜지 않음
        write_request(self.dir, {"cmd": "close"})
        self.bot.step()
        self.bot.step()
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "manual")

    def test_safety_stop_when_rule_has_none(self):
        q = {**Q, "stop_mode": "pct", "sl": "0"}          # 규칙 손절 없음 → 비상 손절 -10%
        write_request(self.dir, {"cmd": "long", "q": q, "usdt": 50, "lev": 1, "pin": ""})
        self.bot.step()
        self.bot.step()
        ex = self.bot.st["exit"]
        self.assertTrue(ex["safety"])
        entry = self.bot.st["pos"]["entry"]
        self.assertAlmostEqual(ex["stop"], round(entry * 0.9, 2))
        self.price = entry * 0.85
        self.bot.step()
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "safety")

    def test_multi_lines_stop_on_entered_line(self):
        # 진입선 MA6(17.5) + MA3(19): 먼저 닿는 MA3 에 지정가, 손절은 진입한 MA3 닿음 -2%
        q = {**Q, "pb_ma": "sma:6", "pb2_ma": "sma:3", "stop_mode": "touch", "stop_ma": "same", "sl": "2"}
        write_request(self.dir, {"cmd": "start", "q": q, "usdt": 50, "lev": 2, "pin": ""})
        self.bot.step()
        o = self.bot.st["entry_orders"]["1"]
        self.assertAlmostEqual(o["price"], 19.0)
        self.assertEqual(o["line"], 1)
        self.price = 18.9
        self.bot.step()
        self.assertEqual(self.bot.st["pos"]["line"], 1)
        self.assertAlmostEqual(self.bot.st["exit"]["stop"], round(19.0 * 0.98, 2))

    def test_exchange_lev_only_changes_margin(self):
        # 거래소 배율 5: 거래소에는 5배로 설정, 주문 크기는 그대로 투입금 x 화면 배율(50 x 2 = 100 USDT)
        self.bot.cfg.exchange_lev = 5
        self.start()
        self.bot.step()
        self.assertEqual(self.bot.st["lev_set"], "TESTUSDT:5")
        self.assertAlmostEqual(self.bot.st["entry_orders"]["1"]["qty"], round(100 / 19.0, 3), places=3)

    def test_symbol_change_cancels_old_entry_orders(self):
        self.start()
        self.bot.step()
        old_ex = self.bot.ex
        cid = self.bot.st["entry_orders"]["1"]["cid"]
        self.bot.exchange("OTHERUSDT")
        self.assertEqual(self.bot.st["entry_orders"], {})
        self.assertNotIn(cid, [o.get("cid") for o in old_ex.s.get("orders", [])])

    def test_start_ignores_sim_entry_before_start(self):
        # 켜기 전 봉에서 모의 매매가 이미 들어갔어도(저가가 선에 닿은 봉), 켠 뒤에는 그걸 따라 기다리지 않고 바로 지정가
        closes = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 20]
        bars = mk_bars(closes)
        bars[-2]["l"] = 17.9                               # 앞 봉에서 3선(18) 닿음 → 모의 매매 롱 진입
        self.market.bars = lambda s, i, c: bars[-c:]
        self.start()
        self.bot.st["entry_from"] = bars[-1]["t"]          # 진행 중인 봉에서 켬
        self.bot.step()
        self.assertIn("1", self.bot.st["entry_orders"])
        self.bot.st["entry_from"] = 0                      # 예전 방식이면 모의 포지션 때문에 주문 없음
        self.bot.st["entry_orders"] = {}
        self.bot.step()
        self.assertEqual(self.bot.st["entry_orders"], {})

    def test_old_request_ignored(self):
        d = Path(self.dir) / "req"
        (d / "0_old.json").write_text(json.dumps({"cmd": "long", "q": Q, "usdt": 50, "lev": 1,
                                                  "time": time.time() - 3600}), encoding="utf-8")
        self.bot.step()
        self.assertIsNone(self.bot.st["cfg"])

    def test_cross_not_allowed(self):
        self.start(q={**Q, "strat": "cross"})
        self.bot.step()
        self.assertIsNone(self.bot.st["cfg"])


if __name__ == "__main__":
    unittest.main()
