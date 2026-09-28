import tempfile
import unittest
from datetime import date, datetime, timedelta

from tossbot.broker import Broker, round_down_to_tick, round_up_to_tick
from tossbot.client import TossClient
from tossbot.config import KST, Config
from tossbot.market_calendar import TradingDay
from tossbot.selector import PullbackParams, WatchItem, analyze, build_watchlist, entry_signal, parse_candles
from tossbot.state import StateStore
from tossbot.strategy import PullbackStrategy

TODAY = date(2026, 9, 22)
# 아래 시나리오는 5일선 기준으로 숫자를 맞춰 두었으므로 기본값(MA_PERIOD)과 무관하게 5일선으로 고정


def candle(d, c, v=1_000_000):
    return {
        "timestamp": f"{d.isoformat()}T00:00:00+09:00",
        "openPrice": str(c), "highPrice": str(c * 1.01), "lowPrice": str(c * 0.99), "closePrice": str(c),
        "volume": str(v), "currency": "KRW",
    }


def closes_to_candles(closes, today_price=None, volumes=None):
    """closes[-1] 이 어제 종가. today_price 가 있으면 오늘(장중) 봉도 추가. API 처럼 최신순.
    volumes 를 안 주면 급등일(+10% 이상)은 거래량 300만, 나머지는 100만."""
    if volumes is None:
        volumes = [3_000_000 if i and c / closes[i - 1] >= 1.1 else 1_000_000 for i, c in enumerate(closes)]
    out = [candle(TODAY - timedelta(days=len(closes) - i), c, v) for i, (c, v) in enumerate(zip(closes, volumes))]
    if today_price:
        out.append(candle(TODAY, today_price))
    return list(reversed(out))


def surge_series(surge_pct=0.12, days_after=(0.97, 0.98), n=40, base=10_000.0):
    closes = [base * 1.002**i for i in range(n)]
    closes.append(closes[-1] * (1 + surge_pct))
    for r in days_after:
        closes.append(closes[-1] * r)
    return closes


