import unittest

from kiwoombot.client import KiwoomApiError, KiwoomClient, ord_key, token_invalid

TOKEN_INVALID = {"return_code": 3, "return_msg": "인증에 실패했습니다[8005:Token이 유효하지 않습니다]"}


class FakeResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.content = b"x"
        self.text = str(data)
        self.headers = {}

    def json(self):
        return self._data


class FakeSession:
    """토큰 요청마다 새 토큰(t1, t2, …)을 주고, TR 요청은 정해 둔 응답을 차례로 돌려준다."""

    def __init__(self, tr_responses):
        self.tr_responses = list(tr_responses)
        self.token_calls = 0
        self.tr_tokens = []

    def post(self, url, json=None, headers=None, timeout=None):
        if url.endswith("/oauth2/token"):
            self.token_calls += 1
            return FakeResponse({"token": f"t{self.token_calls}", "expires_dt": "20991231235959", "return_code": 0})
        self.tr_tokens.append(headers["authorization"])
        return FakeResponse(self.tr_responses.pop(0))


def make_client(responses):
    session = FakeSession(responses)
    return KiwoomClient("k", "s", mock=True, min_interval=0, session=session), session


class KiwoomClientTokenTest(unittest.TestCase):
    def test_token_invalid_reissues_and_retries_once(self):
        client, session = make_client([TOKEN_INVALID, {"return_code": 0, "ord_alow_amt": "000000000879052"}])
        self.assertEqual(client.orderable_cash(), 879052)
        self.assertEqual(session.token_calls, 2)
        self.assertEqual(session.tr_tokens, ["Bearer t1", "Bearer t2"])

    def test_token_invalid_twice_raises(self):
        client, session = make_client([TOKEN_INVALID, TOKEN_INVALID])
        with self.assertRaises(KiwoomApiError):
            client.orderable_cash()
        self.assertEqual(len(session.tr_tokens), 2)

    def test_order_retried_after_token_invalid(self):
        client, session = make_client([TOKEN_INVALID, {"return_code": 0, "ord_no": "0000138"}])
        self.assertEqual(client.buy("005930", 1, 70000), "0000138")
        self.assertEqual(len(session.tr_tokens), 2)

    def test_other_error_not_retried(self):
        client, session = make_client([{"return_code": 20, "return_msg": "주문가능금액 부족"}])
        with self.assertRaises(KiwoomApiError):
            client.buy("005930", 1, 70000)
        self.assertEqual(len(session.tr_tokens), 1)

    def test_helpers(self):
        self.assertTrue(token_invalid(3, TOKEN_INVALID["return_msg"]))
        self.assertFalse(token_invalid(3, "다른 인증 오류"))
        self.assertFalse(token_invalid(0, "8005"))
        self.assertEqual(ord_key("00024"), ord_key("0000024"))
        self.assertEqual(ord_key(None), "")


if __name__ == "__main__":
    unittest.main()
