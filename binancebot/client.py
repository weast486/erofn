"""바이낸스 USDT-M 선물 REST 클라이언트 (봇용). 키는 .env.binance 에서만 읽고 로그에 남기지 않는다."""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import time
from urllib.parse import urlencode

BASE = "https://fapi.binance.com"
log = logging.getLogger("binancebot")


class BinanceError(Exception):
    def __init__(self, status: int, code, msg: str):
        super().__init__(f"HTTP {status} code={code} {msg}")
        self.status, self.code, self.msg = status, code, msg


def round_step(x: float, step: float) -> float:
    """거래소 단위로 내림 (부동소수 오차 보정)."""
    if step <= 0:
        return x
    return math.floor(x / step + 1e-9) * step


def round_tick(x: float, tick: float) -> float:
    if tick <= 0:
        return x
    return round(round(x / tick) * tick, 10)


def fmt(x: float) -> str:
    return f"{x:.10f}".rstrip("0").rstrip(".")


def filters_of(meta: dict) -> dict:
    """tick(가격 단위)·step(수량 단위, 시장가 기준 더 큰 쪽)·min_notional·pct_up/down(지정가 허용 범위)."""
    out = {"tick": 0.0, "step": 0.0, "min_notional": 0.0, "pct_up": 0.0, "pct_down": 0.0}
    for f in meta.get("filters", []):
        t = f.get("filterType")
        if t == "PRICE_FILTER":
            out["tick"] = float(f.get("tickSize", 0))
        elif t in ("LOT_SIZE", "MARKET_LOT_SIZE"):
            out["step"] = max(out["step"], float(f.get("stepSize", 0)))
        elif t == "MIN_NOTIONAL":
            out["min_notional"] = float(f.get("notional", 0))
        elif t == "PERCENT_PRICE":
            out["pct_up"] = float(f.get("multiplierUp", 0)) - 1
            out["pct_down"] = 1 - float(f.get("multiplierDown", 1))
    return out


class BinanceClient:
    def __init__(self, api_key: str = "", api_secret: str = "", base: str = BASE, session=None):
        import requests

        self.key, self.secret, self.base = api_key, api_secret, base
        self.s = session or requests.Session()
        self.offset_ms = 0

    # ------------------------------------------------------------ 기본 요청
    def _req(self, method: str, path: str, params: dict | None = None, signed: bool = False, tries: int = 3):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for i in range(tries):
            q = dict(params)
            headers = {}
            if signed:
                q["timestamp"] = int(time.time() * 1000) + self.offset_ms
                q["recvWindow"] = 5000
                qs = urlencode(q)
                q_str = qs + "&signature=" + hmac.new(self.secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
                headers["X-MBX-APIKEY"] = self.key
            else:
                q_str = urlencode(q)
            url = self.base + path + ("?" + q_str if q_str else "")
            try:
                r = self.s.request(method, url, headers=headers, timeout=15)
            except Exception as exc:  # noqa: BLE001 네트워크 오류는 재시도
                if i == tries - 1:
                    raise
                log.warning("네트워크 오류, 재시도 (%s)", exc)
                time.sleep(1 + i)
                continue
            if r.status_code in (418, 429):
                wait = int(r.headers.get("Retry-After", 5))
                log.warning("요청 제한 (%s), %d초 쉬고 다시", r.status_code, wait)
                time.sleep(wait)
                continue
            try:
                body = r.json()
            except ValueError:
                body = {"msg": r.text[:200]}
            if r.status_code >= 400:
                code = body.get("code") if isinstance(body, dict) else None
                if code == -1021 and i < tries - 1:  # 시각 차이 → 맞추고 다시
                    self.sync_time()
                    continue
                if r.status_code >= 500 and i < tries - 1:
                    time.sleep(1 + i)
                    continue
                raise BinanceError(r.status_code, code, body.get("msg", "") if isinstance(body, dict) else str(body))
            return body
        raise BinanceError(0, None, "재시도 초과")

    def sync_time(self) -> None:
        t0 = time.time() * 1000
        server = self._req("GET", "/fapi/v1/time")["serverTime"]
        self.offset_ms = int(server - (t0 + time.time() * 1000) / 2)

    # ------------------------------------------------------------ 시세 (키 필요 없음)
    def exchange_info(self) -> dict:
        return self._req("GET", "/fapi/v1/exchangeInfo")

    def klines(self, symbol: str, start_ms: int, limit: int = 500) -> list[list]:
        return self._req("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "startTime": start_ms, "limit": limit})

    def prices(self) -> dict[str, float]:
        return {r["symbol"]: float(r["price"]) for r in self._req("GET", "/fapi/v1/ticker/price")}

    # ------------------------------------------------------------ 계좌 (키 필요)
    def account(self) -> dict:
        return self._req("GET", "/fapi/v2/account", signed=True)

    def equity(self) -> float:
        """USDT 기준 평가금액(지갑 + 미실현 손익)."""
        acc = self.account()
        for a in acc.get("assets", []):
            if a.get("asset") == "USDT":
                return float(a.get("marginBalance", 0))
        return float(acc.get("totalMarginBalance", 0))

    def hedge_mode(self) -> bool:
        return bool(self._req("GET", "/fapi/v1/positionSide/dual", signed=True).get("dualSidePosition"))

    def position_amt(self, symbol: str) -> tuple[float, float]:
        """(수량, 평균 진입가). 롱 +, 숏 -."""
        for p in self._req("GET", "/fapi/v2/positionRisk", {"symbol": symbol}, signed=True):
            if p.get("symbol") == symbol:
                return float(p.get("positionAmt", 0)), float(p.get("entryPrice", 0))
        return 0.0, 0.0

    def set_leverage(self, symbol: str, leverage: int) -> None:
        self._req("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)}, signed=True)

    def order(self, **params) -> dict:
        return self._req("POST", "/fapi/v1/order", params, signed=True)

    def cancel_all(self, symbol: str) -> None:
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                self._req("DELETE", path, {"symbol": symbol}, signed=True)
            except BinanceError as exc:
                log.info("%s 주문 취소 응답: %s", path, exc)

    def stop_market(self, symbol: str, side: str, stop: float) -> str:
        """거래소 손절(STOP_MARKET, 포지션 전량). 2025-12 부터 조건부 주문은 algoOrder 로 옮겨져서 먼저 그쪽으로,
        안 되면 예전 주문 경로로. 둘 다 안 되면 예외 (봇이 가격을 보고 직접 손절)."""
        try:
            self._req("POST", "/fapi/v1/algoOrder", {"algoType": "CONDITIONAL", "symbol": symbol, "side": side,
                                                     "type": "STOP_MARKET", "triggerPrice": fmt(stop),
                                                     "closePosition": "true", "workingType": "CONTRACT_PRICE"},
                      signed=True)
            return "algo"
        except BinanceError as exc:
            log.info("algoOrder 손절 실패 (%s) → 예전 방식으로 시도", exc)
        self.order(symbol=symbol, side=side, type="STOP_MARKET", stopPrice=fmt(stop), closePosition="true",
                   workingType="CONTRACT_PRICE")
        return "order"
