import tempfile
import unittest
from datetime import date, datetime, timedelta

from tossbot.backtest import BacktestSettings, BreakoutSettings, run_breakout
from tossbot.breakout import BreakoutParams, evaluate, select_breakouts
from tossbot.broker import Broker
from tossbot.config import KST, Config
from tossbot.market_calendar import TradingDay
from tossbot.selector import Bar
from tossbot.state import StateStore
from tossbot.strategy import BreakoutStrategy, make_strategy, PullbackStrategy


def weekdays(n, start=date(2026, 7, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = weekdays(60)
TODAY = DAYS[45]


def history(closes, volume=2_000_000):
    return [Bar(d, c, c * 1.01, c * 0.99, c, volume) for d, c in zip(DAYS, closes)]


BASE = [10_000.0] * 45  # 45일 보합 (신고가 없음)


class EvaluateTest(unittest.TestCase):
    p = BreakoutParams()

    def ev(self, closes, price, volume=3_000_000):
        return evaluate("A", "A", history(closes), TODAY, price, volume, self.p)

    def test_first_20d_high_passes(self):
        cand, reason = self.ev(BASE, 10_800)  # 거래대금 10,800 x 300만 = 324억
        self.assertEqual(reason, "")
        self.assertAlmostEqual(cand.change, 0.08)

    def test_rejections(self):
        self.assertIn("신고가 아님", self.ev(BASE, 10_000)[1])
        recent_high = BASE[:40] + [10_500] + [10_300] * 4  # 5일 전 신고가
        self.assertIn("이미 신고가", self.ev(recent_high, 10_600)[1])
        self.assertIn("상한가", self.ev(BASE, 13_000)[1])
        self.assertIn("당일 거래대금", self.ev(BASE, 10_800, volume=1_000_000)[1])  # 108억
        pricey = [c * 20 for c in BASE]
        self.assertIn("100,000원 초과", self.ev(pricey, 216_000)[1])
        cheap = [c * 0.5 for c in BASE]
        self.assertEqual(self.ev(cheap, 5_400, volume=6_000_000)[1], "")  # 기본값: 가격 하한 없음
        floor = BreakoutParams(min_price=10_000)
        self.assertIn("10,000원 미만", evaluate("A", "A", history(cheap), TODAY, 5_400, 6_000_000, floor)[1])
        self.assertEqual(self.ev([c * 9 for c in BASE], 97_200, volume=500_000)[1], "")  # 9만7천원 (10만원 이하)

    def test_touched_limit_up_rejected(self):
        bars = history(BASE)
        # 장중 고가가 전일 종가 +29.5% 이상(12,950)이었다가 10,800 으로 내려옴
        self.assertIn("장중 상한가", evaluate("A", "A", bars, TODAY, 10_800, 3_000_000, self.p, 12_950)[1])
        self.assertEqual(evaluate("A", "A", bars, TODAY, 10_800, 3_000_000, self.p, 12_900)[1], "")
        off = BreakoutParams(skip_touched_limit_up=False)
        self.assertEqual(evaluate("A", "A", bars, TODAY, 10_800, 3_000_000, off, 12_950)[1], "")

    def test_live_and_backtest_agree(self):
        """같은 일봉이면 실전 판정과 백테스트 매수가 일치해야 한다."""
        cases = [BASE + [10_800], BASE[:40] + [10_500] + [10_300] * 4 + [10_600], BASE + [10_000]]
        for closes in cases:
            bars = history(closes, 3_000_000)
            live, _ = evaluate("A", "A", bars, TODAY, closes[-1], 3_000_000, self.p)
            b = BreakoutSettings(stop_loss_pct=4.7, take_profit_pct=20.0, exit_on_low=False,
                                 first_in_days=20, min_day_amount=2e10)
            bt = run_breakout({"000010": ("A", bars)}, TODAY, TODAY, BacktestSettings(), b)
            self.assertEqual(live is not None, len(bt.trades) == 1, closes[-3:])

    def test_min_volume_ratio(self):
        """신호일 거래량이 전일의 N배 미만이면 백테스트에서 매수하지 않는다."""
        bars = history(BASE + [10_800], 2_000_000)
        bars[-1] = Bar(TODAY, 10_800, 10_900, 10_700, 10_800, 3_000_000)  # 전일 대비 1.5배
        run = lambda r: run_breakout({"000010": ("A", bars)}, TODAY, TODAY, BacktestSettings(),  # noqa: E731
                                     BreakoutSettings(first_in_days=20, min_day_amount=2e10, min_volume_ratio=r))
        self.assertEqual(len(run(0).trades), 1)
        self.assertEqual(len(run(1.5).trades), 1)
        self.assertEqual(len(run(2).trades), 0)


class FakeClient:
    def __init__(self):
        self.prices = {"000010": 10_800}
        self.bars = history(BASE + [10_800], 3_000_000)
        self.kospi_prev_close, self.kospi_now = 3_000.0, 3_010.0  # 기본: 코스피 상승 중
        self.selects = 0

    def get_indicator_candles(self, symbol, interval="1d", count=5):
        d = DAYS[0]
        return [{"timestamp": f"{d.isoformat()}T00:00:00+09:00", "openPrice": "1", "highPrice": "1",
                 "lowPrice": "1", "closePrice": str(self.kospi_prev_close), "volume": "0", "currency": "KRW"}]

    def get_indicator_prices(self, symbols):
        return [{"symbol": "KOSPI", "lastPrice": str(self.kospi_now)}]

    def get_rankings(self, ranking_type, duration):
        return [{"symbol": "000010", "tradingAmount": "32400000000", "price": {"lastPrice": "10800"}}]

    def get_stocks(self, symbols):
        return [{"symbol": s, "name": "테스트", "status": "ACTIVE", "securityType": "STOCK",
                 "isCommonShare": True, "market": "KOSDAQ", "koreanMarketDetail": {}} for s in symbols]

    def get_candles(self, symbol, interval="1d", count=60):
        return [{"timestamp": f"{b.day.isoformat()}T00:00:00+09:00", "openPrice": str(b.open),
                 "highPrice": str(b.high), "lowPrice": str(b.low), "closePrice": str(b.close),
                 "volume": str(b.volume), "currency": "KRW"} for b in reversed(self.bars)]

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW"} for s in symbols if s in self.prices]


def trading_day(d):
    t = datetime(d.year, d.month, d.day, tzinfo=KST)
    return TradingDay(d, True, d - timedelta(days=1), d + timedelta(days=1), t.replace(hour=9),
                      t.replace(hour=15, minute=20), t.replace(hour=15, minute=30))


class BreakoutStrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(dry_run=True, state_dir=self.tmp.name)  # 기본값 = 신고가 전략
        self.client = FakeClient()
        self.s = make_strategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, d, hh, mm):
        day = trading_day(d)
        self.s.tick(day.market_open.replace(hour=hh, minute=mm), day)

    def test_default_is_breakout(self):
        self.assertIsInstance(self.s, BreakoutStrategy)
        self.assertEqual((self.cfg.stop_loss_pct, self.cfg.take_profit_pct, self.cfg.max_hold_days), (4.7, 20.0, 0))
        self.assertIsInstance(make_strategy(Config(strategy="pullback", state_dir=self.tmp.name),
                                            Broker(self.client), StateStore(self.cfg.state_file)), PullbackStrategy)

    def test_order_budget_is_pct_of_bot_equity(self):
        self.assertEqual(self.s.order_budget(), 100_000)  # 100만원 x 10%
        # 실현 이익 +20만원, 보유 종목 평가이익 +3만원 → 평가금액 123만원 → 종목당 12.3만원
        self.s.state.history.append({"symbol": "000020", "entry_price": 10_000, "exit_price": 12_000, "quantity": 100})
        from tossbot.state import Position
        self.s.state.positions["000010"] = Position(symbol="000010", name="A", quantity=10, entry_price=7_800)
        self.assertEqual(self.s.order_budget(), 123_000)
        fixed = make_strategy(Config(dry_run=True, state_dir=self.tmp.name, position_pct=0),
                              Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        self.assertEqual(fixed.order_budget(), 100_000)  # POSITION_PCT=0 → TOTAL_BUDGET / NUM_STOCKS

    def test_buys_at_1510_once_with_conditional_orders(self):
        self.tick(TODAY, 15, 9)
        self.assertEqual(self.s.state.positions, {})
        self.tick(TODAY, 15, 10)
        pos = self.s.state.positions["000010"]
        self.assertEqual(pos.quantity, 100_000 // 10_860)  # 10,800 x 1.005 → 10,860원 지정가
        conds = self.s.broker._dry_conditionals
        stop, tp = conds[pos.stop_co_id], conds[pos.tp_co_id]
        self.assertEqual((stop["triggerPrice"], stop["orderType"]), (10_290, "MARKET"))  # 10,800 x 0.953 = 10,292 → 10원 단위 내림
        self.assertEqual((tp["triggerPrice"], tp["orderType"], tp["orderPrice"]), (12_960, "LIMIT", 12_960))
        self.assertEqual(self.s.state.last_buy_date, TODAY.isoformat())
        del self.s.state.positions["000010"]
        self.tick(TODAY, 15, 15)  # 같은 날 재매수 없음
        self.assertEqual(self.s.state.positions, {})

    def test_no_buy_while_kospi_below_prev_close(self):
        self.cfg.market_filter = "kospi_not_down"
        self.client.kospi_now = 2_990  # 코스피 하락 중 → 매수 금지
        self.tick(TODAY, 15, 10)
        self.assertEqual(self.s.state.positions, {})
        self.assertIsNone(self.s.state.last_buy_date)  # 오늘 매수 기회를 소진하지 않음
        self.client.kospi_now = 3_000  # 15:20 전에 전일 종가 회복 → 매수
        self.tick(TODAY, 15, 15)
        self.assertIn("000010", self.s.state.positions)

    def test_market_filter_off(self):
        self.assertEqual(self.cfg.market_filter, "none")  # 기본값: 시장 필터 없음
        self.client.kospi_now = 2_900
        self.tick(TODAY, 15, 10)
        self.assertIn("000010", self.s.state.positions)

    def test_select_breakouts(self):
        cands = select_breakouts(self.client, set(), 10, BreakoutParams(), TODAY, request_interval=0)
        self.assertEqual([c.symbol for c in cands], ["000010"])
        self.assertEqual(select_breakouts(self.client, {"000010"}, 10, BreakoutParams(), TODAY, 0), [])


if __name__ == "__main__":
    unittest.main()
