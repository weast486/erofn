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
    return int(math.ceil(price / tick) * tick)


TERMINAL_STATUSES = {"FILLED", "CANCELED", "REJECTED", "REPLACED", "CANCEL_REJECTED", "REPLACE_REJECTED"}


class Broker:
    def __init__(self, client: TossClient, dry_run: bool = True):
        self.client = client
        self.dry_run = dry_run
        self._dry_orders: dict[str, dict] = {}

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

    def buy_limit(self, symbol: str, quantity: int, price: int) -> str:
        """지정가 매수 (현재가보다 약간 높은 가격을 주면 즉시 체결되는 '시장성 지정가')."""
        return self._order(symbol, "BUY", quantity, "LIMIT", price)

    def sell_market(self, symbol: str, quantity: int) -> str:
        return self._order(symbol, "SELL", quantity, "MARKET", None)

    def _order(self, symbol: str, side: str, quantity: int, order_type: str, price: int | None) -> str:
        client_order_id = f"tb-{side[0].lower()}-{uuid.uuid4().hex[:24]}"
        if self.dry_run:
            fill = self.last_prices([symbol])[symbol]
            order_id = f"DRY-{client_order_id}"
            self._dry_orders[order_id] = {
                "orderId": order_id,
                "symbol": symbol,
                "side": side,
                "status": "FILLED",
                "quantity": str(quantity),
                "execution": {"filledQuantity": str(quantity), "averageFilledPrice": str(fill)},
            }
            log.info("[DRY] %s %s x%d %s @ ~%s", side, symbol, quantity, order_type, fill)
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
            # 재시작 등으로 모의 주문 기록이 사라진 경우: 체결 정보 없이 FILLED 로 간주
            return self._dry_orders.get(order_id) or {"orderId": order_id, "status": "FILLED", "execution": {}}
        return self.client.get_order(order_id)

    def cancel(self, order_id: str) -> None:
        if order_id.startswith("DRY-"):
            return
        self.client.cancel_order(order_id)