class SelectorTest(unittest.TestCase):
    def test_analyze_surge_and_pullback(self):
        p = PullbackParams(ma_period=5)
        item, reason = analyze("A", "A", parse_candles(closes_to_candles(surge_series(), today_price=9_000)), TODAY, p)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(item.surge_pct, 0.12, places=6)
        self.assertEqual(item.surge_date, TODAY - timedelta(days=3))
        # 오늘(장중) 봉은 계산에서 제외: 직전 4거래일 종가 합
        closes = surge_series()
        self.assertAlmostEqual(item.prev_closes_sum, sum(closes[-4:]))

    def test_default_7_day_ma(self):
        p = PullbackParams()  # 봇 기본값: 7일선
        self.assertEqual(p.ma_period, 7)
        # 긴 이평선은 급등 전 봉 비중이 커서, 급등폭이 작으면 터치 가격이 급등 전 종가 아래가 되어 매수 제외됨
        closes = surge_series(surge_pct=0.20)
        item, reason = analyze("A", "A", parse_candles(closes_to_candles(closes)), TODAY, p)
        self.assertEqual(reason, "")
        self.assertAlmostEqual(item.prev_closes_sum, sum(closes[-6:]))
        ma_at_touch = item.prev_closes_sum / 6  # 현재가 = 실시간 7일선이 되는 가격
        self.assertTrue(entry_signal(item, ma_at_touch, p)[0])
        self.assertFalse(entry_signal(item, ma_at_touch * 1.01, p)[0])

    def test_analyze_rejections(self):
        p = PullbackParams(ma_period=5)
        no_surge = [10_000 * 1.002**i for i in range(45)]
        self.assertIn("급등 없음", analyze("A", "A", parse_candles(closes_to_candles(no_surge)), TODAY, p)[1])
        too_old = surge_series(days_after=[1.0] * 12)
        self.assertIn("급등 없음", analyze("A", "A", parse_candles(closes_to_candles(too_old)), TODAY, p)[1])
        gave_back = surge_series(days_after=(0.9, 0.9))
        self.assertEqual(analyze("A", "A", parse_candles(closes_to_candles(gave_back)), TODAY, p)[1], "급등분 모두 반납")
        below_ma = surge_series(surge_pct=0.20, days_after=(0.95, 0.95, 0.95))  # 전일 종가가 이미 5일선 아래
        self.assertIn("5일선 아래", analyze("A", "A", parse_candles(closes_to_candles(below_ma)), TODAY, p)[1])
        heavy = surge_series()
        heavy_vol = [1_000_000] * (len(heavy) - 3) + [3_000_000, 2_000_000, 2_000_000]  # 눌림 중 거래량 많음
        self.assertEqual(
            analyze("A", "A", parse_candles(closes_to_candles(heavy, volumes=heavy_vol)), TODAY, p)[1],
            "눌림 구간 거래량 과다",
        )
        fresh = surge_series(days_after=(0.97,))  # 급등 후 2일째
        self.assertIn("급등 후 2일째", analyze("A", "A", parse_candles(closes_to_candles(fresh)), TODAY, p)[1])
        pricey = [c * 20 for c in surge_series()]
        self.assertIn("예산 초과", analyze("A", "A", parse_candles(closes_to_candles(pricey)), TODAY, p)[1])
        thin = parse_candles(closes_to_candles(surge_series()))
        for b in thin:
            b.volume /= 1_000
        self.assertEqual(analyze("A", "A", thin, TODAY, p)[1], "거래대금 부족")

    def test_entry_signal_touch_ma7(self):
        p = PullbackParams(ma_period=5)
        item = WatchItem("A", "A", TODAY, 0.12, pre_surge_close=8_000, prev_closes_sum=4 * 10_000, ma_period=5)
        self.assertTrue(entry_signal(item, 10_000, p)[0])  # 5일선 = 10,000 에 정확히 터치
        self.assertFalse(entry_signal(item, 10_050, p)[0])  # 아직 5일선 위 (5일선 10,010)
        self.assertTrue(entry_signal(item, 9_900, p)[0])  # 살짝 뚫음 (5일선 9,980 대비 -0.8%)
        self.assertFalse(entry_signal(item, 9_800, p)[0])  # -1.7%: 이미 이탈
        low_base = WatchItem("A", "A", TODAY, 0.12, pre_surge_close=10_100, prev_closes_sum=4 * 10_000, ma_period=5)
        self.assertFalse(entry_signal(low_base, 10_000, p)[0])  # 급등 전 가격 아래

    def test_build_watchlist_uses_rankings_and_memory(self):
        class C:
            def get_rankings(self, t, d):
                return [{"symbol": "000001"}] if d == "1w" else []

            def get_stocks(self, symbols):
                base = {"status": "ACTIVE", "securityType": "STOCK", "isCommonShare": True, "market": "KOSDAQ"}
                return [{"symbol": s, "name": s, **base} for s in symbols] + []

            def get_candles(self, s, interval, count):
                return closes_to_candles(surge_series() if s in ("000001", "000002") else [10_000.0] * 45)

        watch = build_watchlist(C(), {"000002", "000003"}, PullbackParams(ma_period=5), TODAY, request_interval=0)
        self.assertEqual(sorted(watch), ["000001", "000002"])


class TickTest(unittest.TestCase):
    def test_round_to_tick(self):
        self.assertEqual(round_up_to_tick(1_999.2), 2_000)
        self.assertEqual(round_up_to_tick(12_341), 12_350)
        self.assertEqual(round_down_to_tick(9_530), 9_530)
        self.assertEqual(round_down_to_tick(71_234), 71_200)


