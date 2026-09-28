"""눌림목 전략.

- 매수: 최근 급등(하루 +10% 이상)한 종목이 7일선을 터치하면, 장중 언제든 종목당 10만원 매수
        (동시 보유 최대 10종목. 매도한 종목은 일정 기간 재매수하지 않음)
- 매수 체결 즉시 토스증권 조건주문(SINGLE) 2건을 서버에 등록
    · 손절: 평균 체결가 -4.7% 도달 시 시장가 매도
    · 익절: 평균 체결가 +15% 도달 시 지정가(+15% 가격) 매도
  한쪽이 발동되면 봇이 반대쪽 조건주문을 취소한다.
  (토스 OCO 는 지정가만 지원해 '손절 시장가 + 익절 지정가' 조합이 불가능하므로 SINGLE 2건 사용)
- 보유 기간: 손절·익절이 10거래일 안에 안 걸리면 10거래일째 15:10 에 시장가 매도 (MAX_HOLD_DAYS)
- 봇의 가격 감시는 백업: 조건주문 등록 실패 등으로 서버 감시가 없는 경우에만 직접 매도
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Callable

from .broker import CONDITIONAL_DONE, TERMINAL_STATUSES, Broker, round_down_to_tick, round_up_to_tick
from .config import KST, Config
from .market_calendar import TradingDay
from .selector import PullbackParams, WatchItem, build_watchlist, entry_signal, parse_candles
from .state import Position, State, StateStore

log = logging.getLogger(__name__)

# 시장성 지정가 매수 시 현재가 대비 허용 슬리피지
BUY_LIMIT_SLIPPAGE = 0.005
# 조건주문 등록 실패 시 재시도 횟수 (초과하면 봇 가격 감시로만 대응)
MAX_ARM_FAILURES = 3
# 감시 목록에 올랐던 급등 종목을 랭킹에서 빠진 뒤에도 추적하는 기간(일)
SURGE_MEMORY_DAYS = 30


def _dec(value) -> float | None:
    return float(Decimal(value)) if value not in (None, "") else None


def _at(day: date, hhmm: str) -> datetime:
    hh, mm = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(hh, mm), KST)


def params_from_config(cfg: Config) -> PullbackParams:
    return PullbackParams(
        slot_budget=cfg.slot_budget,
        surge_pct=cfg.surge_pct,
        surge_lookback_days=cfg.surge_lookback_days,
        ma_period=cfg.ma_period,
        ma_max_break_pct=cfg.ma_max_break_pct,
        require_ma_rising=cfg.require_ma_rising,
        min_avg_trading_amount=cfg.min_avg_trading_amount,
        min_days_after_surge=cfg.min_days_after_surge,
        pullback_volume_ratio=cfg.pullback_volume_ratio,
    )


class PullbackStrategy:
    def __init__(
        self,
        cfg: Config,
        broker: Broker,
        store: StateStore,
        watchlist_builder=build_watchlist,
    ):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.build_watchlist = watchlist_builder
        self.state: State = store.load()
        self.watchlist: dict[str, WatchItem] = {}
        self.watch_date: date | None = None
        self._kospi_prev_close: tuple[date, float] | None = None
        self._tick_time: datetime | None = None

    def now(self) -> datetime:
        """현재 tick 시각 (tick 밖에서 호출되면 실제 현재 시각)."""
        return self._tick_time or datetime.now(KST)

    # ------------------------------------------------------------ prices
    def stop_price(self, entry: float) -> int:
        """손절 감시가: 진입가 -4.7% 이하의 가장 가까운 호가."""
        return round_down_to_tick(entry * (1 - self.cfg.stop_loss_pct / 100))

    def take_profit_price(self, entry: float) -> int:
        """익절 감시가 겸 지정가: 진입가 +15% 이상의 가장 가까운 호가."""
        return round_up_to_tick(entry * (1 + self.cfg.take_profit_pct / 100))

    # ------------------------------------------------------------ schedule
    def buy_window(self, day: TradingDay) -> tuple[datetime, datetime]:
        start, end = _at(day.today, self.cfg.buy_start), _at(day.today, self.cfg.buy_end)
        # 단축 운영일 등으로 종가 단일가가 더 일찍 시작하면 그 전까지만 매수
        if day.closing_auction_start:
            end = min(end, day.closing_auction_start)
        return start, end

    def tick(self, now: datetime, day: TradingDay) -> None:
        """스케줄러가 주기적으로 호출. 현재 시각에 해야 할 일을 판단해 실행한다."""
        if not day.is_open or now < day.market_open or now >= day.market_close:
            return
        self._tick_time = now
        self.sync_orders()
        self.count_hold_days(day.today)
        if self.cfg.max_hold_days > 0 and now >= _at(day.today, self.cfg.time_exit_time):
            self.time_exits()
        self.arm_protection(day.today)
        self.check_exits()

        start, end = self.buy_window(day)
        if start <= now < end:
            self.ensure_watchlist(day.today)
            self.scan_and_buy(day.today)

    # ----------------------------------------------------------- hold days
    def count_hold_days(self, today: date) -> None:
        """거래일이 바뀔 때마다 보유 일수 +1 (매수일 = 0일째)."""
        key = today.isoformat()
        changed = False
        for pos in self.state.positions.values():
            if pos.last_day != key:
                if pos.last_day:
                    pos.hold_days += 1
                pos.last_day = key
                changed = True
        if changed:
            self.store.save(self.state)

    def time_exits(self) -> None:
        """최대 보유 기간이 된 종목: 조건주문 취소 후 시장가 매도."""
        for pos in list(self.state.positions.values()):
            if pos.hold_days < self.cfg.max_hold_days:
                continue
            if pos.status == "SELLING" and pos.sell_reason in ("STOP_LOSS", "TIME_EXIT"):
                continue  # 이미 시장가 매도가 나가 있음
            log.info("보유 기간 만료 %s %s (%d거래일): 시장가 매도", pos.symbol, pos.name, pos.hold_days)
            self._sell_now(pos, "TIME_EXIT")

    # ----------------------------------------------------------------- buy
    def ensure_watchlist(self, today: date) -> None:
        """하루 한 번 급등 종목 감시 목록을 만든다 (실패하면 예외 → 다음 주기에 재시도)."""
        if self.watch_date == today:
            return
        horizon = today - timedelta(days=SURGE_MEMORY_DAYS)
        self.state.surge_seen = {
            s: d for s, d in self.state.surge_seen.items() if date.fromisoformat(d) >= horizon
        }
        self.watchlist = self.build_watchlist(
            self.broker.client, set(self.state.surge_seen), params_from_config(self.cfg), today
        )
        self.watch_date = today
        for symbol in self.watchlist:
            self.state.surge_seen[symbol] = today.isoformat()
        self.store.save(self.state)

    def market_allows_buy(self, today: date) -> bool:
        """시장 필터. kospi_down: 코스피 현재가 < 전일 종가일 때만 신규 매수 허용."""
        if self.cfg.market_filter != "kospi_down":
            return True
        client = self.broker.client
        if self._kospi_prev_close is None or self._kospi_prev_close[0] != today:
            bars = parse_candles(client.get_indicator_candles("KOSPI", "1d", 5))
            prev = [b for b in bars if b.day < today]
            if not prev:
                log.warning("코스피 전일 종가를 알 수 없어 매수 보류")
                return False
            self._kospi_prev_close = (today, prev[-1].close)
        prices = client.get_indicator_prices(["KOSPI"])
        if not prices:
            return False
        now_price = float(prices[0]["lastPrice"])
        prev_close = self._kospi_prev_close[1]
        return now_price < prev_close

    def in_cooldown(self, symbol: str, today: date) -> bool:
        for h in reversed(self.state.history):
            if h["symbol"] == symbol:
                closed = datetime.fromisoformat(h["closed_at"]).date()
                return (today - closed).days < self.cfg.rebuy_cooldown_days
        return False

    def scan_and_buy(self, today: date) -> list[str]:
        """감시 목록 종목의 현재가가 7일선 부근이면 매수. 매수한 종목 코드 목록 반환."""
        cfg = self.cfg
        slots = cfg.num_stocks - len(self.state.positions)
        if slots <= 0 or not self.watchlist:
            return []
        targets = [
            w for s, w in self.watchlist.items()
            if s not in self.state.positions and not self.in_cooldown(s, today)
        ]
        if not targets:
            return []
        prices = self.broker.last_prices([w.symbol for w in targets])
        params = params_from_config(cfg)
        signals = []
        for w in targets:
            price = prices.get(w.symbol)
            if price is None:
                continue
            ok, ma = entry_signal(w, float(price), params)
            if ok:
                signals.append((w, float(price), ma))
        if not signals:
            return []
        if not self.market_allows_buy(today):
            log.info("매수 신호 %d건 있으나 코스피가 하락 중이 아니어서 매수 보류", len(signals))
            return []
        signals.sort(key=lambda x: x[0].surge_pct, reverse=True)

        bought = []
        cash = self.broker.cash_buying_power()
        for w, price, ma in signals[:slots]:
            limit = round_up_to_tick(price * (1 + BUY_LIMIT_SLIPPAGE))
            qty = cfg.slot_budget // limit
            if qty <= 0:
                continue
            if qty * limit > cash:
                log.warning("매수 가능 금액 부족 (필요 %s, 가능 %s) - 매수 중단", qty * limit, cash)
                break
            log.info(
                "눌림목 매수 %s %s: 현재가 %s, %d일선 %.0f (%s +%.1f%% 급등)",
                w.symbol, w.name, f"{price:,.0f}", w.ma_period, ma, w.surge_date.strftime("%m/%d"), w.surge_pct * 100,
            )
            try:
                order_id = self.broker.buy_limit(w.symbol, qty, limit)
            except Exception as exc:
                log.error("%s 매수 주문 실패: %s", w.symbol, exc)
                continue
            cash -= qty * limit
            self.state.positions[w.symbol] = Position(
                symbol=w.symbol,
                name=w.name,
                opened_at=self.now().isoformat(timespec="seconds"),
                buy_order_id=order_id,
                buy_open=True,
                last_day=today.isoformat(),
            )
            bought.append(w.symbol)
            self.store.save(self.state)

        if bought:
            self.sync_orders()
            self.arm_protection(today)
        return bought

    # ------------------------------------------------- conditional orders
    def arm_protection(self, today: date) -> None:
        """매수 체결이 끝난 포지션에 손절·익절 조건주문을 건다 (이미 걸려 있으면 건너뜀)."""
        if not self.cfg.use_conditional_orders:
            return
        expire = (today + timedelta(days=self.cfg.conditional_expire_days)).isoformat()
        for pos in list(self.state.positions.values()):
            if pos.status != "OPEN" or pos.buy_open or pos.quantity <= 0 or pos.entry_price <= 0:
                continue
            if pos.stop_co_id is None and pos.stop_arm_failures < MAX_ARM_FAILURES:
                try:
                    pos.stop_co_id = self.broker.place_conditional_sell(
                        pos.symbol, pos.quantity, self.stop_price(pos.entry_price), expire
                    )
                except Exception as exc:
                    pos.stop_arm_failures += 1
                    log.error("%s 손절 조건주문 등록 실패(%d회): %s", pos.symbol, pos.stop_arm_failures, exc)
            if pos.tp_co_id is None and pos.tp_arm_failures < MAX_ARM_FAILURES:
                tp = self.take_profit_price(pos.entry_price)
                try:
                    pos.tp_co_id = self.broker.place_conditional_sell(
                        pos.symbol, pos.quantity, tp, expire, limit_price=tp
                    )
                except Exception as exc:
                    pos.tp_arm_failures += 1
                    log.error("%s 익절 조건주문 등록 실패(%d회): %s", pos.symbol, pos.tp_arm_failures, exc)
            self.store.save(self.state)

    def _check_conditionals(self, pos: Position) -> None:
        """조건주문 발동 여부 확인. 발동되면 생성된 주문을 매도주문으로 추적하고 반대쪽을 취소."""
        for leg, reason in (("stop", "STOP_LOSS"), ("tp", "TAKE_PROFIT")):
            co_id = getattr(pos, f"{leg}_co_id")
            if not co_id or pos.symbol not in self.state.positions:
                continue
            status, triggered = self.broker.conditional_status(co_id)
            if triggered:
                setattr(pos, f"{leg}_co_id", None)
                log.info("%s %s 조건주문 발동 -> 주문 %s", pos.symbol, reason, triggered)
                if leg == "stop":
                    # 손절 발동: 익절 조건주문 취소, 익절 지정가 주문이 대기 중이면 그것도 취소
                    self._cancel_conditional(pos, "tp")
                    if pos.status == "SELLING" and pos.sell_order_id and pos.sell_order_id != triggered:
                        self._abandon_sell_order(pos)
                # 익절 발동 시 손절 조건주문은 익절 체결 완료까지 유지 (지정가 미체결 대비)
                self._track_sell(pos, triggered, reason)
            elif status in CONDITIONAL_DONE:
                setattr(pos, f"{leg}_co_id", None)
        self.store.save(self.state)

    def _cancel_conditional(self, pos: Position, leg: str) -> None:
        co_id = getattr(pos, f"{leg}_co_id")
        if not co_id:
            return
        try:
            self.broker.cancel_conditional(co_id)
        except Exception as exc:
            log.warning("%s 조건주문 취소 실패(이미 완료됐을 수 있음): %s", pos.symbol, exc)
        setattr(pos, f"{leg}_co_id", None)
        # 취소 직전에 발동됐을 수 있으므로 확인
        try:
            _, triggered = self.broker.conditional_status(co_id)
        except Exception:
            triggered = None
        if triggered and triggered != pos.sell_order_id:
            log.warning("%s 조건주문이 취소 직전 발동됨 -> 주문 %s 취소", pos.symbol, triggered)
            try:
                self.broker.cancel(triggered)
            except Exception as exc:
                log.warning("%s 발동 주문 취소 실패: %s", pos.symbol, exc)
            reason = "STOP_LOSS" if leg == "stop" else "TAKE_PROFIT"
            self._record_fill(pos, self.broker.order_status(triggered), reason)

    def _cancel_all_conditionals(self, pos: Position) -> None:
        self._cancel_conditional(pos, "stop")
        self._cancel_conditional(pos, "tp")

    # --------------------------------------------------------------- exits
    def check_exits(self) -> None:
        """봇 가격 감시 (조건주문의 백업).

        - 손절: 서버에 손절 조건주문이 없는데 -4.7% 이하 → 즉시 시장가 매도
        - 익절: 서버에 익절 조건주문이 없는데 +15% 이상 → +15% 가격에 지정가 매도
        - 익절 지정가가 미체결인 채 -4.7% 까지 밀리고 손절 조건주문도 없으면 → 취소 후 시장가 매도
        """
        held = [p for p in self.state.positions.values() if p.quantity > 0 and p.entry_price > 0]
        if not held:
            return
        prices = self.broker.last_prices([p.symbol for p in held])
        for pos in held:
            price = prices.get(pos.symbol)
            if price is None or pos.symbol not in self.state.positions:
                continue
            stop, tp = self.stop_price(pos.entry_price), self.take_profit_price(pos.entry_price)
            if pos.status == "OPEN":
                if price <= stop and pos.stop_co_id is None:
                    log.info("손절(봇 감시) %s %s: 현재 %s ≤ %s", pos.symbol, pos.name, price, stop)
                    self._sell_now(pos, "STOP_LOSS")
                elif price >= tp and pos.tp_co_id is None:
                    log.info("익절(봇 감시) %s %s: 현재 %s ≥ %s", pos.symbol, pos.name, price, tp)
                    self._sell_limit(pos, tp, "TAKE_PROFIT")
            elif pos.status == "SELLING" and pos.sell_reason == "TAKE_PROFIT":
                if price <= stop and pos.stop_co_id is None:
                    log.info("익절 지정가 미체결 상태에서 손절가 도달 %s: 시장가 전환", pos.symbol)
                    self._sell_now(pos, "STOP_LOSS")

    def liquidate_all(self, reason: str) -> None:
        """수동 청산 (`python -m tossbot liquidate`): 조건주문·미체결 매도주문 취소 후 전량 시장가 매도."""
        for pos in list(self.state.positions.values()):
            if pos.status == "SELLING" and pos.sell_reason in (reason, "STOP_LOSS"):
                continue  # 이미 시장가 매도 주문이 나가 있음
            self._sell_now(pos, reason)

    def _sell_now(self, pos: Position, reason: str) -> None:
        """조건주문·대기 주문을 정리하고 시장가로 매도."""
        if pos.buy_open and pos.buy_order_id:
            try:
                self.broker.cancel(pos.buy_order_id)
            except Exception as exc:
                log.warning("%s 매수 잔량 취소 실패: %s", pos.symbol, exc)
            self._sync_buy(pos)
            if pos.symbol not in self.state.positions:
                return
        self._cancel_all_conditionals(pos)
        if pos.status == "SELLING" and pos.sell_order_id:
            if not self._abandon_sell_order(pos):
                # 취소 처리 중 → 다음 tick 에 잔량 매도
                self.store.save(self.state)
                return
        if pos.quantity <= 0:
            if not pos.buy_open:
                self.state.positions.pop(pos.symbol, None)
                self.store.save(self.state)
            return
        qty = min(pos.quantity, self.broker.sellable_quantity(pos.symbol, pos.quantity))
        if qty <= 0:
            log.warning("%s 매도 가능 수량 0 (대기 주문 취소 처리 중일 수 있음)", pos.symbol)
            return
        try:
            order_id = self.broker.sell_market(pos.symbol, qty)
        except Exception as exc:
            log.error("%s 매도 주문 실패 (%s): %s", pos.symbol, reason, exc)
            return
        self._track_sell(pos, order_id, reason)

    def _sell_limit(self, pos: Position, price: int, reason: str) -> None:
        self._cancel_conditional(pos, "tp")
        qty = min(pos.quantity, self.broker.sellable_quantity(pos.symbol, pos.quantity))
        if qty <= 0:
            return
        try:
            order_id = self.broker.sell_limit(pos.symbol, qty, price)
        except Exception as exc:
            log.error("%s 지정가 매도 실패 (%s): %s", pos.symbol, reason, exc)
            return
        self._track_sell(pos, order_id, reason)

    def _track_sell(self, pos: Position, order_id: str, reason: str) -> None:
        pos.sell_order_id = order_id
        pos.sell_reason = reason
        pos.status = "SELLING"
        self.store.save(self.state)
        self._sync_sell(pos)

    def _abandon_sell_order(self, pos: Position) -> bool:
        """대기 중인 매도주문 취소. 종료 상태가 되면 체결분을 기록하고 True, 아직 취소 처리 중이면 False."""
        order = self.broker.order_status(pos.sell_order_id)
        if order.get("status") not in TERMINAL_STATUSES:
            try:
                self.broker.cancel(pos.sell_order_id)
            except Exception as exc:
                log.warning("%s 매도주문 취소 실패: %s", pos.symbol, exc)
            order = self.broker.order_status(pos.sell_order_id)
            if order.get("status") not in TERMINAL_STATUSES:
                return False
        self._record_fill(pos, order, pos.sell_reason)
        pos.status, pos.sell_order_id = "OPEN", None
        if pos.quantity <= 0:
            self.state.positions.pop(pos.symbol, None)
        return True

    # ---------------------------------------------------------------- sync
    def sync_orders(self) -> None:
        for pos in list(self.state.positions.values()):
            try:
                if pos.buy_open:
                    self._sync_buy(pos)
                if pos.symbol in self.state.positions:
                    self._check_conditionals(pos)
                if pos.status == "SELLING" and pos.symbol in self.state.positions:
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
            else:
                log.info("%s 매수 체결 %d주 @ %s", pos.symbol, pos.quantity, pos.entry_price)
        self.store.save(self.state)

    def _record_fill(self, pos: Position, order: dict, reason: str | None) -> int:
        """매도 주문의 체결분을 이력에 남기고 보유 수량에서 차감. 체결 수량 반환."""
        ex = order.get("execution") or {}
        filled = _dec(ex.get("filledQuantity"))
        filled = int(filled) if filled is not None else (pos.quantity if order.get("status") == "FILLED" else 0)
        filled = min(filled, pos.quantity)
        if filled <= 0:
            return 0
        exit_price = _dec(ex.get("averageFilledPrice"))
        self.state.history.append(
            {
                **{k: v for k, v in asdict(pos).items() if k in ("symbol", "name", "entry_price", "opened_at")},
                "quantity": filled,
                "exit_price": exit_price,
                "reason": reason,
                "closed_at": self.now().isoformat(timespec="seconds"),
                "return_pct": round((exit_price / pos.entry_price - 1) * 100, 2)
                if exit_price and pos.entry_price
                else None,
            }
        )
        pos.quantity -= filled
        return filled

    def _sync_sell(self, pos: Position) -> None:
        order = self.broker.order_status(pos.sell_order_id)
        if order.get("status") not in TERMINAL_STATUSES:
            return
        self._record_fill(pos, order, pos.sell_reason)
        if pos.quantity <= 0:
            log.info("%s 매도 완료 (%s)", pos.symbol, pos.sell_reason)
            self._cancel_all_conditionals(pos)  # 남은 반대쪽 조건주문 정리
            self.state.positions.pop(pos.symbol, None)
        else:
            log.warning("%s 매도 잔량 %d 주 (%s) - 조건주문 재등록", pos.symbol, pos.quantity, order.get("status"))
            # 수량이 바뀌었으므로 남은 조건주문을 취소하고 잔량 기준으로 다시 건다
            self._cancel_all_conditionals(pos)
            pos.status, pos.sell_order_id = "OPEN", None
            pos.stop_arm_failures = pos.tp_arm_failures = 0
        self.store.save(self.state)
