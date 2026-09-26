import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from tossbot.broker import Broker, round_down_to_tick, round_up_to_tick
from tossbot.client import TossClient
from tossbot.config import KST, Config
from tossbot.market_calendar import TradingDay
from tossbot.selector import Candidate, SelectionParams, score_candidates, select_stocks
from tossbot.state import StateStore
from tossbot.strategy import ClosingBetStrategy

TODAY = date(2026, 9, 22)


def candle(d, o, h, low, c, v):
    return {
        "timestamp": f"{d.isoformat()}T00:00:00+09:00",
        "openPrice": str(o), "highPrice": str(h), "lowPrice": str(low), "closePrice": str(c),
        "volume": str(v), "currency": "KRW",
    }


def history(today_bar, days=30, base=10_000.0):
    """과거 30일은 완만한 등락, 마지막은 오늘(장중) 봉. API 처럼 최신순으로 반환."""
    out, price = [], base
    steps = [1.004, 0.997, 1.003, 0.998]
    for i in range(days):
        d = TODAY - timedelta(days=days - i)
        price *= steps[i % 4]
        out.append(candle(d, price, price * 1.01, price * 0.99, price, 1_000_000))
    o, h, low, c, v = today_bar
    out.append(candle(TODAY, o, h, low, c, v))
    return list(reversed(out))


class FakeClient:
    def __init__(self, prices=None):
        self.prices = dict(prices or {})
        self.candles, self.stocks, self.rankings = {}, {}, []

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW"} for s in symbols if s in self.prices]

    def get_stocks(self, symbols):
        return [self.stocks[s] for s in symbols if s in self.stocks]

    def get_candles(self, symbol, interval="1d", count=60):
        return self.candles[symbol]

    def get_rankings(self, ranking_type, duration):
        return self.rankings

    def add(self, sym, change, amount, today_bar, **stock):
        self.rankings.append({
            "symbol": sym, "tradingAmount": str(amount), "tradingVolume": "0",
            "price": {"lastPrice": str(today_bar[3]), "basePrice": "10000", "changeRate": str(change)},
        })
        self.stocks[sym] = {"symbol": sym, "name": f"종목{sym}", "status": "ACTIVE", "securityType": "STOCK",
                            "isCommonShare": True, "koreanMarketDetail": {}, **stock}
        self.candles[sym] = history(today_bar)


class SelectorTest(unittest.TestCase):
    def test_closing_bet_filters(self):
        c = FakeClient()
        strong = (10_100, 10_700, 10_050, 10_650, 3_000_000)  # 양봉, 고가 근처 마감, 거래량 3배
        c.add("000001", 0.065, 20e9, strong)
        c.add("000002", 0.065, 30e9, (10_100, 10_700, 10_050, 10_600, 5_000_000))  # 더 강함
        c.add("000003", 0.065, 20e9, (10_100, 11_800, 10_050, 10_650, 3_000_000))  # 긴 윗꼬리
        c.add("000004", 0.065, 20e9, (10_100, 10_700, 10_050, 10_650, 1_200_000))  # 거래량 부족
        c.add("000005", 0.25, 20e9, strong)  # 등락률 과다 (상한가 근처)
        c.add("000006", 0.065, 5e9, strong)  # 거래대금 부족
        c.add("000007", 0.065, 20e9, strong, koreanMarketDetail={"krxTradingSuspended": True})  # 거래정지
        c.add("000008", 0.065, 20e9, (10_700, 10_750, 10_500, 10_650, 3_000_000))  # 음봉
        picks = select_stocks(c, set(), 10, SelectionParams(), TODAY, request_interval=0)
        self.assertEqual([p.symbol for p in picks], ["000002", "000001"])
        # 이미 보유 중인 종목은 제외, 빈 자리 수만큼만 선정
        self.assertEqual([p.symbol for p in select_stocks(c, {"000002"}, 1, SelectionParams(), TODAY, 0)], ["000001"])
        self.assertEqual(select_stocks(c, set(), 0, SelectionParams(), TODAY, 0), [])

    def test_no_candidates_means_zero(self):
        c = FakeClient()
        c.add("000001", 0.01, 20e9, (10_000, 10_100, 9_950, 10_050, 3_000_000))  # +1%: 조건 미달
        self.assertEqual(select_stocks(c, set(), 10, SelectionParams(), TODAY, 0), [])

    def test_score_ranking(self):
        a = Candidate("A", "A", 1, {"trading_amount": 2, "vol_ratio": 3, "close_to_high": 1.0, "breakout": 1.1})
        b = Candidate("B", "B", 1, {"trading_amount": 1, "vol_ratio": 2, "close_to_high": 0.98, "breakout": 1.0})
        self.assertEqual([x.symbol for x in score_candidates([b, a])], ["A", "B"])


class TickTest(unittest.TestCase):
    def test_round_to_tick(self):
        self.assertEqual(round_up_to_tick(1_999.2), 2_000)
        self.assertEqual(round_up_to_tick(12_341), 12_350)
        self.assertEqual(round_down_to_tick(9_530), 9_530)
        self.assertEqual(round_down_to_tick(71_234), 71_200)


def trading_day(d: date) -> TradingDay:
    t = datetime(d.year, d.month, d.day, tzinfo=KST)
    return TradingDay(d, True, d - timedelta(days=1), d + timedelta(days=1), t.replace(hour=9),
                      t.replace(hour=15, minute=20), t.replace(hour=15, minute=30))


