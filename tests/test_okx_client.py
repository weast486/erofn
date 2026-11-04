"""OKX 클라이언트: 바이낸스 모양으로 바꾸는 부분(종목 이름·계약 수·주문·체결)을 가짜 응답으로 확인."""
import json
import unittest

from binancebot.client import BinanceError, filters_of
from binancebot.okx_client import OkxClient, to_inst, to_sym

INST = {"instId": "SOXL-USDT-SWAP", "settleCcy": "USDT", "state": "live", "instCategory": "3", "ctVal": "0.1",
        "lotSz": "0.01", "tickSz": "0.01", "floatPxLmtPct": "0.02", "lever": "20"}
BTC = {"instId": "BTC-USDT-SWAP", "settleCcy": "USDT", "state": "live", "instCategory": "1", "ctVal": "0.01",
       "lotSz": "0.01", "tickSz": "0.1", "floatPxLmtPct": "0.01", "lever": "100"}


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body


class FakeSession:
    def __init__(self):
        self.calls = []
        self.order_state = "filled"

    def request(self, method, url, headers=None, data=None, timeout=None):
        path = url.split("https://www.okx.com")[1]
        body = json.loads(data) if data else None
        self.calls.append((method, path, body, headers))
        p = path.split("?")[0]
        if p == "/api/v5/public/instruments":
            return Resp({"code": "0", "data": [INST, BTC]})
        if p == "/api/v5/trade/order" and method == "POST":
            return Resp({"code": "0", "data": [{"ordId": "111", "sCode": "0"}]})
        if p == "/api/v5/trade/order" and method == "GET":
            return Resp({"code": "0", "data": [{"state": self.order_state, "accFillSz": "30", "avgPx": "95.5"}]})
        if p == "/api/v5/account/positions":
            return Resp({"code": "0", "data": [{"instId": "SOXL-USDT-SWAP", "pos": "-30", "avgPx": "95.5", "posSide": "net"}]})
        if p == "/api/v5/trade/fills":
            return Resp({"code": "0", "data": [{"side": "sell", "fillPx": "95.5", "fillSz": "30", "ts": "1", "ordId": "111"}]})
        if p == "/api/v5/account/balance":
            return Resp({"code": "0", "data": [{"totalEq": "0", "details": [{"ccy": "USDT", "eq": "123.4"}]}]})
        if p == "/api/v5/trade/order-algo":
            return Resp({"code": "0", "data": [{"algoId": "9", "sCode": "0"}]})
        if p == "/api/v5/account/set-leverage":
            return Resp({"code": "1", "msg": "", "data": [{"sCode": "59000", "sMsg": "거절 테스트"}]})
        if p == "/api/v5/market/candles":
            return Resp({"code": "0", "data": []})
        return Resp({"code": "0", "data": []})


class OkxClientTest(unittest.TestCase):
    def setUp(self):
        self.s = FakeSession()
        self.c = OkxClient("k", "s", "p", session=self.s)

    def test_names(self):
        self.assertEqual((to_inst("SOXLUSDT"), to_sym("SOXL-USDT-SWAP")), ("SOXL-USDT-SWAP", "SOXLUSDT"))

    def test_exchange_info_like_binance(self):
        syms = {x["symbol"]: x for x in self.c.exchange_info()["symbols"]}
        self.assertEqual(syms["SOXLUSDT"]["underlyingType"], "EQUITY")
        self.assertEqual(syms["BTCUSDT"]["underlyingType"], "")
        f = filters_of(syms["SOXLUSDT"])
        self.assertAlmostEqual(f["step"], 0.001)    # 0.01 계약 x 0.1주
        self.assertAlmostEqual(f["pct_up"], 0.02)

    def test_ioc_order_contracts_and_fill(self):
        self.c.exchange_info()
        r = self.c.order(symbol="SOXLUSDT", side="SELL", type="LIMIT", timeInForce="IOC", quantity="3.0049", price="95.5")
        post = [c for c in self.s.calls if c[0] == "POST" and c[1] == "/api/v5/trade/order"][0][2]
        self.assertEqual(post["ordType"], "ioc")
        self.assertEqual(post["sz"], "30.04")        # 3.0049주 / 0.1 = 30.049 계약 → 0.01 단위 내림
        self.assertEqual(post["tdMode"], "cross")
        self.assertAlmostEqual(float(r["executedQty"]), 3.0)
        self.assertEqual(r["avgPrice"], "95.5")
        headers = [c for c in self.s.calls if c[0] == "POST"][0][3]
        self.assertTrue(headers["OK-ACCESS-SIGN"] and headers["OK-ACCESS-PASSPHRASE"] == "p")

    def test_market_reduce_only_and_limit(self):
        self.c.exchange_info()
        self.c.order(symbol="SOXLUSDT", side="BUY", type="MARKET", quantity="3", reduceOnly="true")
        self.c.order(symbol="SOXLUSDT", side="BUY", type="LIMIT", timeInForce="GTC", quantity="3", price="90",
                     reduceOnly="true")
        posts = [c[2] for c in self.s.calls if c[0] == "POST" and c[1] == "/api/v5/trade/order"]
        self.assertEqual((posts[0]["ordType"], posts[0].get("px"), posts[0]["reduceOnly"]), ("market", None, True))
        self.assertEqual((posts[1]["ordType"], posts[1]["px"]), ("limit", "90"))

    def test_position_fills_equity_stop(self):
        self.assertEqual(self.c.position_amt("SOXLUSDT"), (-3.0, 95.5))
        f = self.c.fills("SOXLUSDT", order_id="111")[0]
        self.assertEqual((f["side"], f["price"]), ("SELL", "95.5"))
        self.assertAlmostEqual(f["qty"], 3.0)
        self.assertAlmostEqual(self.c.equity(), 123.4)
        self.assertEqual(self.c.stop_market("SOXLUSDT", "BUY", 97.1), "algo")
        algo = [c[2] for c in self.s.calls if c[1] == "/api/v5/trade/order-algo"][0]
        self.assertEqual((algo["slTriggerPx"], algo["closeFraction"], algo["side"]), ("97.1", "1", "buy"))

    def test_error_is_binance_error(self):
        self.c.exchange_info()
        with self.assertRaises(BinanceError) as ctx:
            self.c.set_leverage("SOXLUSDT", 20)
        self.assertEqual(ctx.exception.code, "59000")


