"""주간 스윙 전략.

- 매수: 매주 화요일(휴장·연휴 전날이면 그 주 다음 거래일) 정규장 시작 N분 후,
        선정 종목 10개를 종목당 동일 금액(10만원)으로 매수
- 매도: 평균 체결가 대비 -5% 손절 / +15% 익절 (장중 주기적으로 감시)
- 청산: 금요일 또는 공휴일 전날, 종가 단일가 시작 N분 전 봇 보유 종목 전량 매도
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal

from .broker import TERMINAL_STATUSES, Broker, round_up_to_tick
from .config import KST, Config
from .market_calendar import TradingDay
from .selector import Candidate, load_universe, select_stocks
from .state import Position, State, StateStore

log = logging.getLogger(__name__)

# 시장성 지정가 매수 시 현재가 대비 허용 슬리피지
BUY_LIMIT_SLIPPAGE = 0.005


def week_key(d) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _dec(value) -> float | None:
    return float(Decimal(value)) if value not in (None, "") else None


class WeeklyStrategy:
    def __init__(self, cfg: Config, broker: Broker, store: StateStore, selector=select_stocks):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.select = selector
        self.state: State = store.load()

    # ------------------------------------------------------------ schedule
    def buy_time(self, day: TradingDay) -> datetime:
        return day.market_open + timedelta(minutes=self.cfg.buy_delay_minutes)

    def liquidation_time(self, day: TradingDay) -> datetime:
        anchor = day.closing_auction_start or (day.market_close - timedelta(minutes=10))
        return anchor - timedelta(minutes=self.cfg.liquidation_lead_minutes)

    def tick(self, now: datetime, day: TradingDay) -> None:
        """스케줄러가 주기적으로 호출. 현재 시각에 해야 할 일을 판단해 실행한다."""
        if not day.is_open or now < day.market_open or now >= day.market_close:
            return
        self.sync_orders()

        liq_time = self.liquidation_time(day)
        if day.is_liquidation_day and now >= liq_time:
            self.liquidate_all("WEEKLY_CLOSE" if day.today.weekday() == 4 else "PRE_HOLIDAY_CLOSE")
            return

        self.check_exits()

        if (
            day.is_buy_day
            and self.state.last_buy_week != week_key(day.today)
            and self.buy_time(day) <= now < liq_time
        ):
            self.buy_weekly(day)

    # ----------------------------------------------------------------- buy
    def buy_weekly(self, day: TradingDay) -> list[Candidate]:
        cfg = self.cfg
        slots = cfg.num_stocks - len(self.state.positions)
        if slots <= 0:
            log.info("이미 %d 종목 보유 중, 추가 매수 없음", len(self.state.positions))
            self.state.last_buy_week = week_key(day.today)
            self.store.save(self.state)
            return []

        universe = [s for s in load_universe(cfg.universe_file) if s not in self.state.positions]
        picks = self.select(
            self.broker.client,
            universe,
            top_n=slots,
            slot_budget=cfg.slot_budget,
            min_avg_traded_value=cfg.min_avg_traded_value,
            max_5d_return_pct=cfg.max_5d_return_pct,
            today=day.today,
        )
        log.info("이번 주 선정 종목: %s", ", ".join(f"{c.name}({c.symbol})" for c in picks) or "없음")

        prices = self.broker.last_prices([c.symbol for c in picks])
        cash = self.broker.cash_buying_power()
        for cand in picks:
            last = float(prices.get(cand.symbol, cand.close))
            limit = round_up_to_tick(last * (1 + BUY_LIMIT_SLIPPAGE))
            qty = cfg.slot_budget // limit
            if qty <= 0:
                log.info("%s: 1주 가격(%s)이 종목당 예산 초과, 건너뜀", cand.symbol, limit)
                continue
            if qty * limit > cash:
                log.warning("매수 가능 금액 부족 (필요 %s, 가능 %s) - 매수 중단", qty * limit, cash)
                break
            try:
                order_id = self.broker.buy_limit(cand.symbol, qty, limit)
            except Exception as exc:
                log.error("%s 매수 주문 실패: %s", cand.symbol, exc)
                continue
            cash -= qty * limit
            self.state.positions[cand.symbol] = Position(
                symbol=cand.symbol,
                name=cand.name,
                opened_at=datetime.now(KST).isoformat(timespec="seconds"),
                buy_order_id=order_id,
                buy_open=True,
            )
            self.store.save(self.state)

        self.state.last_buy_week = week_key(day.today)
        self.store.save(self.state)
        self.sync_orders()
        return picks

    # --------------------------------------------------------------- exits
    def check_exits(self) -> None:
        held = [p for p in self.state.positions.values() if p.status == "OPEN" and p.quantity > 0]
        if not held:
            return
        prices = self.broker.last_prices([p.symbol for p in held])
        # 부동소수점 오차로 경계값(정확히 -5%/+15%)을 놓치지 않도록 Decimal 로 비교
        sl = -Decimal(str(self.cfg.stop_loss_pct)) / 100
        tp = Decimal(str(self.cfg.take_profit_pct)) / 100
        for pos in held:
            price = prices.get(pos.symbol)
            if price is None or pos.entry_price <= 0:
                continue
            ret = price / Decimal(str(pos.entry_price)) - 1
            if ret <= sl:
                log.info("손절 %s %s: %.2f%% (진입 %s → 현재 %s)", pos.symbol, pos.name, ret * 100, pos.entry_price, price)
                self._sell(pos, "STOP_LOSS")
            elif ret >= tp:
                log.info("익절 %s %s: %.2f%% (진입 %s → 현재 %s)", pos.symbol, pos.name, ret * 100, pos.entry_price, price)
                self._sell(pos, "TAKE_PROFIT")

    def liquidate_all(self, reason: str) -> None:
        for pos in list(self.state.positions.values()):
            if pos.status == "OPEN":
                if pos.quantity > 0 or pos.buy_open:
                    self._sell(pos, reason)

    def _sell(self, pos: Position, reason: str) -> None:
        # 아직 진행 중인 매수 주문이 있으면 먼저 취소 (부분 체결 잔량 등)
        if pos.buy_open and pos.buy_order_id:
            try:
                self.broker.cancel(pos.buy_order_id)
            except Exception as exc:
                log.warning("%s 매수 잔량 취소 실패: %s", pos.symbol, exc)
            self._sync_buy(pos)
        if pos.quantity <= 0:
            if not pos.buy_open:
                self.state.positions.pop(pos.symbol, None)
                self.store.save(self.state)
            return
        qty = min(pos.quantity, self.broker.sellable_quantity(pos.symbol, pos.quantity))
        if qty <= 0:
            log.warning("%s 매도 가능 수량 0", pos.symbol)
            return
        try:
            pos.sell_order_id = self.broker.sell_market(pos.symbol, qty)
        except Exception as exc:
            log.error("%s 매도 주문 실패 (%s): %s", pos.symbol, reason, exc)
            return
        pos.status = "SELLING"
        pos.sell_reason = reason
        self.store.save(self.state)
        self._sync_sell(pos)

    # ---------------------------------------------------------------- sync
    def sync_orders(self) -> None:
        for pos in list(self.state.positions.values()):
            try:
                if pos.buy_open:
                    self._sync_buy(pos)
                if pos.status == "SELLING":
                    self._sync_sell(pos)
            except Exception as exc:
                log.warning("%s 주문 상태 조회 실패: %s", pos.symbol, exc)

    def _sync_buy(self, pos: Position) -> None:
        order = self.broker.order_status(pos.buy_order_id)
        ex = order.get("execution") or {}
        filled = _dec(ex.get("filledQuantity"))
        avg = _dec(ex.get("averageFilledPrice"))
        if filled is not None:
            pos.quantity = int(filled)
        if avg:
            pos.entry_price = avg
        if order.get("status") in TERMINAL_STATUSES:
            pos.buy_open = False
            if pos.quantity <= 0:
                log.info("%s 매수 미체결 종료 (%s)", pos.symbol, order.get("status"))
                self.state.positions.pop(pos.symbol, None)
        self.store.save(self.state)

    def _sync_sell(self, pos: Position) -> None:
        order = self.broker.order_status(pos.sell_order_id)
        if order.get("status") not in TERMINAL_STATUSES:
            return
        ex = order.get("execution") or {}
        filled = _dec(ex.get("filledQuantity"))
        filled = pos.quantity if filled is None else int(filled)
        exit_price = _dec(ex.get("averageFilledPrice"))
        if filled > 0:
            self.state.history.append(
                {
                    **{k: v for k, v in asdict(pos).items() if k in ("symbol", "name", "entry_price", "opened_at")},
                    "quantity": filled,
                    "exit_price": exit_price,
                    "reason": pos.sell_reason,
                    "closed_at": datetime.now(KST).isoformat(timespec="seconds"),
                    "return_pct": round((exit_price / pos.entry_price - 1) * 100, 2)
                    if exit_price and pos.entry_price
                    else None,
                }
            )
        remaining = pos.quantity - filled
        if remaining <= 0:
            log.info("%s 매도 완료 (%s)", pos.symbol, pos.sell_reason)
            self.state.positions.pop(pos.symbol, None)
        else:
            log.warning("%s 매도 잔량 %d 주 (%s) - 다음 주기에 재시도", pos.symbol, remaining, order.get("status"))
            pos.quantity = remaining
            pos.status = "OPEN"
            pos.sell_order_id = None
        self.store.save(self.state)