MON, TUE, WED = trading_day(date(2026, 9, 21)), trading_day(date(2026, 9, 22)), trading_day(date(2026, 9, 23))


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(dry_run=True, state_dir=self.tmp.name)
        self.client = FakeClient({f"{i:06d}": 10_000 for i in range(1, 30)})
        self.picks_per_day = {}
        self.now = None

        def fake_select(client, exclude, top_n, params, today):
            syms = [s for s in self.picks_per_day.get(today, []) if s not in exclude][:top_n]
            return [Candidate(s, f"종목{s}", 10_000) for s in syms]

        self.strategy = ClosingBetStrategy(
            self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file), fake_select,
            clock=lambda: self.now,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, day, hh, mm):
        self.now = day.market_open.replace(hour=hh, minute=mm)
        self.strategy.tick(self.now, day)

    def test_buy_window_and_once_per_day(self):
        s = self.strategy
        self.picks_per_day[MON.today] = ["000001", "000002", "000003"]
        self.tick(MON, 15, 9)
        self.assertEqual(len(s.state.positions), 0)
        self.tick(MON, 15, 10)
        self.assertEqual(sorted(s.state.positions), ["000001", "000002", "000003"])
        p = s.state.positions["000001"]
        self.assertEqual(p.quantity, 9)  # 10,050원 지정가 → 9주
        self.tick(MON, 15, 15)  # 같은 날 재매수 없음
        self.assertEqual(len(s.state.positions), 3)

        # 15:20 이후(종가 단일가)에는 매수하지 않음
        self.picks_per_day[TUE.today] = ["000004"]
        self.tick(TUE, 15, 20)
        self.assertNotIn("000004", s.state.positions)

    def test_zero_picks(self):
        self.tick(MON, 15, 10)
        self.assertEqual(len(self.strategy.state.positions), 0)
        self.assertEqual(self.strategy.state.last_buy_date, "2026-09-21")

    def test_max_ten_positions_across_days(self):
        s = self.strategy
        self.picks_per_day[MON.today] = [f"{i:06d}" for i in range(1, 8)]  # 7종목
        self.tick(MON, 15, 10)
        self.picks_per_day[TUE.today] = [f"{i:06d}" for i in range(8, 20)]  # 12종목 후보
        self.tick(TUE, 15, 10)
        self.assertEqual(len(s.state.positions), 10)  # 빈 자리 3개만 채움
        self.assertIn("000010", s.state.positions)
        self.assertNotIn("000011", s.state.positions)

    def test_stop_market_and_take_profit_limit_via_conditional_orders(self):
        s = self.strategy
        self.picks_per_day[MON.today] = ["000001", "000002", "000003"]
        self.tick(MON, 15, 10)
        conds = s.broker._dry_conditionals
        p1 = s.state.positions["000001"]
        stop, tp = conds[p1.stop_co_id], conds[p1.tp_co_id]
        # 손절: 10,000 × 0.953 = 9,530원 감시 → 시장가 / 익절: 11,500원 감시 → 11,500원 지정가
        self.assertEqual((stop["triggerPrice"], stop["orderType"]), (9_530, "MARKET"))
        self.assertEqual((tp["triggerPrice"], tp["orderType"], tp["orderPrice"]), (11_500, "LIMIT", 11_500))
        tp_id = p1.tp_co_id

        # 다음 날: 000001 손절, 000002 익절, 000003 (-4.6%) 계속 보유
        self.client.prices.update({"000001": 9_500, "000002": 11_500, "000003": 9_540})
        self.tick(TUE, 9, 1)
        reasons = {h["symbol"]: h["reason"] for h in s.state.history}
        self.assertEqual(reasons, {"000001": "STOP_LOSS", "000002": "TAKE_PROFIT"})
        self.assertEqual(conds[tp_id]["status"], "EXPIRED")  # 손절 발동 → 익절 조건주문 취소
        self.assertEqual(list(s.state.positions), ["000003"])

        # 걸릴 때까지 보유: 며칠이 지나도 유지
        self.tick(WED, 15, 0)
        self.assertIn("000003", s.state.positions)

    def test_conditional_expired_is_rearmed(self):
        s = self.strategy
        self.picks_per_day[MON.today] = ["000001"]
        self.tick(MON, 15, 10)
        pos = s.state.positions["000001"]
        old = pos.stop_co_id
        s.broker._dry_conditionals[old]["status"] = "EXPIRED"
        self.tick(TUE, 10, 0)
        self.assertIsNotNone(pos.stop_co_id)
        self.assertNotEqual(pos.stop_co_id, old)

    def test_bot_polling_when_conditional_disabled(self):
        self.cfg.use_conditional_orders = False
        s = self.strategy
        self.picks_per_day[MON.today] = ["000001", "000002"]
        self.tick(MON, 15, 10)
        self.assertFalse(s.broker._dry_conditionals)
        self.client.prices.update({"000001": 9_520, "000002": 11_600})
        self.tick(TUE, 10, 0)
        result = {h["symbol"]: (h["reason"], h["exit_price"]) for h in s.state.history}
        self.assertEqual(result["000001"], ("STOP_LOSS", 9_520))
        self.assertEqual(result["000002"][0], "TAKE_PROFIT")

    def test_manual_liquidate_cancels_conditionals(self):
        s = self.strategy
        self.picks_per_day[MON.today] = ["000001", "000002"]
        self.tick(MON, 15, 10)
        s.liquidate_all("MANUAL")
        self.assertEqual(len(s.state.positions), 0)
        self.assertFalse([c for c in s.broker._dry_conditionals.values() if c["status"] == "WATCHING"])

    def test_state_persists_across_restart(self):
        self.picks_per_day[MON.today] = ["000001"]
        self.tick(MON, 15, 10)
        reloaded = ClosingBetStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        self.assertEqual(list(reloaded.state.positions), ["000001"])
        self.assertEqual(reloaded.state.last_buy_date, "2026-09-21")


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
