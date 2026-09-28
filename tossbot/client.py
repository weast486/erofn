"""토스증권 Open API REST 클라이언트.

스펙: 토스증권 Open API v1.1.1 (https://openapi.tossinvest.com)
- 인증: OAuth2 Client Credentials (POST /oauth2/token, form-urlencoded)
- 모든 요청: Authorization: Bearer {access_token}
- 계좌 컨텍스트 API: X-Tossinvest-Account: {accountSeq}
- 성공 응답: {"result": ...}, 실패 응답: {"error": {"code", "message", "requestId"}}
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

log = logging.getLogger(__name__)


class TossApiError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(f"[{status}] {code}: {message} (requestId={request_id})")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


class TossClient:
    MAX_RETRIES = 4

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str = "https://openapi.tossinvest.com",
        account_seq: int | None = None,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ):
        if not client_id or not client_secret:
            raise ValueError("TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 이 설정되지 않았습니다.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.account_seq = account_seq
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        with self._lock:
            # 만료 60초 전에 재발급. client 당 유효 토큰은 1개이므로 불필요한 재발급은 피한다.
            if self._token and time.time() < self._token_expires_at - 60:
                return self._token
            resp = self.session.post(
                f"{self.base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                raise TossApiError(resp.status_code, "oauth2-error", resp.text[:300])
            body = resp.json()
            self._token = body["access_token"]
            self._token_expires_at = time.time() + int(body.get("expires_in", 3600))
            log.info("access token 발급 완료 (expires_in=%s)", body.get("expires_in"))
            return self._token

    # --------------------------------------------------------------- request
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        account: bool = False,
    ) -> Any:
        headers = {}
        if account:
            headers["X-Tossinvest-Account"] = str(self.get_account_seq())
        # 주문 생성은 재시도 시 중복 주문 위험이 있으므로 clientOrderId(멱등성 키, 10분 유효)가
        # 있을 때만 네트워크 오류/5xx 재시도를 허용한다.
        retry_unsafe = method == "POST" and not (json or {}).get("clientOrderId")

        for attempt in range(self.MAX_RETRIES + 1):
            headers["Authorization"] = f"Bearer {self._access_token()}"
            try:
                resp = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    params=params,
                    json=json,
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if retry_unsafe or attempt >= self.MAX_RETRIES:
                    raise
                wait = 2**attempt
                log.warning("네트워크 오류 %s, %ss 후 재시도", exc, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 401 and attempt < self.MAX_RETRIES:
                # 다른 곳에서 토큰을 재발급해 기존 토큰이 무효화된 경우
                self._token = None
                continue
            retryable = resp.status_code == 429 or (
                resp.status_code in (502, 503, 504) and not retry_unsafe
            )
            if retryable and attempt < self.MAX_RETRIES:
                wait = float(resp.headers.get("Retry-After") or 2**attempt)
                log.warning("%s %s -> %s, %.1fs 후 재시도", method, path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error", {})
                except ValueError:
                    err = {}
                raise TossApiError(
                    resp.status_code,
                    err.get("code", "http-error"),
                    err.get("message", resp.text[:300]),
                    err.get("requestId", resp.headers.get("X-Request-Id", "")),
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json().get("result")
        raise RuntimeError("unreachable")

    # --------------------------------------------------------------- account
    def get_accounts(self) -> list[dict]:
        return self._request("GET", "/api/v1/accounts") or []

    def get_account_seq(self) -> int:
        if self.account_seq is None:
            accounts = self.get_accounts()
            brokerage = [a for a in accounts if a.get("accountType") == "BROKERAGE"]
            if not brokerage:
                raise RuntimeError("사용 가능한 종합매매(BROKERAGE) 계좌가 없습니다.")
            self.account_seq = int(brokerage[0]["accountSeq"])
            log.info("계좌 선택: %s (accountSeq=%s)", brokerage[0].get("accountNo"), self.account_seq)
        return self.account_seq

    def get_holdings(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/api/v1/holdings", params=params, account=True)

    def get_buying_power(self, currency: str = "KRW") -> dict:
        return self._request(
            "GET", "/api/v1/buying-power", params={"currency": currency}, account=True
        )

    def get_sellable_quantity(self, symbol: str) -> dict:
        return self._request(
            "GET", "/api/v1/sellable-quantity", params={"symbol": symbol}, account=True
        )

    # ------------------------------------------------------------ market data
    def get_prices(self, symbols: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(symbols), 200):
            chunk = symbols[i : i + 200]
            out.extend(self._request("GET", "/api/v1/prices", params={"symbols": ",".join(chunk)}) or [])
        return out

    def get_stocks(self, symbols: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(symbols), 200):
            chunk = symbols[i : i + 200]
            out.extend(self._request("GET", "/api/v1/stocks", params={"symbols": ",".join(chunk)}) or [])
        return out

    def get_candles(
        self, symbol: str, interval: str = "1d", count: int = 200, adjusted: bool = True
    ) -> list[dict]:
        result = self._request(
            "GET",
            "/api/v1/candles",
            params={
                "symbol": symbol,
                "interval": interval,
                "count": count,
                "adjusted": str(adjusted).lower(),
            },
        )
        return (result or {}).get("candles", [])

    def get_rankings(
        self,
        ranking_type: str = "MARKET_TRADING_AMOUNT",
        duration: str = "realtime",
        market: str = "KR",
        count: int = 100,
        exclude_investment_caution: bool = True,
    ) -> list[dict]:
        """시장 랭킹 (상위 100위). ranking_type: MARKET_TRADING_AMOUNT / MARKET_TRADING_VOLUME / TOP_GAINERS 등."""
        result = self._request(
            "GET",
            "/api/v1/rankings",
            params={
                "type": ranking_type,
                "marketCountry": market,
                "duration": duration,
                "count": count,
                "excludeInvestmentCaution": str(exclude_investment_caution).lower(),
            },
        )
        return (result or {}).get("rankings", [])

    def get_indicator_prices(self, symbols: list[str]) -> list[dict]:
        """시장 지표(KOSPI, KOSDAQ 등) 현재가."""
        return self._request(
            "GET", "/api/v1/market-indicators/prices", params={"symbols": ",".join(symbols)}
        ) or []

    def get_indicator_candles(self, symbol: str, interval: str = "1d", count: int = 5) -> list[dict]:
        """시장 지표 캔들 (최신순일 수 있음)."""
        result = self._request(
            "GET",
            f"/api/v1/market-indicators/{symbol}/candles",
            params={"interval": interval, "count": count},
        )
        return (result or {}).get("candles", [])

    def get_market_calendar_kr(self, date: str | None = None) -> dict:
        params = {"date": date} if date else None
        return self._request("GET", "/api/v1/market-calendar/KR", params=params)

    # ----------------------------------------------------------------- orders
    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        order_type: str = "MARKET",
        price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "quantity": str(int(quantity)),
        }
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 주문은 price 가 필요합니다.")
            body["price"] = str(int(price))
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/orders", json=body, account=True)

    def get_order(self, order_id: str) -> dict:
        return self._request("GET", f"/api/v1/orders/{order_id}", account=True)

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        return (self._request("GET", "/api/v1/orders", params=params, account=True) or {}).get(
            "orders", []
        )

    def cancel_order(self, order_id: str) -> dict:
        return self._request("POST", f"/api/v1/orders/{order_id}/cancel", json={}, account=True)

    # ----------------------------------------------------- conditional orders
    # (Open API v1.2 에 추가된 조건주문. 증권사 서버가 가격을 감시하다 조건 충족 시 주문을 생성)
    def create_conditional_order(
        self,
        symbol: str,
        quantity: int,
        side: str,
        trigger_price: int,
        expire_date: str,
        order_type: str = "MARKET",
        order_price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """SINGLE 조건주문 생성. 현재가가 trigger_price 에 닿으면 주문이 나간다."""
        first: dict[str, Any] = {"orderSide": side, "triggerPrice": str(int(trigger_price))}
        if order_type == "LIMIT":
            if order_price is None:
                raise ValueError("LIMIT 조건주문은 order_price 가 필요합니다.")
            first["orderPrice"] = str(int(order_price))
        body: dict[str, Any] = {
            "symbol": symbol,
            "type": "SINGLE",
            "quantity": str(int(quantity)),
            "orderType": order_type,
            "expireDate": expire_date,
            "first": first,
        }
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._request("POST", "/api/v1/conditional-orders", json=body, account=True)

    def get_conditional_order(self, conditional_order_id: str) -> dict:
        return self._request("GET", f"/api/v1/conditional-orders/{conditional_order_id}", account=True)

    def cancel_conditional_order(self, conditional_order_id: str) -> None:
        self._request("DELETE", f"/api/v1/conditional-orders/{conditional_order_id}", account=True)
