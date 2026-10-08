"""키움증권 REST API 클라이언트 (필요한 TR 만).

- 토큰: POST /oauth2/token {grant_type, appkey, secretkey} → {token, expires_dt}
- 모든 TR: POST {base}{경로}, 헤더 api-id / authorization / cont-yn / next-key, 본문 JSON
- 응답 본문의 return_code 가 0 이 아니면 실패
- 실전 https://api.kiwoom.com, 모의투자 https://mockapi.kiwoom.com (모의는 KRX 만)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

import requests

log = logging.getLogger(__name__)

REAL_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"


class KiwoomApiError(RuntimeError):
    def __init__(self, api_id: str, status: int, code: Any, message: str):
        super().__init__(f"[{api_id}] HTTP {status} code={code}: {message}")
        self.api_id = api_id
        self.status = status
        self.code = code


def num(value: Any) -> float:
    """키움 숫자 문자열('+12,300', '-500', '') → 부호 없는 float. 가격 필드는 부호가 등락 방향이라 절댓값을 쓴다."""
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    if not s:
        return 0.0
    return abs(float(s))


def token_invalid(code: Any, message: Any) -> bool:
    """응답이 '토큰이 유효하지 않음'(return_code 3, 메시지에 8005)인지."""
    return str(code) == "3" and "8005" in str(message or "")


def ord_key(order_no: Any) -> str:
    """주문번호 비교용: 주문 응답('00024')과 미체결 목록('0000024')의 자릿수가 달라도 같게 본다."""
    return str(order_no or "").strip().lstrip("0")


class KiwoomClient:
    MAX_RETRIES = 3

    def __init__(self, app_key: str, secret_key: str, mock: bool = True, timeout: float = 10.0,
                 min_interval: float = 0.25, session: requests.Session | None = None):
        if not app_key or not secret_key:
            raise ValueError("KIWOOM_APP_KEY / KIWOOM_SECRET_KEY 가 설정되지 않았습니다 (.env.kiwoom).")
        self.app_key = app_key
        self.secret_key = secret_key
        self.base_url = MOCK_URL if mock else REAL_URL
        self.mock = mock
        self.timeout = timeout
        self.min_interval = min_interval  # 초당 호출 제한 대비 최소 간격
        self.session = session or requests.Session()
        self._token: str | None = None
        self._token_expires: datetime | None = None
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        if self._token and self._token_expires and datetime.now() < self._token_expires:
            return self._token
        resp = self.session.post(
            f"{self.base_url}/oauth2/token",
            json={"grant_type": "client_credentials", "appkey": self.app_key, "secretkey": self.secret_key},
            headers={"Content-Type": "application/json;charset=UTF-8"},
            timeout=self.timeout,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get("token"):
            raise KiwoomApiError("token", resp.status_code, data.get("return_code"), str(data.get("return_msg") or resp.text[:200]))
        self._token = data["token"]
        exp = data.get("expires_dt")
        try:
            # 만료 5분 전에 다시 받는다
            self._token_expires = datetime.strptime(exp, "%Y%m%d%H%M%S") - timedelta(minutes=5) if exp else None
        except ValueError:
            self._token_expires = None
        log.info("키움 토큰 발급 (%s, 만료 %s)", "모의투자" if self.mock else "실전", exp)
        return self._token

    # --------------------------------------------------------------- request
    def request(self, api_id: str, path: str, body: dict, cont_key: str = "") -> tuple[dict, str]:
        """TR 호출. (응답 본문, 다음 페이지 키 — 없으면 '')."""
        retried_token = False
        for attempt in range(self.MAX_RETRIES + 1):
            with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {self._access_token()}",
                "api-id": api_id,
                "cont-yn": "Y" if cont_key else "N",
                "next-key": cont_key,
            }
            try:
                resp = self.session.post(f"{self.base_url}{path}", json=body, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                # 주문은 중복 위험이 있어 재시도하지 않는다
                if path.endswith("/ordr") or attempt >= self.MAX_RETRIES:
                    raise
                log.warning("%s 네트워크 오류 %s, 재시도", api_id, exc)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401 and attempt < self.MAX_RETRIES:
                self._token = None
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < self.MAX_RETRIES and not path.endswith("/ordr"):
                time.sleep(2 ** attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                data = {}
            code = data.get("return_code", 0 if resp.status_code < 400 else None)
            # 토큰 무효는 HTTP 401 이 아니라 본문 return_code 3 / 메시지 8005 로 오기도 한다.
            # 요청이 처리되기 전에 거절된 것이라 주문도 다시 보내도 중복되지 않는다.
            if token_invalid(code, data.get("return_msg")) and not retried_token:
                log.warning("%s 토큰이 유효하지 않음 → 다시 발급받아 재시도", api_id)
                self._token = None
                retried_token = True
                continue
            if resp.status_code >= 400 or code not in (0, "0", None):
                raise KiwoomApiError(api_id, resp.status_code, code, str(data.get("return_msg") or resp.text[:200]))
            next_key = resp.headers.get("next-key", "") if resp.headers.get("cont-yn") == "Y" else ""
            return data, next_key
        raise RuntimeError("unreachable")

    # ----------------------------------------------------------- market data
    def change_rate_ranking(self, max_pages: int = 5) -> list[dict]:
        """ka10027 전일대비 등락률 상위 (상승률 순, 코스피+코스닥, ETF·ETN 제외, KRX)."""
        body = {"mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "16", "crd_cnd": "0",
                "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": "1"}
        out, key = [], ""
        for _ in range(max_pages):
            data, key = self.request("ka10027", "/api/dostk/rkinfo", body, key)
            out.extend(data.get("pred_pre_flu_rt_upper") or [])
            if not key:
                break
        return out

    def daily_chart(self, code: str, base_dt: str) -> list[dict]:
        """ka10081 일봉 (최신순). base_dt = YYYYMMDD."""
        data, _ = self.request("ka10081", "/api/dostk/chart", {"stk_cd": code, "base_dt": base_dt, "upd_stkpc_tp": "1"})
        return data.get("stk_dt_pole_chart_qry") or []

    def minute_chart(self, code: str) -> list[dict]:
        """ka10080 1분봉 (최신순, 오늘 + 이전 거래일 일부)."""
        data, _ = self.request("ka10080", "/api/dostk/chart", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"})
        return data.get("stk_min_pole_chart_qry") or []

    # ---------------------------------------------------------------- account
    def balance(self) -> dict:
        """kt00018 계좌평가잔고 (합산). 추정예탁자산 prsm_dpst_aset_amt, 종목별 acnt_evlt_remn_indv_tot."""
        data, _ = self.request("kt00018", "/api/dostk/acnt", {"qry_tp": "1", "dmst_stex_tp": "KRX"})
        return data

    def holdings(self) -> dict[str, dict]:
        """{종목코드(6자리): {qty, avg_price, cur_price, name}}."""
        out = {}
        for r in self.balance().get("acnt_evlt_remn_indv_tot") or []:
            code = str(r.get("stk_cd", "")).lstrip("A")[-6:]
            qty = int(num(r.get("rmnd_qty")))
            if qty > 0:
                out[code] = {"qty": qty, "avg_price": num(r.get("pur_pric")), "cur_price": num(r.get("cur_prc")),
                             "name": r.get("stk_nm", "")}
        return out

    def orderable_cash(self) -> float:
        """kt00001 예수금상세현황에서 '증거금 100% 종목 주문가능금액'(100stk_ord_alow_amt).
        2026-10-08 실계좌 확인: 아침에 ETF 를 팔고 난 뒤 ord_alow_amt 는 347,307원(매도 대금이 결제되기 전 현금만)인데
        100stk_ord_alow_amt 는 1,019,874원(= D+2 추정예수금, 매도 대금을 바로 다시 쓸 수 있는 금액)이었다.
        ord_alow_amt 로 수량을 막으면 매도 대금을 못 써서 주문이 1/3 로 줄어든다. 100% 기준이라 미수는 생기지 않는다."""
        data, _ = self.request("kt00001", "/api/dostk/acnt", {"qry_tp": "3"})
        full = num(data.get("100stk_ord_alow_amt"))
        return full if full > 0 else num(data.get("ord_alow_amt"))

    def unfilled(self) -> list[dict]:
        """ka10075 미체결 (전체 종목)."""
        data, _ = self.request("ka10075", "/api/dostk/acnt", {"all_stk_tp": "0", "trde_tp": "0", "stex_tp": "1"})
        return data.get("oso") or []

    # ----------------------------------------------------------------- orders
    def _order(self, api_id: str, code: str, qty: int, price: int | None) -> str:
        body = {"dmst_stex_tp": "KRX", "stk_cd": code, "ord_qty": str(int(qty)),
                "ord_uv": str(int(price)) if price else "", "trde_tp": "0" if price else "3", "cond_uv": ""}
        data, _ = self.request(api_id, "/api/dostk/ordr", body)
        return str(data.get("ord_no", ""))

    def buy(self, code: str, qty: int, price: int | None = None) -> str:
        """kt10000 매수. price 가 있으면 지정가(보통), 없으면 시장가. 주문번호 반환."""
        return self._order("kt10000", code, qty, price)

    def sell(self, code: str, qty: int, price: int | None = None) -> str:
        """kt10001 매도. price 가 있으면 지정가, 없으면 시장가."""
        return self._order("kt10001", code, qty, price)

    def cancel(self, code: str, order_no: str) -> str:
        """kt10003 취소 (잔량 전부)."""
        data, _ = self.request("kt10003", "/api/dostk/ordr",
                               {"dmst_stex_tp": "KRX", "orig_ord_no": order_no, "stk_cd": code, "cncl_qty": "0"})
        return str(data.get("ord_no", ""))
