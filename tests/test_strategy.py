import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from tossbot.broker import Broker, round_down_to_tick, round_up_to_tick
from tossbot.client import TossClient
from tossbot.config import KST, Config
from tossbot.market_calendar import TradingDay
from tossbot.selector import Candidate, compute_metrics, parse_candles, passes_filters, score_candidates, select_stocks
from tossbot.state import StateStore
from tossbot.strategy import WeeklyStrategy


def make_candles(closes, volume=1_000_000, start=date(2026, 5, 1)):
    out = []
    for i, c in enumerate(closes):
        d = start + timedelta(days=i)
        out.append(
            {
                "timestamp": f"{d.isoformat()}T00:00:00+09:00",
                "openPrice": str(c),
                "highPrice": str(c * 1.01),
                "lowPrice": str(c * 0.99),
                "closePrice": str(c),
                "volume": str(volume),
                "currency": "KRW",
            }
        )
    return list(reversed(out))  # API 는 최신순일 수 있으므로 역순으로 준다


def zigzag(n, start=10_000.0):
    """등락을 반복하며 완만히 오르는 가격 (RSI 약 58)."""
    steps = [1.015, 0.99, 1.01, 0.992]
    out = [start]
    for i in range(n - 1):
        out.append(out[-1] * steps[i % 4])
    return out


class FakeClient:
    """토스증권 API 흉내. 주문은 Broker(dry_run=True)가 처리하므로 시세만 제공."""

    def __init__(self, prices):
        self.prices = dict(prices)
        self.candles = {}
        self.stocks = {}

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW"} for s in symbols if s in self.prices]

    def get_stocks(self, symbols):
        return [self.stocks[s] for s in symbols if s in self.stocks]

    def get_candles(self, symbol, interval="1d", count=120):
        return self.candles[symbol]


def trading_day(today: date, prev: date, nxt: date) -> TradingDay:
    t = datetime(today.year, today.month, today.day, tzinfo=KST)
    return TradingDay(
        today=today,
        is_open=True,
        previous_business_day=prev,
        next_business_day=nxt,
        market_open=t.replace(hour=9),
        closing_auction_start=t.replace(hour=15, minute=20),
        market_close=t.replace(hour=15, minute=30),
    )


TUE = trading_day(date(2026, 9, 15), date(2026, 9, 14), date(2026, 9, 16))
WED = trading_day(date(2026, 9, 16), date(2026, 9, 15), date(2026, 9, 17))
FRI = trading_day(date(2026, 9, 18), date(2026, 9, 17), date(2026, 9, 21))


class SelectorTest(unittest.TestCase):
    def test_uptrend_passes_downtrend_fails(self):
        up = zigzag(80)
        down = [20_000 * (0.997**i) for i in range(80)]
        m_up = compute_metrics(parse_candles(make_candles(up, volume=1_000_000)))
        m_down = compute_metrics(parse_candles(make_candles(down, volume=1_000_000)))
        self.assertIsNone(passes_filters(m_up, 100_000, 1e9, 15), m_up)
        self.assertEqual(passes_filters(m_down, 100_000, 1e9, 15), "정배열 아님")
        # 주가가 종목당 예산보다 비싸면 제외
        self.assertIsNotNone(passes_filters(m_up, 5_000, 1e9, 15))

    def test_score_ranking(self):
        a = Candidate("A", "A", 1, {"ret20": 0.2, "vol_ratio": 2.0, "high_prox": 1.0})
        b = Candidate("B", "B", 1, {"ret20": 0.1, "vol_ratio": 1.0, "high_prox": 0.9})
        self.assertEqual([c.symbol for c in score_candidates([b, a])], ["A", "B"])

    def test_select_excludes_today_bar_and_suspended(self):
        closes = zigzag(80)
        client = FakeClient({})
        for sym in ("000001", "000002"):
            client.candles[sym] = make_candles(closes)
            client.stocks[sym] = {
                "symbol": sym, "name": sym, "status": "ACTIVE", "securityType": "STOCK",
                "isCommonShare": True, "koreanMarketDetail": {"krxTradingSuspended": sym == "000002"},
            }
        picks = select_stocks(client, ["000001", "000002"], 10, 100_000, 1e9, 15, date(2026, 7, 30), 0)
        self.assertEqual([p.symbol for p in picks], ["000001"])


