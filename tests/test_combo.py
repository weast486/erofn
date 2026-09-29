import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from tossbot.breakout import BreakoutCandidate
from tossbot.broker import Broker
from tossbot.config import KST, Config
from tossbot.market_calendar import TradingDay
from tossbot.rsi import RsiParams, rsi_series, select_rsi, today_rsi
from tossbot.selector import parse_candles
from tossbot.state import StateStore
from tossbot.strategy import ComboStrategy, make_strategy

MON, TUE = date(2026, 9, 21), date(2026, 9, 22)
FALLING = [20_000 - 150 * i for i in range(60)]  # 꾸준히 하락 → RSI 0 근처
RISING = [10_000 + 100 * i for i in range(60)]  # 꾸준히 상승 → RSI 100 근처


def candles(closes, today, today_price, volume=1_000_000):
    """closes: 오늘 전까지의 종가 (마지막이 어제). API 처럼 최신순, 오늘 봉 포함."""
    days = [today - timedelta(days=len(closes) - i) for i in range(len(closes))] + [today]
    out = []
    for d, c in zip(days, closes + [today_price]):
        out.append({"timestamp": f"{d.isoformat()}T00:00:00+09:00", "openPrice": str(c), "highPrice": str(c),
                    "lowPrice": str(c), "closePrice": str(c), "volume": str(volume), "currency": "KRW"})
    return list(reversed(out))


class FakeClient:
    def __init__(self):
        self.closes = {"000100": list(FALLING), "000200": list(RISING), "000300": list(FALLING)}
        self.prices = {"000100": 11_000, "000200": 16_000, "000300": 11_100, "000010": 10_800}
        self.today = MON

    def get_stocks(self, symbols):
        return [{"symbol": s, "name": f"종목{s}", "status": "ACTIVE", "securityType": "STOCK",
                 "isCommonShare": True, "market": "KOSPI", "koreanMarketDetail": {}} for s in symbols]

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW"} for s in symbols if s in self.prices]

    def get_candles(self, symbol, interval="1d", count=200):
        return candles(self.closes[symbol], self.today, self.prices[symbol])


def trading_day(d):
    t = datetime(d.year, d.month, d.day, tzinfo=KST)
    return TradingDay(d, True, d - timedelta(days=1), d + timedelta(days=1), t.replace(hour=9),
                      t.replace(hour=15, minute=20), t.replace(hour=15, minute=30))


class RsiTest(unittest.TestCase):
    def test_rsi_extremes(self):
        self.assertEqual(rsi_series(RISING, 14)[-1], 100.0)
        self.assertLess(rsi_series(FALLING, 14)[-1], 1)
        self.assertIsNone(rsi_series([1, 2, 3], 14)[-1])

    def test_today_rsi_uses_current_price(self):
        bars = parse_candles(candles(FALLING, MON, 30_000))
        self.assertGreater(today_rsi(bars, MON, 30_000, 14), 50)  # 오늘 급반등
        self.assertLess(today_rsi(bars, MON, FALLING[-1] - 150, 14), 30)

    def test_select_rsi(self):
        c = FakeClient()
        universe = {"000100": "A", "000200": "B", "000300": "C"}
        cands = select_rsi(c, universe, set(), 10, RsiParams(), MON, request_interval=0)
        self.assertEqual([x.symbol for x in cands], ["000100", "000300"])  # 상승 중인 000200 제외, RSI 낮은 순
        self.assertEqual([x.symbol for x in select_rsi(c, universe, {"000100"}, 10, RsiParams(), MON, 0)], ["000300"])
        self.assertEqual(select_rsi(c, universe, set(), 10, RsiParams(price_cap=10_000), MON, 0), [])  # 가격 상한


class ComboStrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        uni = Path(self.tmp.name) / "uni.txt"
        uni.write_text("# test\n000100 A\n000200 B\n000300 C\n", encoding="utf-8")
        self.cfg = Config(dry_run=True, state_dir=self.tmp.name, strategy="combo", num_stocks=2,
                          total_budget=200_000, position_pct=0, rsi_universe_file=str(uni))
        self.client = FakeClient()
        self.bo = [BreakoutCandidate("000010", "신고가", 10_800, 3e10, 0.05, 10_000)]
        self.s = ComboStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file),
                               selector=lambda client, held, n, p, today: [c for c in self.bo if c.symbol not in held][:n])
        # RSI 후보는 실제 select_rsi 로 (요청 간격 없이)
        self.s.select_rsi = lambda *a: select_rsi(*a, request_interval=0)

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, d, hh, mm):
        self.client.today = d
        day = trading_day(d)
        self.s.tick(day.market_open.replace(hour=hh, minute=mm), day)

    def test_make_strategy(self):
        s = make_strategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        self.assertIsInstance(s, ComboStrategy)
        self.assertEqual(len(s.universe), 3)

    def test_breakout_first_then_rsi_fills(self):
        self.tick(MON, 15, 10)
        pos = self.s.state.positions
        self.assertEqual({k: p.kind for k, p in pos.items()}, {"000010": "breakout", "000100": "rsi"})
        conds = self.s.broker._dry_conditionals
        rsi_pos, bo_pos = pos["000100"], pos["000010"]
        self.assertEqual(conds[rsi_pos.stop_co_id]["triggerPrice"], 9_900)  # 11,000 x 0.9 (손절 -10%)
        self.assertIsNone(rsi_pos.tp_co_id)  # RSI 종목은 익절 조건주문 없음
        self.assertEqual(conds[bo_pos.stop_co_id]["triggerPrice"], 10_290)  # 신고가: -4.7%
        self.assertEqual(conds[bo_pos.tp_co_id]["triggerPrice"], 12_960)  # +20%

    def test_rsi_only_when_no_breakout(self):
        self.bo = []
        self.tick(MON, 15, 10)
        self.assertEqual(sorted(self.s.state.positions), ["000100", "000300"])

    def test_rsi_exit_on_rebound_then_buy_next_tick(self):
        self.bo = []
        self.tick(MON, 15, 10)
        self.client.closes["000100"] = list(RISING)  # 다음 날 RSI 회복
        self.client.prices["000100"] = 16_000
        self.tick(TUE, 15, 10)
        self.assertNotIn("000100", self.s.state.positions)
        self.assertEqual((self.s.state.history[-1]["reason"], self.s.state.history[-1]["kind"]), ("RSI_EXIT", "rsi"))
        self.assertIn("000300", self.s.state.positions)  # RSI 아직 낮음 → 유지
        self.assertNotEqual(self.s.state.last_buy_date, TUE.isoformat())  # 매도한 주기에는 매수 안 함
        self.bo = [BreakoutCandidate("000010", "신고가", 10_800, 3e10, 0.05, 10_000)]
        self.tick(TUE, 15, 11)
        self.assertEqual(self.s.state.positions["000010"].kind, "breakout")

    def test_rsi_time_exit(self):
        self.bo = []
        self.cfg.rsi_max_hold_days = 2
        self.tick(MON, 15, 10)
        self.tick(TUE, 15, 10)  # 1일째
        self.assertIn("000100", self.s.state.positions)
        self.tick(date(2026, 9, 23), 15, 10)  # 2일째 → 매도
        self.assertNotIn("000100", self.s.state.positions)
        self.assertEqual(self.s.state.history[-1]["reason"], "TIME_EXIT")

    def test_breakout_positions_unaffected_by_rsi_exit(self):
        self.tick(MON, 15, 10)
        self.client.closes["000010"] = list(RISING)
        self.tick(TUE, 15, 10)
        self.assertIn("000010", self.s.state.positions)  # 신고가 종목은 RSI 로 팔지 않음

    def test_old_state_without_kind_is_breakout(self):
        Path(self.cfg.state_file).write_text(json.dumps({"positions": {"000010": {
            "symbol": "000010", "name": "옛", "quantity": 9, "entry_price": 10_000}}}), encoding="utf-8")
        s = ComboStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        p = s.state.positions["000010"]
        self.assertEqual(p.kind, "breakout")
        self.assertEqual((s.stop_for(p), s.tp_for(p)), (9_530, 12_000))


if __name__ == "__main__":
    unittest.main()
