"""주문 실행 계층. DRY_RUN 이면 시세·계좌는 실제로 조회하되 주문은 모의 체결한다."""
from __future__ import annotations

import logging
import math
import uuid
from decimal import Decimal

from .client import TossClient

log = logging.getLogger(__name__)

# KRX 호가가격단위 (2023.01 개편 이후 코스피·코스닥 공통)
_TICKS = [(2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50), (200_000, 100), (500_000, 500)]


def tick_size(price: float) -> int:
    for upper, tick in _TICKS:
        if price < upper:
            return tick
    return 1_000


def round_up_to_tick(price: float) -> int:
    tick = tick_size(price)
    return int(math.ceil(round(price / tick, 9)) * tick)


def round_down_to_tick(price: float) -> int:
    tick = tick_size(price)
    return int(math.floor(round(price / tick, 9)) * tick)


TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}
# 조건주문(그룹) 상태 중 더 이상 감시하지 않는 상태
CONDITIONAL_DONE = {"COMPLETED", "EXPIRED"}


class Broker:
    def __init__(self, client: TossClient, dry_run: bool = True):
        self.client = client
        self.dry_run = dry_run
        self._dry_orders: dict[str, dict] = {}
        self._dry_conditionals: dict[str, dict] = {}

    def last_prices(self, symbols: list[str]) -> dict[str, Decimal]:
        if not symbols:
            return {}
        return {p["symbol"]: Decimal(p["lastPrice"]) for p in self.client.get_prices(symbols)}

    def cash_buying_power(self) -> int:
        if self.dry_run:
            return 10**12  # 모의 매매: 예수금 제한 없음 (TOTAL_BUDGET 으로만 제한)
        return int(Decimal(self.client.get_buying_power("KRW")["cashBuyingPower"]))

    def sellable_quantity(self, symbol: str, fallback: int) -> int:
        if self.dry_run:
            return fallback
        return int(Decimal(self.client.get_sellable_quantity(symbol)["sellableQuantity"]))

    # -------------------------------------------------------------- orders
    def buy_limit(self, symbol: str, quantity: int, price: int) -> str:
        """지정가 매수 (현재가보다 약간 높은 가격을 주면 즉시 체결되는 '시장성 지정가')."""
        return self._order(symbol, "BUY", quantity, "LIMIT", price)

    def sell_market(self, symbol: str, quantity: int) -> str:
        return self._order(symbol, "SELL", quantity, "MARKET", None)

    def sell_limit(self, symbol: str, quantity: int, price: int) -> str:
        return self._order(symbol, "SELL", quantity, "LIMIT", price)

    def _order(self, symbol: str, side: str, quantity: int, order_type: str, price: int | None) -> str:
        client_order_id = f"tb-{side[0].lower()}-{uuid.uuid4().hex[:24]}"
        if self.dry_run:
            order_id = f"DRY-{client_order_id}"
            self._dry_orders[order_id] = {
                "orderId": order_id, "symbol": symbol, "side": side, "orderType": order_type,
                "price": price, "quantity": quantity, "status": "PENDING", "execution": {},
            }
            log.info("[DRY] %s %s x%d %s%s", side, symbol, quantity, order_type, f" @{price}" if price else "")
            self._dry_try_fill(self._dry_orders[order_id])
            return order_id
        result = self.client.create_order(
            symbol, side, quantity, order_type=order_type, price=price, client_order_id=client_order_id
        )
        log.info(
            "주문 접수 %s %s x%d %s%s -> orderId=%s",
            side, symbol, quantity, order_type, f" @{price}" if price else "", result["orderId"],
        )
        return result["orderId"]

    def order_status(self, order_id: str) -> dict:
        if order_id.startswith("DRY-"):
            order = self._dry_orders.get(order_id)
            if order is None:
                # 재시작 등으로 모의 주문 기록이 사라진 경우: 체결 정보 없이 FILLED 로 간주
                return {"orderId": order_id, "status": "FILLED", "execution": {}}
            self._dry_try_fill(order)
            return order
        return self.client.get_order(order_id)

    def cancel(self, order_id: str) -> None:
        if order_id.startswith("DRY-"):
            order = self._dry_orders.get(order_id)
            if order and order["status"] == "PENDING":
                order["status"] = "CANCELED"
            return
        self.client.cancel_order(order_id)

    def _dry_try_fill(self, order: dict) -> None:
        if order["status"] != "PENDING":
            return
        last = self.last_prices([order["symbol"]])[order["symbol"]]
        price = order["price"]
        marketable = (
            order["orderType"] == "MARKET"
            or (order["side"] == "BUY" and last <= price)
            or (order["side"] == "SELL" and last >= price)
        )
        if marketable:
            fill = last if order["orderType"] == "MARKET" or order["side"] == "BUY" else max(last, Decimal(price))
            order["status"] = "FILLED"
            order["execution"] = {"filledQuantity": str(order["quantity"]), "averageFilledPrice": str(fill)}

    # --------------------------------------------------- conditional orders
    def place_conditional_sell(
        self, symbol: str, quantity: int, trigger_price: int, expire_date: str, limit_price: int | None = None
    ) -> str:
        """조건 매도주문(SINGLE). limit_price 가 없으면 조건 충족 시 시장가, 있으면 지정가로 매도."""
        order_type = "LIMIT" if limit_price is not None else "MARKET"
        client_order_id = f"tb-c-{uuid.uuid4().hex[:24]}"
        desc = f"{symbol} x{quantity} 감시가 {trigger_price:,} → {order_type}{f' @{limit_price:,}' if limit_price else ''} (만료 {expire_date})"
        if self.dry_run:
            co_id = f"DRY-{client_order_id}"
            self._dry_conditionals[co_id] = {
                "conditionalOrderId": co_id, "symbol": symbol, "quantity": quantity, "orderType": order_type,
                "triggerPrice": trigger_price, "orderPrice": limit_price, "status": "WATCHING",
                "direction": "DOWN" if limit_price is None else "UP", "triggeredOrderId": None,
            }
            log.info("[DRY] 조건주문 등록 %s", desc)
            return co_id
        result = self.client.create_conditional_order(
            symbol, quantity, "SELL", trigger_price, expire_date,
            order_type=order_type, order_price=limit_price, client_order_id=client_order_id,
        )
        log.info("조건주문 등록 %s -> %s", desc, result["conditionalOrderId"])
        return result["conditionalOrderId"]

    def conditional_status(self, co_id: str) -> tuple[str, str | None]:
        """(조건주문 상태, 발동으로 생성된 주문 ID 또는 None)."""
        if co_id.startswith("DRY-"):
            co = self._dry_conditionals.get(co_id)
            if co is None:
                return "EXPIRED", None
            if co["status"] == "WATCHING":
                last = self.last_prices([co["symbol"]])[co["symbol"]]
                hit = last <= co["triggerPrice"] if co["direction"] == "DOWN" else last >= co["triggerPrice"]
                if hit:
                    order_type = co["orderType"]
                    co["triggeredOrderId"] = self._order(
                        co["symbol"], "SELL", co["quantity"], order_type, co["orderPrice"]
                    )
                    co["status"] = "ORDERED"
            return co["status"], co["triggeredOrderId"]
        co = self.client.get_conditional_order(co_id)
        return co.get("status", ""), (co.get("first") or {}).get("triggeredOrderId")

    def cancel_conditional(self, co_id: str) -> None:
        if co_id.startswith("DRY-"):
            co = self._dry_conditionals.get(co_id)
            if co and co["status"] == "WATCHING":
                co["status"] = "EXPIRED"
            return
        self.client.cancel_conditional_order(co_id)
        log.info("조건주문 취소 %s", co_id)