class TickTest(unittest.TestCase):
    def test_round_up_to_tick(self):
        self.assertEqual(round_up_to_tick(1_999.2), 2_000)
        self.assertEqual(round_up_to_tick(12_341), 12_350)
        self.assertEqual(round_up_to_tick(71_234), 71_300)
        self.assertEqual(round_down_to_tick(9_550), 9_550)
        self.assertEqual(round_down_to_tick(71_234), 71_200)


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "universe.txt").write_text("\n".join(f"{i:06d}" for i in range(1, 13)))
        self.cfg = Config(dry_run=True, universe_file=str(tmp / "universe.txt"), state_dir=str(tmp))
        self.client = FakeClient({f"{i:06d}": 10_000 for i in range(1, 13)})
        self.picked_universe = None

        def fake_select(client, universe, top_n, **kw):
            self.picked_universe = universe
            return [Candidate(s, f"종목{s}", 10_000) for s in universe[:top_n]]

        self.strategy = WeeklyStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file), fake_select)

    def tearDown(self):
        self.tmp.cleanup()

    def at(self, day, hh, mm):
        return day.market_open.replace(hour=hh, minute=mm)

    def test_full_week(self):
        s = self.strategy
        s.tick(self.at(TUE, 9, 5), TUE)  # 매수 시각(09:10) 전
        self.assertEqual(len(s.state.positions), 0)

        s.tick(self.at(TUE, 9, 10), TUE)
        self.assertEqual(len(s.state.positions), 10)
        for p in s.state.positions.values():
            # 10,000원 * 1.005 = 10,050원 지정가 → 9주 (종목당 10만원 이내)
            self.assertEqual(p.quantity, 9)
            self.assertLessEqual(p.quantity * 10_050, self.cfg.slot_budget)

        s.tick(self.at(TUE, 10, 0), TUE)  # 같은 주 재매수 없음
        self.assertEqual(len(s.state.history), 0)

        # 손절(-4.5% = 9,550원) / 익절(+15% = 11,500원)
        self.client.prices["000001"] = 9_550
        self.client.prices["000002"] = 11_500
        self.client.prices["000003"] = 9_560  # -4.4%: 유지
        s.tick(self.at(WED, 11, 0), WED)
        reasons = {h["symbol"]: h["reason"] for h in s.state.history}
        self.assertEqual(reasons, {"000001": "STOP_LOSS", "000002": "TAKE_PROFIT"})
        self.assertEqual(len(s.state.positions), 8)

        # 매수 슬롯이 비어도 같은 주에는 추가 매수하지 않음
        s.tick(self.at(WED, 11, 1), WED)
        self.assertEqual(len(s.state.positions), 8)

        # 금요일 15:09 에는 유지, 15:10(종가 단일가 10분 전) 전량 청산
        s.tick(self.at(FRI, 15, 9), FRI)
        self.assertEqual(len(s.state.positions), 8)
        s.tick(self.at(FRI, 15, 10), FRI)
        self.assertEqual(len(s.state.positions), 0)
        self.assertEqual(sum(h["reason"] == "WEEKLY_CLOSE" for h in s.state.history), 8)

    def test_conditional_orders_armed_and_cancelled(self):
        s = self.strategy
        s.tick(self.at(TUE, 9, 10), TUE)
        conds = s.broker._dry_conditionals
        p1 = s.state.positions["000001"]
        stop, tp = conds[p1.stop_co_id], conds[p1.tp_co_id]
        # 손절: 9,550원 감시 → 시장가 / 익절: 11,500원 감시 → 11,500원 지정가, 만료일은 그 주 금요일
        self.assertEqual((stop["triggerPrice"], stop["orderType"], stop["orderPrice"]), (9_550, "MARKET", None))
        self.assertEqual((tp["triggerPrice"], tp["orderType"], tp["orderPrice"]), (11_500, "LIMIT", 11_500))
        self.assertEqual(len(conds), 20)

        # 손절 발동 → 익절 조건주문은 봇이 취소
        stop_id, tp_id = p1.stop_co_id, p1.tp_co_id
        self.client.prices["000001"] = 9_500
        s.tick(self.at(WED, 10, 0), WED)
        self.assertNotIn("000001", s.state.positions)
        self.assertEqual(conds[stop_id]["status"], "ORDERED")
        self.assertEqual(conds[tp_id]["status"], "EXPIRED")  # 모의 취소 상태
        self.assertEqual(s.state.history[-1]["reason"], "STOP_LOSS")

        # 금요일 15:10: 남은 조건주문 전부 취소 후 시장가 매도
        s.tick(self.at(FRI, 15, 10), FRI)
        self.assertEqual(len(s.state.positions), 0)
        self.assertFalse([c for c in conds.values() if c["status"] == "WATCHING"])
        self.assertEqual(sum(h["reason"] == "WEEKLY_CLOSE" for h in s.state.history), 9)

    def test_bot_polling_when_conditional_disabled(self):
        self.cfg.use_conditional_orders = False
        s = self.strategy
        s.tick(self.at(TUE, 9, 10), TUE)
        self.assertFalse(s.broker._dry_conditionals)
        self.client.prices["000001"] = 9_540
        self.client.prices["000002"] = 11_600
        s.tick(self.at(WED, 10, 0), WED)
        reasons = {h["symbol"]: (h["reason"], h["exit_price"]) for h in s.state.history}
        # 손절은 시장가(현재가), 익절은 11,500원 지정가 이상으로 체결
        self.assertEqual(reasons["000001"], ("STOP_LOSS", 9_540))
        self.assertEqual(reasons["000002"][0], "TAKE_PROFIT")
        self.assertGreaterEqual(reasons["000002"][1], 11_500)

    def test_state_persists_across_restart(self):
        self.strategy.tick(self.at(TUE, 9, 10), TUE)
        reloaded = WeeklyStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        self.assertEqual(len(reloaded.state.positions), 10)
        self.assertEqual(reloaded.state.last_buy_week, "2026-W38")


