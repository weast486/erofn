import json
import tempfile
import time
import unittest
from pathlib import Path

from binancebot.mabot import MaBot, PaperExchange, Settings, base5, read_status, rule_summary, write_request


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

    def test_fvg_guard_closes_before_us_open(self):
        """바이낸스 FVG 봇이 켜져 있으면 정규장 10분 전(동부 9:20)에 자동 매매 멈춤·포지션 정리 (2026-11-27)."""
        from datetime import datetime
        from binancebot.backtest import ET
        write_request(self.dir, {"cmd": "long", "q": Q, "usdt": 50, "lev": 1, "pin": ""})
        self.bot.step()
        self.bot.step()
        self.bot.st["enabled"] = True
        self.assertEqual(self.bot.st["pos"]["dir"], 1)
        running = [True]
        self.bot.fvg_running = lambda: running[0]
        ts = lambda h, m: datetime(2026, 11, 30, h, m, tzinfo=ET).timestamp()   # 월요일
        self.bot.now = lambda: ts(9, 15)          # 아직 10분 전 아님
        self.bot.step()
        self.assertTrue(self.bot.st["enabled"])
        running[0] = False
        self.bot.now = lambda: ts(9, 21)          # FVG 봇 꺼져 있으면 그대로
        self.bot.step()
        self.assertTrue(self.bot.st["enabled"])
        running[0] = True
        self.bot.step()
        self.assertFalse(self.bot.st["enabled"])
        self.bot.step()
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "fvg")
        self.bot.st["enabled"] = True             # 그날 다시 켜면 다시 막지 않음 (하루 한 번)
        self.bot.step()
        self.assertTrue(self.bot.st["enabled"])

    def test_fvg_running_reads_lock(self):
        from kiwoombot.__main__ import acquire_lock
        d = Path(self.dir) / "fvg"
        self.bot.cfg.fvg_state_dir = str(d)
        self.assertFalse(self.bot.fvg_running())   # 잠금 파일 없음
        f = acquire_lock(str(d))
        try:
            import os
            if os.name == "nt":   # 윈도우 잠금은 같은 프로세스에서도 막힘
                self.assertTrue(self.bot.fvg_running())
        finally:
            f.close()
        self.assertFalse(self.bot.fvg_running())

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

    def test_max_loss_caps_stop(self):
        # 손절 없음(비상 -10%) + 최대 손실 2 USDT: 손절가를 2 USDT 잃는 가격으로 당기고, 닿으면 정리
        self.bot.cfg.max_loss_usdt = 2.0
        q = {**Q, "stop_mode": "pct", "sl": "0"}
        write_request(self.dir, {"cmd": "long", "q": q, "usdt": 50, "lev": 1, "pin": ""})
        self.bot.step()
        self.bot.step()
        ex = self.bot.st["exit"]
        pos = self.bot.st["pos"]
        self.assertEqual(ex["safety"], "maxloss")
        loss = pos["qty"] * (pos["entry"] - ex["stop"]) + pos["qty"] * pos["entry"] * 0.001
        self.assertAlmostEqual(loss, 2.0, delta=0.05)
        self.price = ex["stop"] - 0.01
        self.bot.step()
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "maxloss")

    def test_max_loss_keeps_tighter_rule_stop(self):
        self.bot.cfg.max_loss_usdt = 50.0                 # 규칙 손절(-2%)이 더 가까우면 그대로
        write_request(self.dir, {"cmd": "long", "q": Q, "usdt": 50, "lev": 1, "pin": ""})
        self.bot.step()
        self.bot.step()
        self.assertFalse(self.bot.st["exit"]["safety"])

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

    def test_opposite_line_take_profit(self):
        # 진입선 MA3(19, 아래) + 위에 있는 MA10(반대 방향 선): 18.9 에 롱 → 위 선까지 20% 안이면 그 선에서 익절
        self.market.closes = [22] * 8 + [16, 17, 18, 19, 20, 20]
        q = {**Q, "pb2_ma": "sma:10", "opp_tp": "20", "opp_block": "1"}
        write_request(self.dir, {"cmd": "start", "q": q, "usdt": 50, "lev": 2, "pin": ""})
        self.bot.step()
        self.assertEqual(self.bot.st["entry_orders"]["1"]["line"], 0)
        self.price = 18.9
        self.bot.step()
        self.assertEqual(self.bot.st["pos"]["opp"], 1)
        self.market.closes = self.market.closes + [19.2, 19.2]   # 다음 봉 (익절은 산 다음 봉부터)
        self.bot.step()
        self.assertAlmostEqual(self.bot.st["exit"]["tp"], 19.52)  # 그 봉의 MA10

    def test_stop_checked_on_5m_bars(self):
        # 차트는 1시간봉이어도 봇은 5분봉으로 계산: 1시간봉 3선은 진행 중인 1시간봉까지 반영((19+20+20)/3),
        # 손절선 종가 이탈은 5분봉이 끝날 때마다
        chart = mk_bars([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 20], sec=3600)
        five = {"closes": [20] * 6}

        def bars(symbol, interval, count):
            if interval == "5m":
                t0 = chart[-1]["t"]
                return [{"t": t0 + i * 300, "o": c, "h": c + 0.1, "l": c - 0.1, "c": c, "v": 1.0, "q": c}
                        for i, c in enumerate(five["closes"])][-count:]
            return chart[-count:]
        self.market.bars = bars
        q = {**Q, "interval": "1h", "stop_mode": "line", "stop_ma": "sma:3", "sl": "2"}
        write_request(self.dir, {"cmd": "start", "q": q, "usdt": 50, "lev": 2, "pin": ""})
        self.bot.step()
        self.assertAlmostEqual(self.bot.st["entry_orders"]["1"]["price"], 19.67)
        self.price = 18.9                                  # 1시간봉 3선에 닿아 롱
        self.bot.step()
        self.assertIsNotNone(self.bot.st["pos"])
        five["closes"] = [20] * 6 + [19.5, 19.5]
        self.bot.step()
        self.assertIsNotNone(self.bot.st["pos"])           # 아직 5분봉 종가 이탈 없음
        five["closes"] = [20] * 6 + [19.5, 19.5, 18.5, 18.6]   # 5분봉이 19.28(19.67 -2%) 아래로 마감
        self.price = 18.7
        self.bot.step()
        self.assertEqual(self.bot.st["pos"]["reason"], "line")
        self.bot.step()                                   # 다음 확인에서 정리 기록
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "line")

    def test_base5_keeps_chart_lines(self):
        q = base5({"interval": "4h", "pb_ma": "sma:200", "pb_tf": "", "stop_tf": "15m", "tf1": ""}, "4h")
        self.assertEqual((q["interval"], q["pb_tf"], q["stop_tf"], q["tf1"], q["ends5"]), ("5m", "4h", "15m", "4h", "1"))

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

    def test_rule_summary(self):
        q = {"side": "both", "pb_ma": "vwma:100", "pb_tf": "5m", "pb3_ma": "vwma:100", "pb3_tf": "15m",
             "pb2_kind": "off", "entry": "touch", "entry_pct": "0.2", "min_above": "5", "stop_mode": "line",
             "stop_ma": "same", "sl": "0.5", "tp_mode": "pct", "tp": "3", "max_hold": "10", "opp_tp": "3", "opp_block": "1"}
        rows = dict(rule_summary(q, "5m"))
        self.assertIn("5분봉 VWMA100 / 15분봉 VWMA100", rows["진입"])
        self.assertIn("진입한 선", rows["손절"])
        self.assertEqual(rows["익절"], "진입가 +3% 지정가")
        self.assertIn("50분", rows["최대 보유"])
        self.assertIn("반대 진입선", rows)

    def test_far_reverse_entry_market(self):
        # 숏 대기(가격이 3선 아래) 중 끝난 5분봉 종가가 3선에서 5% 넘게 아래 → 롱 시장가, 손절 -1% · 익절 +3%
        self.market.closes = [30, 29, 28, 27, 26, 25, 24, 23, 22, 21, 20, 14, 14]
        self.price = 14.0
        q = {**Q, "side": "short", "far_pct": "5", "far_sl": "1", "far_tp": "3"}
        write_request(self.dir, {"cmd": "start", "q": q, "usdt": 50, "lev": 2, "pin": ""})
        self.bot.step()
        self.assertIn("-1", self.bot.st["entry_orders"])  # 켠 순간의 진행 중인 봉은 원래 숏 지정가만
        self.bot.st["entry_from"] = 0                     # (넘어가는 봉이 끝난 뒤 켜져 있던 것처럼)
        self.bot.step()
        self.assertEqual(self.bot.st["entry_orders"], {})  # 숏 지정가 취소 → 롱 시장가
        self.bot.step()                                   # 다음 확인에서 보유 시작 (반대 진입)
        pos = self.bot.st["pos"]
        self.assertEqual((pos["dir"], pos.get("far")), (1, True))
        e = pos["entry"]
        self.assertAlmostEqual(self.bot.st["exit"]["stop"], round(e * 0.99, 2))
        self.assertAlmostEqual(self.bot.st["exit"]["tp"], round(e * 1.03, 2))
        self.bot.step()                                   # 같은 봉에서 다시 사지 않음
        self.price = 13.8
        self.bot.step()
        self.assertIsNone(self.bot.st["pos"])
        self.assertEqual(self.bot.st["trades"][-1]["reason"], "far_stop")

    def test_cross_not_allowed(self):
        self.start(q={**Q, "strat": "cross"})
        self.bot.step()
        self.assertIsNone(self.bot.st["cfg"])


if __name__ == "__main__":
    unittest.main()
