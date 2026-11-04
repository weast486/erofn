"""OKX USDT 무기한 선물 REST 클라이언트 (FVG 봇용, 2026-10-28).

봇(bot.py)은 바이낸스 클라이언트 모양으로 짜여 있어서, 이 클래스가 같은 메서드·같은 응답 모양을 흉내 낸다.
- 종목 이름: 봇 안에서는 바이낸스처럼 'NVDAUSDT', OKX 에는 'NVDA-USDT-SWAP' 으로 바꿔 보냄
- 수량: 봇은 주식 수(기초 자산 단위), OKX 는 계약 수(sz) → ctVal 로 바꿈
- 미국 주식·ETF 는 instCategory 3 → underlyingType 'EQUITY' 로 표시
- 계좌는 단방향(net) 포지션 모드·교차 마진(tdMode cross) 기준
키·패스프레이즈는 .env.okx 에서만 읽고 로그에 남기지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from .client import BinanceError, fmt

BASE = "https://www.okx.com"
log = logging.getLogger("binancebot")
UA = {"User-Agent": "Mozilla/5.0 (erofn bot)"}
IV_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}
OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "4h": "4H"}


class OkxError(BinanceError):
    """BinanceError 를 이어받아 봇의 except BinanceError 가 그대로 잡는다."""


def to_inst(sym: str) -> str:
    return sym[:-4] + "-USDT-SWAP" if sym.endswith("USDT") else sym


def to_sym(inst: str) -> str:
    return inst.replace("-USDT-SWAP", "USDT")


class OkxClient:
    name = "OKX"

    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = "", demo: bool = False,
                 base: str = BASE, session=None):
        import requests

        self.key, self.secret, self.passphrase, self.demo, self.base = api_key, api_secret, passphrase, demo, base
        self.s = session or requests.Session()
        self.offset_ms = 0
        self.inst: dict[str, dict] = {}   # 'NVDAUSDT' → OKX 종목 정보

    # ------------------------------------------------------------ 기본 요청
    def _ts(self) -> str:
        t = datetime.fromtimestamp(time.time() + self.offset_ms / 1000, timezone.utc)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"

    def _req(self, method: str, path: str, params=None, signed: bool = False, tries: int = 5):
        params = params or {}
        for i in range(tries):
            headers = dict(UA)
            if method == "GET":
                q = {k: v for k, v in params.items() if v is not None and v != ""}
                req_path = path + ("?" + urlencode(q) if q else "")
                body = ""
            else:
                req_path, body = path, json.dumps(params)
                headers["Content-Type"] = "application/json"
            if signed:
                ts = self._ts()
                sign = hmac.new(self.secret.encode(), (ts + method + req_path + body).encode(), hashlib.sha256).digest()
                headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": base64.b64encode(sign).decode(),
                                "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase})
            if self.demo:
                headers["x-simulated-trading"] = "1"
            try:
                r = self.s.request(method, self.base + req_path, headers=headers, data=body or None, timeout=15)
            except Exception as exc:  # noqa: BLE001
                if method != "GET":  # 주문은 다시 보내면 두 번 체결될 수 있음 → 봇이 포지션으로 확인
                    raise OkxError(408, None, f"응답 없음 ({exc})") from exc
                if i == tries - 1:
                    raise
                log.warning("OKX 네트워크 오류, 재시도 (%s)", exc)
                time.sleep(1 + i)
                continue
            try:
                data = r.json()
            except ValueError:
                data = {"code": str(r.status_code), "msg": r.text[:200]}
            code = str(data.get("code", "")) if isinstance(data, dict) else ""
            if r.status_code == 429 or code == "50011":  # 요청 제한
                time.sleep(1 + i)
                continue
            if code == "50102" and i < tries - 1:  # 시각 차이 → 맞추고 다시
                self.sync_time()
                continue
            if (r.status_code >= 500 or code in ("50001", "50004", "50013")) and method == "GET" and i < tries - 1:
                log.warning("OKX 서버 응답 지연 (%s %s), %d초 뒤 다시", r.status_code, code, 2 * (i + 1))
                time.sleep(2 * (i + 1))
                continue
            if r.status_code >= 400 or code not in ("0", ""):
                d0 = (data.get("data") or [{}])[0] if isinstance(data, dict) and isinstance(data.get("data"), list) else {}
                scode, smsg = (d0 or {}).get("sCode"), (d0 or {}).get("sMsg")
                raise OkxError(r.status_code, scode or code, smsg or data.get("msg", ""))
            return data.get("data", [])
        raise OkxError(0, None, "재시도 초과")

    def sync_time(self) -> None:
        t0 = time.time() * 1000
        server = int(self._req("GET", "/api/v5/public/time")[0]["ts"])
        self.offset_ms = int(server - (t0 + time.time() * 1000) / 2)

    # ------------------------------------------------------------ 종목 정보
    def exchange_info(self) -> dict:
        out = []
        for x in self._req("GET", "/api/v5/public/instruments", {"instType": "SWAP"}):
            if x.get("settleCcy") != "USDT" or not x["instId"].endswith("-USDT-SWAP"):
                continue
            sym = to_sym(x["instId"])
            self.inst[sym] = x
            ct, lot = float(x.get("ctVal") or 1), float(x.get("lotSz") or 1)
            pct = float(x.get("floatPxLmtPct") or 0)
            out.append({"symbol": sym, "status": "TRADING" if x.get("state") == "live" else x.get("state", ""),
                        "quoteAsset": "USDT", "underlyingType": "EQUITY" if x.get("instCategory") == "3" else "",
                        "filters": [{"filterType": "PRICE_FILTER", "tickSize": x.get("tickSz", "0")},
                                    {"filterType": "LOT_SIZE", "stepSize": fmt(lot * ct)},
                                    {"filterType": "PERCENT_PRICE", "multiplierUp": str(1 + pct),
                                     "multiplierDown": str(1 - pct)}]})
        return {"symbols": out}

    def _ct(self, sym: str) -> float:
        if sym not in self.inst:
            self.exchange_info()
        return float(self.inst.get(sym, {}).get("ctVal") or 1)

    def _sz(self, sym: str, qty: float) -> str:
        """주식 수 → 계약 수 (lotSz 단위로 내림)."""
        x = self.inst.get(sym) or {}
        lot = float(x.get("lotSz") or 1)
        n = int(qty / self._ct(sym) / lot + 1e-9) * lot
        return fmt(round(n, 10))

    # ------------------------------------------------------------ 시세 (키 필요 없음)
    def klines(self, symbol: str, start_ms: int, limit: int = 500, interval: str = "1m") -> list[list]:
        """바이낸스 klines 모양으로: [시작ms, 시, 고, 저, 종, 거래량(주), 끝ms, 거래대금(USDT)] (start_ms 부터 limit 개, 오래된 순)."""
        iv = IV_MS[interval]
        now = int(time.time() * 1000)
        end = min(start_ms + limit * iv, now + iv)
        recent = start_ms >= now - 1400 * iv
        path, page = ("/api/v5/market/candles", 300) if recent else ("/api/v5/market/history-candles", 100)
        rows: dict[int, list] = {}
        cursor = end
        for _ in range(200):
            data = self._req("GET", path, {"instId": to_inst(symbol), "bar": OKX_BAR[interval], "after": cursor,
                                           "limit": page})
            if not data:
                break
            for k in data:
                ts = int(k[0])
                if ts >= start_ms:
                    rows[ts] = [ts, k[1], k[2], k[3], k[4], k[6], ts + iv - 1, k[7]]
            oldest = min(int(k[0]) for k in data)
            if oldest <= start_ms or oldest >= cursor:
                break
            cursor = oldest
            time.sleep(0.06)
        return [rows[t] for t in sorted(rows)][:limit]

    def prices(self) -> dict[str, float]:
        return {to_sym(r["instId"]): float(r["last"]) for r in self._req("GET", "/api/v5/market/tickers", {"instType": "SWAP"})
                if r.get("instId", "").endswith("-USDT-SWAP") and r.get("last")}

    # ------------------------------------------------------------ 계좌 (키 필요)
    def config(self) -> dict:
        return (self._req("GET", "/api/v5/account/config", signed=True) or [{}])[0]

    def equity(self) -> float:
        """USDT 평가금액(잔고 + 미실현 손익)."""
        acc = (self._req("GET", "/api/v5/account/balance", {"ccy": "USDT"}, signed=True) or [{}])[0]
        for d in acc.get("details", []):
            if d.get("ccy") == "USDT":
                return float(d.get("eq") or 0)
        return float(acc.get("totalEq") or 0)

    def hedge_mode(self) -> bool:
        return self.config().get("posMode") == "long_short_mode"

    def position_amt(self, symbol: str) -> tuple[float, float]:
        """(주식 수, 평균 진입가). 롱 +, 숏 -."""
        ct = self._ct(symbol)
        for p in self._req("GET", "/api/v5/account/positions", {"instType": "SWAP", "instId": to_inst(symbol)}, signed=True):
            if p.get("instId") == to_inst(symbol) and p.get("pos") not in ("", None):
                pos = float(p["pos"])
                if p.get("posSide") == "short":
                    pos = -abs(pos)
                return pos * ct, float(p.get("avgPx") or 0)
        return 0.0, 0.0

    def fills(self, symbol: str, start_ms: int | None = None, order_id=None) -> list[dict]:
        ct = self._ct(symbol)
        q = {"instType": "SWAP", "instId": to_inst(symbol), "ordId": order_id, "limit": 100}
        if start_ms:
            q["begin"] = int(start_ms)
        return [{"side": f["side"].upper(), "price": f["fillPx"], "qty": float(f["fillSz"]) * ct, "time": int(f["ts"]),
                 "orderId": f.get("ordId")} for f in self._req("GET", "/api/v5/trade/fills", q, signed=True)]

    def set_leverage(self, symbol: str, leverage: int) -> None:
        mx = int(float(self.inst.get(symbol, {}).get("lever") or leverage))
        self._req("POST", "/api/v5/account/set-leverage",
                  {"instId": to_inst(symbol), "lever": str(min(int(leverage), mx)), "mgnMode": "cross"}, signed=True)

    def set_margin_type(self, symbol: str, margin: str = "CROSSED") -> None:
        """OKX 는 주문마다 tdMode=cross 로 정함 → 할 일 없음."""

    def order(self, **p) -> dict:
        """바이낸스식 인자(symbol, side, type, timeInForce, quantity, price, reduceOnly)를 OKX 주문으로."""
        sym = p["symbol"]
        typ, tif = p.get("type", "LIMIT"), p.get("timeInForce", "GTC")
        ord_type = "market" if typ == "MARKET" else ("ioc" if tif == "IOC" else "limit")
        body = {"instId": to_inst(sym), "tdMode": "cross", "side": p["side"].lower(), "ordType": ord_type,
                "sz": self._sz(sym, float(p["quantity"]))}
        if ord_type != "market":
            body["px"] = str(p["price"])
        if str(p.get("reduceOnly", "")).lower() == "true":
            body["reduceOnly"] = True
        if float(body["sz"]) <= 0:
            raise OkxError(400, "sz", f"수량 {p['quantity']} 이 최소 계약 단위보다 작음")
        ord_id = self._req("POST", "/api/v5/trade/order", body, signed=True)[0]["ordId"]
        if ord_type == "limit":
            return {"orderId": ord_id, "status": "NEW"}
        o: dict = {}
        for _ in range(10):  # IOC·시장가: 끝날 때까지(체결 또는 남은 수량 취소) 확인해 체결 수량·평균가를 돌려줌
            o = self.query(sym, ord_id)
            if o.get("state") in ("filled", "canceled", "mmp_canceled"):
                break
            time.sleep(0.3)
        ct = self._ct(sym)
        return {"orderId": ord_id, "executedQty": str(float(o.get("accFillSz") or 0) * ct),
                "avgPrice": o.get("avgPx") or "0", "status": (o.get("state") or "").upper()}

    def query(self, symbol: str, ord_id: str) -> dict:
        return (self._req("GET", "/api/v5/trade/order", {"instId": to_inst(symbol), "ordId": ord_id}, signed=True) or [{}])[0]

    def cancel_all(self, symbol: str) -> None:
        inst = to_inst(symbol)
        try:
            pend = self._req("GET", "/api/v5/trade/orders-pending", {"instType": "SWAP", "instId": inst}, signed=True)
            if pend:
                self._req("POST", "/api/v5/trade/cancel-batch-orders",
                          [{"instId": inst, "ordId": o["ordId"]} for o in pend], signed=True)
        except OkxError as exc:
            log.info("%s 주문 취소 응답: %s", symbol, exc)
        try:
            algos = self._req("GET", "/api/v5/trade/orders-algo-pending", {"ordType": "conditional", "instId": inst},
                              signed=True)
            if algos:
                self._req("POST", "/api/v5/trade/cancel-algos",
                          [{"instId": inst, "algoId": a["algoId"]} for a in algos], signed=True)
        except OkxError as exc:
            log.info("%s 손절 주문 취소 응답: %s", symbol, exc)

    def stop_market(self, symbol: str, side: str, stop: float) -> str:
        """거래소 손절: 조건부 주문(마지막 체결가가 stop 에 닿으면 포지션 전량 시장가)."""
        self._req("POST", "/api/v5/trade/order-algo",
                  {"instId": to_inst(symbol), "tdMode": "cross", "side": side.lower(), "ordType": "conditional",
                   "slTriggerPx": fmt(stop), "slOrdPx": "-1", "slTriggerPxType": "last", "closeFraction": "1",
                   "reduceOnly": True}, signed=True)
        return "algo"