if __name__ == "__main__":
    unittest.main()


class OrderTestSession(FakeSession):
    """okx-test 흐름: 포지션 없음 → 매수 뒤 30계약 → 매도 뒤 없음, 손절은 등록 뒤 취소되면 사라짐."""

    def __init__(self):
        super().__init__()
        self.pos, self.algo = "0", []

    def request(self, method, url, headers=None, data=None, timeout=None):
        p = url.split("https://www.okx.com")[1].split("?")[0]
        body = json.loads(data) if data else None
        self.calls.append((method, p, body, headers))
        if p == "/api/v5/public/time":
            return Resp({"code": "0", "data": [{"ts": "1"}]})
        if p == "/api/v5/account/config":
            return Resp({"code": "0", "data": [{"acctLv": "2", "posMode": "net_mode"}]})
        if p == "/api/v5/market/tickers":
            return Resp({"code": "0", "data": [{"instId": "SOXL-USDT-SWAP", "last": "95.5"}]})
        if p == "/api/v5/account/set-leverage":
            return Resp({"code": "0", "data": [{}]})
        if p == "/api/v5/trade/order" and method == "POST":
            self.pos = "0" if body.get("reduceOnly") else "0.01"
            return Resp({"code": "0", "data": [{"ordId": "111", "sCode": "0"}]})
        if p == "/api/v5/trade/order" and method == "GET":
            return Resp({"code": "0", "data": [{"state": "filled", "accFillSz": "0.01", "avgPx": "95.5"}]})
        if p == "/api/v5/account/positions":
            return Resp({"code": "0", "data": [{"instId": "SOXL-USDT-SWAP", "pos": self.pos, "avgPx": "95.5"}]})
        if p == "/api/v5/trade/order-algo":
            self.algo = [{"algoId": "9"}]
            return Resp({"code": "0", "data": [{"algoId": "9", "sCode": "0"}]})
        if p == "/api/v5/trade/orders-algo-pending":
            return Resp({"code": "0", "data": self.algo})
        if p == "/api/v5/trade/cancel-algos":
            self.algo = []
            return Resp({"code": "0", "data": [{}]})
        return super().request(method, url, headers, data, timeout)


class OkxOrderTestTest(unittest.TestCase):
    def test_full_cycle(self):
        import io
        import os
        from contextlib import redirect_stdout
        from unittest import mock

        import binancebot.okx_client as oc
        from binancebot.botmain import okx_order_test

        s = OrderTestSession()
        env = {"EXCHANGE": "okx", "OKX_API_KEY": "k", "OKX_API_SECRET": "s", "OKX_API_PASSPHRASE": "p"}
        orig = oc.OkxClient.__init__

        def init(self, *a, **kw):
            orig(self, *a, session=s, **{k: v for k, v in kw.items() if k != "session"})

        out = io.StringIO()
        with mock.patch.dict(os.environ, env), mock.patch.object(oc.OkxClient, "__init__", init), \
                mock.patch("binancebot.botmain.load_dotenv"), mock.patch("time.sleep"), redirect_stdout(out):
            okx_order_test("nofile", "SOXLUSDT", yes=True)
        text = out.getvalue()
        self.assertIn("모두 정상", text, text)
        self.assertEqual(s.pos, "0")
