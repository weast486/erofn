"""토스 계좌 한 종목 스플릿(분할) 매매 — 손절 없음 (2026-10-28 사용자 요청, KODEX 코스닥150 229200).

규칙 (SPLIT_* 설정):
- k차 매수가 = 시작가 - 간격 x (k-1), 차수마다 SPLIT_QTY 주 (SPLIT_BUDGET 이 있으면 차수마다 그 금액 안에서
  살 수 있는 만큼 = 금액 // 매수가 주, 1주도 못 사면 그 차수는 건너뜀), 최대 SPLIT_LEVELS 차수
- 차수마다 자기 매수가 + 간격 에 지정가 익절, 판 차수는 다시 내려오면 또 삼
- 손절 없음 (마지막 차수까지 사면 익절 주문만 걸어 두고 기다림)
- 모든 차수가 팔리면 그 뒤 고점 - SPLIT_REENTRY_DROP % 에서 1차를 다시 사고(시장성 지정가),
  그 체결가를 새 시작가로, 간격 = 새 시작가 x SPLIT_GAP_PCT % (호가 단위로 반올림)

주문은 한쪽에 하나씩만 걸어 둔다: 매수 = 아직 안 산 차수 중 가장 높은 가격(보유 중 가장 낮은 차수 바로 아래),
매도 = 보유 중 가장 낮은 가격 차수. 체결될 때마다 다음 주문으로 바꾼다. 이미 지나친 가격의 지정가는
바로 체결되므로(시장성 지정가) 크게 움직여도 다음 확인 때 따라잡는다.
새 주문은 정규장(09:00 ~ 마감 10분 전)에만 내고, 체결·취소 확인은 언제든 한다 (DAY 주문은 장이 끝나면 사라짐).

봇 포지션(state.json)과는 따로 state/split_종목코드.json 에 기록한다. 봇이 같은 종목을 사면 섞이므로 그때는 멈춘다.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta
from decimal import Decimal
from pathlib import Path

from .broker import Broker, TERMINAL_STATUSES, round_down_to_tick, round_up_to_tick, tick_size
from .config import _bool, _get
from .market_calendar import TradingDay

log = logging.getLogger(__name__)

GONE_STATUSES = TERMINAL_STATUSES | {"EXPIRED"}
LAST_ORDER_BEFORE_CLOSE = timedelta(minutes=10)  # 마감 직전(동시호가)에는 새 주문을 내지 않음


@dataclass
class SplitConfig:
    enabled: bool = False
    symbol: str = "229200"
    start: int = 15540  # 1차 매수가 (처음 상태를 만들 때만 씀)
    gap: int = 390  # 차수 간격(원) (처음 상태를 만들 때만 씀)
    levels: int = 10
    qty: int = 1  # 차수당 주식 수 (budget 이 0 일 때)
    budget: int = 0  # 차수당 매수 금액 상한(원), 0 = qty 주씩
    init_held: int = 8  # 처음 상태를 만들 때 이미 산 것으로 볼 차수 (1~N차)
    gap_pct: float = 2.5  # 재진입 뒤 새 간격 = 새 시작가 x N %
    reentry_drop: float = 2.0  # 모든 차수 익절 뒤 고점 -N% 에서 재진입
    reentry: bool = True  # false = 모든 차수가 팔리면 끝 (재진입 안 함)

    @classmethod
    def from_env(cls) -> "SplitConfig":
        def num(name: str, attr: str, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            enabled=_bool(os.environ.get("SPLIT_ENABLED"), cls.enabled),
            symbol=_get("SPLIT_SYMBOL", cls.symbol),
            start=num("SPLIT_START", "start", int),
            gap=num("SPLIT_GAP", "gap", int),
            levels=num("SPLIT_LEVELS", "levels", int),
            qty=num("SPLIT_QTY", "qty", int),
            budget=num("SPLIT_BUDGET", "budget", int),
            init_held=num("SPLIT_INIT_HELD", "init_held", int),
            gap_pct=num("SPLIT_GAP_PCT", "gap_pct"),
            reentry_drop=num("SPLIT_REENTRY_DROP", "reentry_drop"),
            reentry=_bool(os.environ.get("SPLIT_REENTRY"), cls.reentry),
        )


@dataclass
class Lot:
    level: int
    price: int  # 이 차수의 매수 기준가
    qty: int = 0  # 보유 수량 (0 = 안 삼)
    cost: float = 0.0  # 실제 평균 체결가 (기록용)
    gap: int = 0  # 이 차수의 익절 폭(원), 0 = 상태의 gap (간격을 바꾸기 전에 만든 차수는 원래 폭 유지)


@dataclass
class SplitState:
    symbol: str
    start: int = 0
    gap: int = 0
    cycle: int = 1
    lots: list[Lot] = field(default_factory=list)
    buy_order: dict | None = None  # {"id", "level", "price", "qty"}
    sell_order: dict | None = None
    peak: float = 0.0  # 모든 차수가 팔린 뒤 고점 (재진입 기준)
    paused: str = ""  # 비어 있지 않으면 새 주문을 내지 않음 (이유)
    realized: float = 0.0  # 실현 손익 합계 (수수료 전)
    history: list[dict] = field(default_factory=list)


class SplitStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> SplitState | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["lots"] = [Lot(**x) for x in raw.get("lots", [])]
        return SplitState(**raw)

    def save(self, st: SplitState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def make_lots(start: int, gap: int, levels: int, first_level: int = 1) -> list[Lot]:
    """first_level 차부터 levels 차까지 start 에서 gap 씩 내려가는 차수 (가격 0 이하는 안 만듦)."""
    return [Lot(level=first_level + k, price=start - gap * k)
            for k in range(levels - first_level + 1) if start - gap * k > 0]


def regap(st: SplitState, new_gap: int) -> int:
    """보유 중 가장 낮은 차수 아래의 안 산 차수를 new_gap 간격으로 다시 만든다 (보유 차수는 원래 익절 폭 유지).
    다시 만든 차수 수를 돌려줌. 걸어 둔 매수 주문은 다음 확인 때 새 차수로 바뀜."""
    held = [x for x in st.lots if x.qty > 0]
    keep = st.lots[: max(x.level for x in held)] if held else []
    for x in keep:
        x.gap = x.gap or st.gap
    base = keep[-1].price if keep else st.start + new_gap
    st.lots = keep + make_lots(base - new_gap, new_gap, len(keep) + base // new_gap, len(keep) + 1)
    st.gap = new_gap
    return len(st.lots) - len(keep)


class SplitTrader:
    def __init__(self, cfg: SplitConfig, broker: Broker, store: SplitStore, bot_symbols=lambda: set()):
        self.cfg = cfg
        self.broker = broker
        self.store = store
        self.bot_symbols = bot_symbols  # 토스 봇이 보유한 종목 (같은 종목이면 멈춤)
        st = store.load()
        if st is None:
            st = SplitState(symbol=cfg.symbol, start=cfg.start, gap=cfg.gap,
                            lots=make_lots(cfg.start, cfg.gap, cfg.levels))
            for lot in st.lots[: cfg.init_held]:
                lot.qty, lot.cost = cfg.qty, float(lot.price)
            store.save(st)
            log.info("스플릿 %s 새 상태: 1차 %s원, 간격 %s원, %d차수 x %d주, 이미 산 차수 %d",
                     cfg.symbol, f"{cfg.start:,}", f"{cfg.gap:,}", cfg.levels, cfg.qty, cfg.init_held)
        if cfg.levels > len(st.lots) and st.lots:  # SPLIT_LEVELS 를 늘리면 마지막 차수 아래로 같은 간격 차수를 덧붙임
            more = make_lots(st.lots[-1].price - st.gap, st.gap, cfg.levels, len(st.lots) + 1)
            if more:
                st.lots += more
                store.save(st)
                log.info("스플릿 %s 차수 %d → %d (마지막 %s원)", st.symbol, len(st.lots) - len(more),
                         len(st.lots), f"{st.lots[-1].price:,}")
        self.st = st
        self.checked_day = None  # 계좌 보유 수량을 확인한 날 (하루 한 번)
        self.buy_blocked_day = None  # 현금 부족 등으로 매수를 못 낸 날 (그날은 매수 쉼)
        self.today = None

    def real_quantity(self) -> int | None:
        """계좌의 실제 보유 수량 (모의 매매면 None = 확인 안 함)."""
        if self.broker.dry_run:
            return None
        items = (self.broker.client.get_holdings(self.st.symbol) or {}).get("items", [])
        return sum(int(Decimal(str(x.get("quantity", 0)))) for x in items if x.get("symbol") == self.st.symbol)

    # ------------------------------------------------------------ helpers
    def held(self) -> list[Lot]:
        return [x for x in self.st.lots if x.qty > 0]

    def held_qty(self) -> int:
        return sum(x.qty for x in self.st.lots)

    def lot(self, level: int) -> Lot:
        return self.st.lots[level - 1]

    def desired(self) -> tuple[Lot | None, Lot | None]:
        """(걸어 둘 매수 차수, 걸어 둘 매도 차수)."""
        held = self.held()
        if not held:
            return None, None  # 재진입은 따로
        sell = min(held, key=lambda x: x.price)
        empty = [x for x in self.st.lots if x.qty == 0 and x.price < sell.price]
        buy = max(empty, key=lambda x: x.price) if empty else None
        return buy, sell

    def stale(self) -> list[str]:
        """걸어 둔 주문 중 지금 원하는 차수와 다른 것 (재진입 매수 주문은 그대로 둠)."""
        if not self.held():
            o = self.st.buy_order
            return ["buy_order"] if o and not o.get("reentry") else []
        buy, sell = self.desired()
        out = []
        o = self.st.buy_order
        if o and (o.get("reentry") or buy is None or o["level"] != buy.level):
            out.append("buy_order")
        o = self.st.sell_order
        if o and (sell is None or o["level"] != sell.level):
            out.append("sell_order")
        return out

    def buy_qty(self, price: int) -> int:
        """이 가격에 한 차수로 살 주식 수."""
        return int(self.cfg.budget // price) if self.cfg.budget > 0 else self.cfg.qty

    def sell_price(self, lot: Lot) -> int:
        return round_up_to_tick(lot.price + (lot.gap or self.st.gap))

    def log_trade(self, now: datetime, side: str, lot: Lot, qty: int, price: float, pnl: float | None = None) -> None:
        row = {"at": now.isoformat(timespec="seconds"), "cycle": self.st.cycle, "side": side,
               "level": lot.level, "qty": qty, "price": price}
        if pnl is not None:
            row["pnl"] = round(pnl)
        self.st.history.append(row)
        log.info("스플릿 %s %d차 %s %d주 @ %s%s", self.st.symbol, lot.level, "매수" if side == "BUY" else "익절 매도",
                 qty, f"{price:,.0f}", f" (손익 {pnl:+,.0f}원)" if pnl is not None else "")

    # ------------------------------------------------------------- orders
    def sync(self, now: datetime) -> bool:
        """걸어 둔 주문의 체결·취소를 반영. 상태가 바뀌었으면 True."""
        changed = False
        for key in ("buy_order", "sell_order"):
            o = getattr(self.st, key)
            if not o:
                continue
            info = self.broker.order_status(o["id"])
            status = info.get("status", "")
            if status not in GONE_STATUSES and hasattr(self.broker, "closed_order"):
                # 토스 단건 조회는 취소된 주문도 계속 PENDING 으로 줄 때가 있음 (2026-11-04) → 끝난 주문 목록으로 확인
                try:
                    closed = self.broker.closed_order(o["id"], self.st.symbol)
                except Exception:  # noqa: BLE001
                    log.warning("스플릿 끝난 주문 목록 조회 실패", exc_info=True)
                    closed = None
                if closed:
                    info, status = closed, closed.get("status", "")
            if status not in GONE_STATUSES and self.expired_day_order(info, now):
                # 토스는 전날 DAY 주문이 장 마감으로 없어져도 조회하면 PENDING 으로 줌 (취소는 404)
                status = "EXPIRED"
            if status not in GONE_STATUSES:
                continue
            ex = info.get("execution") or {}
            filled = int(Decimal(str(ex.get("filledQuantity") or 0)))
            if status == "FILLED" and not filled:
                filled = o["qty"]
            avg = float(Decimal(str(ex.get("averageFilledPrice") or o["price"])))
            lot = self.lot(o["level"])
            if filled:
                if key == "buy_order":
                    lot.cost = (lot.cost * lot.qty + avg * filled) / (lot.qty + filled)
                    lot.qty += filled
                    self.log_trade(now, "BUY", lot, filled, avg)
                else:
                    pnl = (avg - lot.cost) * filled
                    self.st.realized += pnl
                    lot.qty -= filled
                    if lot.qty <= 0:
                        lot.qty, lot.cost = 0, 0.0
                    self.log_trade(now, "SELL", lot, filled, avg, pnl)
                    if not self.held():
                        self.st.peak = avg
                        log.info("스플릿 %s 모든 차수 익절 (사이클 %d 끝)%s", self.st.symbol, self.st.cycle,
                                 f" → 고점 -{self.cfg.reentry_drop:g}% 에서 재진입 대기" if self.cfg.reentry else "")
            elif status != "FILLED":
                log.info("스플릿 %s %s 주문 %s (%s) 정리", self.st.symbol, "매수" if key == "buy_order" else "매도",
                         o["id"], status)
            setattr(self.st, key, None)
            changed = True
        return changed

    @staticmethod
    def expired_day_order(info: dict, now: datetime) -> bool:
        at = str(info.get("orderedAt") or "")[:10]
        return info.get("timeInForce", "DAY") == "DAY" and bool(at) and at < now.date().isoformat()

    def cancel(self, key: str) -> None:
        o = getattr(self.st, key)
        if not o:
            return
        try:
            self.broker.cancel(o["id"])
        except Exception as exc:  # noqa: BLE001  이미 체결됐을 수 있음 → 다음 sync 에서 처리
            if getattr(exc, "code", "") == "order-not-found":
                log.warning("스플릿 주문 %s 이 토스에 없음 → 기록에서 지움", o["id"])
                setattr(self.st, key, None)
                return
            log.warning("스플릿 주문 취소 실패 %s (이미 체결됐을 수 있음)", o["id"], exc_info=True)

    def place(self, key: str, lot: Lot, price: int, qty: int) -> None:
        side = "BUY" if key == "buy_order" else "SELL"
        if side == "BUY":
            if self.buy_blocked_day == self.today:
                return
            reason = ""
            try:
                cash = self.broker.cash_buying_power()
                if cash < price * qty * 1.001:
                    reason = f"현금 부족 (매수 가능 {cash:,}원)"
                else:
                    oid = self.broker.buy_limit(self.st.symbol, qty, price)
            except Exception as exc:  # noqa: BLE001  증거금 부족 등 거절
                if "opposite-pending-order" in str(exc):  # 반대쪽 미체결 주문 때문 → 하루 막지 않고 다음 확인 때
                    log.warning("스플릿 %s %d차 매수 보류: 매도 주문이 아직 걸려 있음", self.st.symbol, lot.level)
                    return
                reason = f"주문 거절 ({exc})"
            if reason:
                self.buy_blocked_day = self.today  # 오늘은 다시 시도하지 않음 (매분 같은 오류 방지)
                log.warning("스플릿 %s %d차 매수 %s원 못 냄: %s → 오늘은 매수 쉼", self.st.symbol, lot.level,
                            f"{price:,}", reason)
                return
        else:
            oid = self.broker.sell_limit(self.st.symbol, qty, price)
        setattr(self.st, key, {"id": oid, "level": lot.level, "price": price, "qty": qty})

    def can_order(self, now: datetime, day: TradingDay | None) -> bool:
        if day is None or not day.is_open or day.market_open is None or day.market_close is None:
            return False
        return day.market_open <= now < day.market_close - LAST_ORDER_BEFORE_CLOSE

    # --------------------------------------------------------------- tick
    def tick(self, now: datetime, day: TradingDay | None) -> None:
        self.today = now.date()
        if self.sync(now):
            self.store.save(self.st)
        if self.st.symbol in self.bot_symbols():
            if not self.st.paused:
                self.st.paused = "토스 봇이 같은 종목을 보유 중"
                log.warning("스플릿 %s: 토스 봇이 같은 종목을 사서 스플릿 주문을 멈춥니다", self.st.symbol)
                self.store.save(self.st)
            return
        if self.st.paused or not self.can_order(now, day):
            return
        if self.checked_day != now.date():
            self.checked_day = now.date()
            real = self.real_quantity()
            if real is not None and real < self.held_qty():
                self.st.paused = f"계좌 보유 {real}주 < 스플릿 기록 {self.held_qty()}주"
                log.warning("스플릿 %s: %s → 새 주문을 멈춥니다 (앱에서 직접 판 경우 state/%s 를 고쳐야 함)",
                            self.st.symbol, self.st.paused, self.store.path.name)
                self.store.save(self.st)
                return
        stale = self.stale()
        if stale:  # 걸어 둔 주문이 지금 원하는 차수와 다르면 취소하고 결과를 반영한 뒤 새로 냄
            for key in stale:
                self.cancel(key)
            if self.sync(now):
                self.store.save(self.st)
            if self.stale():
                return  # 취소가 아직 반영 안 됨 → 다음 확인 때
        if not self.held():
            if not self.cfg.reentry:
                self.st.paused = "모든 차수 익절 — 끝 (재진입 끔)"
                log.info("스플릿 %s 모든 차수 익절, 재진입을 꺼 두어 여기서 끝냅니다 (실현 손익 %s원)",
                         self.st.symbol, f"{self.st.realized:+,.0f}")
                self.store.save(self.st)
                return
            self.reentry(now)
            self.store.save(self.st)
            return
        # 토스는 같은 종목에 매도 주문이 걸려 있으면 매수 주문을 거절함(409 opposite-pending-order-exists)
        # → 지금 가격에 더 가까운 쪽 하나만 걸어 두고, 가격이 반대쪽으로 확실히 가까워지면 바꿔 검
        buy, sell = self.desired()
        if buy and self.buy_qty(buy.price) <= 0:
            buy = None
        if not buy and not sell:
            return
        price = float(self.broker.last_prices([self.st.symbol])[self.st.symbol])
        cur = "buy_order" if self.st.buy_order else "sell_order" if self.st.sell_order else None
        want = self.pick_side(price, buy, sell, cur)
        if want == "buy_order" and self.buy_blocked_day == self.today and sell:
            want = "sell_order"  # 오늘은 매수를 못 내니 익절 주문이라도 걸어 둠
        if cur and cur != want:
            self.cancel(cur)
            if self.sync(now):
                self.store.save(self.st)
            if getattr(self.st, cur):
                return  # 취소가 아직 반영 안 됨 → 다음 확인 때
            if not self.held():
                self.store.save(self.st)
                return  # 취소 직전에 마지막 차수가 팔림 → 다음 확인 때 끝/재진입 처리
            buy, sell = self.desired()  # 취소 직전에 체결됐을 수 있어 다시 계산
            if buy and self.buy_qty(buy.price) <= 0:
                buy = None
            want = self.pick_side(price, buy, sell, None)
        if want == "sell_order" and sell and not self.st.sell_order:
            self.place("sell_order", sell, self.sell_price(sell), sell.qty)
        elif want == "buy_order" and buy and not self.st.buy_order:
            self.place("buy_order", buy, buy.price, self.buy_qty(buy.price))
        self.store.save(self.st)

    def pick_side(self, price: float, buy: Lot | None, sell: Lot | None, cur: str | None) -> str:
        """걸어 둘 쪽: 이미 닿은 쪽 > 더 가까운 쪽. 이미 걸린 쪽은 반대쪽이 간격의 1/4 이상 더 가까워질 때만 바꿈."""
        if not buy:
            return "sell_order"
        if not sell:
            return "buy_order"
        d_buy = price - buy.price
        d_sell = self.sell_price(sell) - price
        if d_buy <= 0:
            return "buy_order"
        if d_sell <= 0:
            return "sell_order"
        margin = self.st.gap / 4
        if cur == "sell_order":
            return "buy_order" if d_buy + margin < d_sell else "sell_order"
        if cur == "buy_order":
            return "sell_order" if d_sell + margin < d_buy else "buy_order"
        return "buy_order" if d_buy <= d_sell else "sell_order"

    def reentry(self, now: datetime) -> None:
        if self.st.buy_order:
            return
        price = float(self.broker.last_prices([self.st.symbol])[self.st.symbol])
        self.st.peak = max(self.st.peak, price)
        trigger = self.st.peak * (1 - self.cfg.reentry_drop / 100)
        if price > trigger:
            return
        start = round_down_to_tick(price)
        tick = tick_size(start)  # 간격도 이 가격대의 호가 단위로 (모든 차수 가격이 호가에 맞게)
        gap = max(tick, int(start * self.cfg.gap_pct / 100 // tick) * tick)
        self.st.cycle += 1
        self.st.start, self.st.gap, self.st.peak = start, gap, 0.0
        self.st.lots = make_lots(start, gap, self.cfg.levels)
        log.info("스플릿 %s 재진입 (사이클 %d): 1차 %s원, 간격 %s원", self.st.symbol, self.st.cycle,
                 f"{start:,}", f"{gap:,}")
        # 지금 가격보다 한 호가 위 지정가 = 바로 체결되는 시장성 지정가
        limit = round_up_to_tick(price + tick_size(price))
        if self.buy_qty(limit) > 0:
            self.place("buy_order", self.st.lots[0], limit, self.buy_qty(limit))
        if self.st.buy_order:
            self.st.buy_order["reentry"] = True

    # ------------------------------------------------------------- status
    def summary(self) -> str:
        st = self.st
        lines = [f"스플릿 {st.symbol} 사이클 {st.cycle}, 1차 {st.start:,}원, 간격 {st.gap:,}원, "
                 f"보유 {self.held_qty()}주, 실현 손익 {st.realized:+,.0f}원{' [멈춤: ' + st.paused + ']' if st.paused else ''}"]
        for x in st.lots:
            mark = ""
            if st.buy_order and st.buy_order["level"] == x.level:
                mark = f"  ← 매수 주문 {st.buy_order['price']:,}"
            if st.sell_order and st.sell_order["level"] == x.level:
                mark = f"  ← 익절 주문 {st.sell_order['price']:,}"
            lines.append(f"  {x.level:2d}차 {x.price:>8,}  {'보유 ' + str(x.qty) + '주' if x.qty else '-':<8} "
                         f"익절가 {self.sell_price(x):,}{mark}")
        if not self.held() and self.cfg.reentry:
            lines.append(f"  재진입 대기: 고점 {st.peak:,.0f} -{self.cfg.reentry_drop:g}%")
        return "\n".join(lines)