class FakeClient:
    def __init__(self, prices):
        self.prices = dict(prices)
        self.kospi_prev_close, self.kospi_now = 3_000.0, 2_990.0  # 기본: 코스피 하락 중

    def get_indicator_candles(self, symbol, interval="1d", count=5):
        d = TODAY - timedelta(days=30)  # 테스트 날짜들보다 이전의 전일 종가
        return [candle(d, self.kospi_prev_close)]

    def get_indicator_prices(self, symbols):
        return [{"symbol": "KOSPI", "lastPrice": str(self.kospi_now)}]

    def get_prices(self, symbols):
        return [{"symbol": s, "lastPrice": str(self.prices[s]), "currency": "KRW"} for s in symbols if s in self.prices]


def trading_day(d: date) -> TradingDay:
    t = datetime(d.year, d.month, d.day, tzinfo=KST)
    return TradingDay(d, True, d - timedelta(days=1), d + timedelta(days=1), t.replace(hour=9),
                      t.replace(hour=15, minute=20), t.replace(hour=15, minute=30))


MON, TUE, WED = trading_day(date(2026, 9, 21)), trading_day(date(2026, 9, 22)), trading_day(date(2026, 9, 23))


def watch(symbol, surge_pct=0.12):
    # 직전 4일 종가 합 40,000 → 현재가 10,000 이면 5일선 10,000 (괴리 0%)
    return WatchItem(symbol, f"종목{symbol}", date(2026, 9, 17), surge_pct, pre_surge_close=8_000,
                     prev_closes_sum=40_000, ma_period=5)


class StrategyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 눌림목 전략 시나리오 (기본값이 신고가 전략으로 바뀌었으므로 눌림목 설정을 명시)
        self.cfg = Config(dry_run=True, state_dir=self.tmp.name, strategy="pullback", ma_period=5,
                          buy_start="09:00", market_filter="kospi_down", take_profit_pct=15.0, max_hold_days=10)
        # 기본은 5일선보다 10% 위 (아직 눌리지 않음)
        self.client = FakeClient({f"{i:06d}": 11_000 for i in range(1, 30)})
        self.watch = {f"{i:06d}": watch(f"{i:06d}", 0.10 + i / 100) for i in range(1, 16)}
        self.builds = 0

        def builder(client, extra, params, today):
            self.builds += 1
            return dict(self.watch)

        self.strategy = PullbackStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file), builder)

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, day, hh, mm):
        self.strategy.tick(day.market_open.replace(hour=hh, minute=mm), day)

    def test_buys_only_when_price_reaches_ma(self):
        s = self.strategy
        self.tick(MON, 9, 0)
        self.assertEqual(len(s.state.positions), 0)
        self.client.prices["000003"] = 10_050  # 아직 5일선(10,007) 위
        self.tick(MON, 11, 0)
        self.assertEqual(len(s.state.positions), 0)
        self.client.prices["000003"] = 10_000  # 5일선 터치
        self.tick(MON, 13, 47)  # 시간대 상관없이 장중 아무 때나
        self.assertEqual(list(s.state.positions), ["000003"])
        self.assertEqual(s.state.positions["000003"].quantity, 9)  # 10,050원 지정가 → 9주
        self.assertEqual(self.builds, 1)  # 감시 목록은 하루 한 번
        self.tick(MON, 14, 0)
        self.assertEqual(len(s.state.positions), 1)  # 보유 중인 종목은 재매수 없음
        self.assertEqual(s.state.surge_seen["000003"], "2026-09-21")

    def test_buys_only_when_kospi_is_down(self):
        s = self.strategy
        self.client.prices["000001"] = 10_000  # 5일선 터치
        self.client.kospi_now = 3_010  # 코스피 상승 중 → 매수 보류
        self.tick(MON, 10, 0)
        self.assertEqual(len(s.state.positions), 0)
        self.client.kospi_now = 2_999  # 코스피 하락 전환 → 매수
        self.tick(MON, 10, 1)
        self.assertEqual(list(s.state.positions), ["000001"])

    def test_market_filter_can_be_disabled(self):
        self.cfg.market_filter = "none"
        self.client.prices["000001"] = 10_000
        self.client.kospi_now = 3_010
        self.tick(MON, 10, 0)
        self.assertEqual(list(self.strategy.state.positions), ["000001"])

    def test_no_buy_after_1520(self):
        self.client.prices["000001"] = 10_000
        self.tick(MON, 15, 20)
        self.assertEqual(len(self.strategy.state.positions), 0)

    def test_slots_limited_and_bigger_surge_first(self):
        s = self.strategy
        for sym in self.watch:
            self.client.prices[sym] = 10_000
        self.tick(MON, 10, 0)
        self.assertEqual(len(s.state.positions), 10)
        # 급등폭 큰 순 (000015 가 가장 큼) 으로 10종목
        self.assertEqual(sorted(s.state.positions), [f"{i:06d}" for i in range(6, 16)])

    def test_stop_loss_then_cooldown(self):
        s = self.strategy
        self.client.prices["000001"] = 10_000
        self.tick(MON, 10, 0)
        conds = s.broker._dry_conditionals
        p = s.state.positions["000001"]
        self.assertEqual((conds[p.stop_co_id]["triggerPrice"], conds[p.stop_co_id]["orderType"]), (9_530, "MARKET"))
        self.assertEqual((conds[p.tp_co_id]["triggerPrice"], conds[p.tp_co_id]["orderPrice"]), (11_500, 11_500))

        self.client.prices["000001"] = 9_500
        self.tick(MON, 11, 0)
        self.assertNotIn("000001", s.state.positions)
        self.assertEqual(s.state.history[-1]["reason"], "STOP_LOSS")
        # 다시 5일선 부근이 와도 쿨다운(5일) 동안은 재매수하지 않음
        self.client.prices["000001"] = 10_000
        self.tick(TUE, 10, 0)
        self.assertNotIn("000001", s.state.positions)
        self.assertTrue(s.in_cooldown("000001", date(2026, 9, 25)))
        self.assertFalse(s.in_cooldown("000001", date(2026, 9, 26)))

    def test_take_profit_and_hold_until_hit(self):
        s = self.strategy
        self.client.prices["000002"] = 10_000
        self.tick(MON, 10, 0)
        self.client.prices["000002"] = 10_500  # +5%: 유지
        self.tick(WED, 10, 0)
        self.assertIn("000002", s.state.positions)
        self.client.prices["000002"] = 11_500
        self.tick(WED, 11, 0)
        self.assertEqual(s.state.history[-1]["reason"], "TAKE_PROFIT")

    def test_time_exit_after_max_hold_days(self):
        s = self.strategy
        self.cfg.max_hold_days = 2
        self.client.prices["000001"] = 10_000
        self.tick(MON, 10, 0)  # 매수일 = 0일째
        self.client.prices["000001"] = 10_300  # 손절·익절 모두 안 걸림
        self.tick(TUE, 15, 10)  # 1일째
        self.tick(WED, 15, 9)  # 2일째, 매도 시각 전
        self.assertIn("000001", s.state.positions)
        self.assertEqual(s.state.positions["000001"].hold_days, 2)
        self.tick(WED, 15, 10)
        self.assertNotIn("000001", s.state.positions)
        self.assertEqual((s.state.history[-1]["reason"], s.state.history[-1]["exit_price"]), ("TIME_EXIT", 10_300))
        # 조건주문도 모두 취소됨
        self.assertFalse([c for c in s.broker._dry_conditionals.values() if c["status"] == "WATCHING"])

    def test_state_persists_across_restart(self):
        self.client.prices["000001"] = 10_000
        self.tick(MON, 10, 0)
        reloaded = PullbackStrategy(self.cfg, Broker(self.client, dry_run=True), StateStore(self.cfg.state_file))
        self.assertEqual(list(reloaded.state.positions), ["000001"])
        self.assertIn("000001", reloaded.state.surge_seen)


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
