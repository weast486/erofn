import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from tossbot.config import KST
from kiwoombot.client import num
from kiwoombot.config import KiwoomConfig
from kiwoombot.daytrade import DayTrader, today_bars


def daily_rows(closes, ymd_last="20261001", amount_vol=1_000_000):
    """최신순 일봉. closes 는 오래된 → 최근 순."""
    d0 = datetime.strptime(ymd_last, "%Y%m%d")
    rows = []
    for i, c in enumerate(reversed(closes)):
        rows.append({"dt": (d0 - timedelta(days=i)).strftime("%Y%m%d"), "cur_prc": f"+{c}", "trde_qty": str(amount_vol)})
    return rows


class FakeClient:
    """주문을 기록하고, 정해 둔 분봉을 시각에 맞춰 돌려준다 (실주문 모드 흉내)."""

    def __init__(self, today="20261002"):
        self.today = today
        self.minute = {}  # code -> list of (hhmm, o, h, l, c)
        self.now = None
        self.orders = []  # (kind, code, qty, price, ord_no)
        self.open_orders = {}  # ord_no -> dict
        self.held = {}
        self.cancels = []
        self.cash = 10_000_000

    def balance(self):
        return {"prsm_dpst_aset_amt": "1000000"}

    def change_rate_ranking(self, max_pages=5):
        return [{"stk_cd": "111110", "stk_nm": "급등A", "flu_rt": "+20.5"},
                {"stk_cd": "222220", "stk_nm": "급등B", "flu_rt": "+16.0"},
                {"stk_cd": "333330", "stk_nm": "약함", "flu_rt": "+9.0"}]

    def daily_chart(self, code, base_dt):
        if code == "111110":
            return daily_rows([9900] * 21 + [12000], "20261001", 1_000_000)  # +21%, 평균 100억
        if code == "222220":
            return daily_rows([10000] * 21 + [11600], "20261001", 100)  # 거래대금 부족
        return daily_rows([10000] * 22)

    def minute_chart(self, code):
        hm = self.now.strftime("%H%M")
        rows = [r for r in self.minute.get(code, []) if r[0] <= hm]
        return [{"cntr_tm": f"{self.today}{t}00", "open_pric": o, "high_pric": h, "low_pric": l, "cur_prc": c,
                 "trde_qty": "100"} for t, o, h, l, c in reversed(rows)]

    def _order(self, kind, code, qty, price):
        no = str(len(self.orders) + 1)
        self.orders.append((kind, code, qty, price, no))
        return no

    def buy(self, code, qty, price=None):
        no = self._order("buy", code, qty, price)
        self.held[code] = {"qty": qty, "avg_price": float(price)}  # 즉시 전량 체결로 흉내
        return no

    def sell(self, code, qty, price=None):
        no = self._order("sell", code, qty, price)
        if price:  # 익절 지정가는 미체결로 남김
            self.open_orders[no] = {"ord_no": no, "oso_qty": str(qty)}
        else:
            self.held.pop(code, None)
        return no

    def cancel(self, code, order_no):
        self.cancels.append(order_no)
        self.open_orders.pop(order_no, None)
        return "c" + order_no

    def orderable_cash(self):
        return self.cash

    def unfilled(self):
        return list(self.open_orders.values())

    def holdings(self):
        return dict(self.held)


class KiwoomDayTradeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = KiwoomConfig(app_key="x", secret_key="y", mock=True, dry_run=False, state_dir=self.tmp.name,
                                swing_state_file="")
        self.client = FakeClient()
        self.clock = [datetime(2026, 10, 2, 8, 50, tzinfo=KST)]
        self.client.now = self.clock[0]
        self.trader = DayTrader(self.client, self.cfg, now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, hh, mm):
        self.clock[0] = datetime(2026, 10, 2, hh, mm, tzinfo=KST)
        self.client.now = self.clock[0]

    def test_num_strips_sign_and_commas(self):
        self.assertEqual(num("+12,300"), 12300)
        self.assertEqual(num("-500"), 500)
        self.assertEqual(num(""), 0)

    def test_candidates_filtered_by_daily(self):
        self.trader.prepare()
        codes = [c.code for c in self.trader.state.candidates]
        self.assertEqual(codes, ["111110"])
        self.assertEqual(self.trader.state.candidates[0].prev_close, 12000)

    def test_swing_holdings_excluded(self):
        p = Path(self.tmp.name) / "swing.json"
        p.write_text(json.dumps({"positions": {"111110": {}}}), encoding="utf-8")
        self.cfg.swing_state_file = str(p)
        self.trader.prepare()
        self.assertEqual(self.trader.state.candidates, [])

    def test_break_prev_close_buy_take_profit_order_then_stop(self):
        self.trader.prepare()
        # 시가 11,800 (전일 종가 12,000 아래) → 09:01 고가 12,050 으로 돌파
        self.client.minute["111110"] = [("0900", "11800", "11900", "11700", "11850"),
                                        ("0901", "11850", "12050", "11850", "12040")]
        self.at(9, 0)
        self.trader.step()
        self.assertEqual(self.client.orders, [])
        self.at(9, 1)
        self.trader.step()
        kind, code, qty, price, _ = self.client.orders[0]
        self.assertEqual((kind, code), ("buy", "111110"))
        self.assertLessEqual(price, 12000 * 1.015 + 10)
        self.assertEqual(qty, int(1_000_000 * self.cfg.position_pct / 100 // price))
        # 다음 확인에서 체결 → 익절 지정가 +7%
        self.at(9, 1)
        self.trader.step()
        t = self.trader.state.trades[0]
        self.assertEqual(t.status, "open")
        self.assertEqual(self.client.orders[1][0], "sell")
        self.assertGreaterEqual(self.client.orders[1][3], price * 1.07)
        # 손절가 아래로 → 익절 주문 취소 + 시장가 매도
        self.client.minute["111110"].append(("0930", "11000", "11000", "10500", "10600"))
        self.at(9, 30)
        self.trader.step()
        self.assertIn(self.client.orders[1][4], self.client.cancels)
        self.assertEqual(self.client.orders[-1][:4], ("sell", "111110", qty, None))
        self.at(9, 31)
        self.trader.step()
        self.assertEqual(t.status, "closed")
        self.assertEqual(t.exit_reason, "STOP_LOSS")

    def test_gap_up_skipped_and_exit_time(self):
        self.trader.prepare()
        self.client.minute["111110"] = [("0900", "12100", "12300", "12000", "12200")]
        self.at(9, 0)
        self.trader.step()
        self.assertEqual(self.trader.state.candidates[0].status, "skip_gap")
        self.assertEqual(self.client.orders, [])

    def test_time_exit_sells_and_cancels_take_profit(self):
        self.trader.prepare()
        self.client.minute["111110"] = [("0900", "11800", "11900", "11700", "11850"),
                                        ("0901", "11850", "12050", "11850", "12040")]
        self.at(9, 1)
        self.trader.step()
        self.trader.step()
        tp_no = self.client.orders[1][4]
        self.at(12, 0)
        self.trader.step()
        self.assertIn(tp_no, self.client.cancels)
        self.assertIsNone(self.client.orders[-1][3])  # 시장가 매도
        self.trader.step()
        self.assertEqual(self.trader.state.trades[0].exit_reason, "TIME_EXIT")
        self.assertEqual(self.trader.state.trades[0].status, "closed")

    def test_today_bars_filters_and_sorts(self):
        rows = [{"cntr_tm": "20261002090100", "open_pric": "+2", "high_pric": "3", "low_pric": "1", "cur_prc": "2",
                 "trde_qty": "1"},
                {"cntr_tm": "20261002090000", "open_pric": "1", "high_pric": "2", "low_pric": "1", "cur_prc": "2",
                 "trde_qty": "1"},
                {"cntr_tm": "20261001153000", "open_pric": "9", "high_pric": "9", "low_pric": "9", "cur_prc": "9",
                 "trde_qty": "1"}]
        b = today_bars(rows, "20261002")
        self.assertEqual([x["t"] for x in b], ["0900", "0901"])

    def test_dry_run_sends_no_orders(self):
        self.cfg.dry_run = True
        self.trader.prepare()
        self.client.minute["111110"] = [("0900", "11800", "11900", "11700", "11850"),
                                        ("0901", "11850", "12050", "11850", "12040"),
                                        ("0910", "12100", "13200", "12100", "13100")]
        self.at(9, 1)
        self.trader.step()
        self.trader.step()  # 체결 처리
        self.at(9, 10)
        self.trader.step()  # 익절가 도달
        self.assertEqual(self.client.orders, [])
        t = self.trader.state.trades[0]
        self.assertEqual((t.status, t.exit_reason), ("closed", "TAKE_PROFIT"))


class PaddedOrderNoClient(FakeClient):
    """미체결 목록의 주문번호만 7자리로 0 을 채워 돌려준다 (주문 응답과 자릿수가 다른 경우)."""

    def unfilled(self):
        return [{**o, "ord_no": str(o["ord_no"]).zfill(7)} for o in self.open_orders.values()]


class KiwoomOrderNoPaddingTest(unittest.TestCase):
    def test_stop_cancels_take_profit_when_order_no_padded(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = KiwoomConfig(app_key="x", secret_key="y", mock=True, dry_run=False, state_dir=tmp.name, swing_state_file="")
        client = PaddedOrderNoClient()
        clock = [datetime(2026, 10, 2, 8, 50, tzinfo=KST)]
        client.now = clock[0]
        trader = DayTrader(client, cfg, now_fn=lambda: clock[0], sleep_fn=lambda s: None)

        def at(hh, mm):
            clock[0] = datetime(2026, 10, 2, hh, mm, tzinfo=KST)
            client.now = clock[0]

        trader.prepare()
        client.minute["111110"] = [("0900", "11800", "11900", "11700", "11850"),
                                   ("0901", "11850", "12050", "11850", "12040")]
        at(9, 1)
        trader.step()
        trader.step()
        tp_no = client.orders[1][4]
        client.minute["111110"].append(("0930", "11000", "11000", "10500", "10600"))
        at(9, 30)
        trader.step()
        self.assertIn(tp_no, client.cancels)  # 원래 주문번호 그대로 취소
        self.assertIsNone(client.orders[-1][3])  # 시장가 매도


class KiwoomCashCapTest(KiwoomDayTradeTest):
    def test_buy_qty_capped_by_orderable_cash(self):
        self.client.cash = 50_000
        self.trader.prepare()
        self.client.minute["111110"] = [("0900", "11800", "11900", "11700", "11850"),
                                        ("0901", "11850", "12050", "11850", "12040")]
        self.at(9, 1)
        self.trader.step()
        _, _, qty, price, _ = self.client.orders[0]
        self.assertEqual(qty, 50_000 // price)