if __name__ == "__main__":
    unittest.main()


class ClientConditionalOrderTest(unittest.TestCase):
    def test_request_shapes(self):
        calls = []

        class Resp:
            def __init__(self, code, body=None):
                self.status_code, self._body, self.headers = code, body, {}
                self.content = b"" if body is None else b"{}"
                self.text = ""

            def json(self):
                return self._body

        class Session:
            def post(self, url, data=None, timeout=None):
                return Resp(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})

            def request(self, method, url, params=None, json=None, headers=None, timeout=None):
                calls.append((method, url.split(".com")[1], json))
                if method == "DELETE":
                    return Resp(204)
                return Resp(200, {"result": {"conditionalOrderId": "co-1", "clientOrderId": None}})

        c = TossClient("id", "secret", account_seq=1, session=Session())
        c.create_conditional_order("005930", 3, "SELL", 68_000, "2026-09-25")
        c.create_conditional_order("005930", 3, "SELL", 82_000, "2026-09-25", order_type="LIMIT", order_price=82_000)
        self.assertIsNone(c.cancel_conditional_order("co-1"))
        self.assertEqual(calls[0][2]["first"], {"orderSide": "SELL", "triggerPrice": "68000"})
        self.assertEqual(calls[0][2]["orderType"], "MARKET")
        self.assertEqual(calls[0][2]["type"], "SINGLE")
        self.assertEqual(calls[1][2]["first"]["orderPrice"], "82000")
        self.assertEqual(calls[2][:2], ("DELETE", "/api/v1/conditional-orders/co-1"))
