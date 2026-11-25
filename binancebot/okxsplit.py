"""OKX 무기한 선물 한 종목 스플릿 매매 (2026-11-25 사용자 결정: KORU, 롱만, 손절 없음).

규칙
- 1차: 시작 시각(OS_START, 한국 시각)이 지나면 OS_FIRST_QTY 주(3주) 시장가 매수. 1차는 팔지 않음.
- k차(2~OS_LEVELS=10): 1차 체결가 x (1 - OS_DROP%)^(k-1) 에 OS_QTY 주(1주) 지정가 매수 (직전 차수 -3%씩)
- 2차부터: 차수마다 자기 체결가 x (1 + OS_RISE%) 에 reduceOnly 지정가 매도. 팔린 차수는 다시 내려오면 또 삼
- 손절 없음. 걸어 두는 주문 = 다음 매수 차수 하나 + 가장 깊은 보유 차수의 매도 하나 (GTC)
- OKX 지정가 허용 범위(±2%) 밖 가격은 거절되므로 현재가에서 OS_BAND% 안에 들어왔을 때만 검
- 30초마다 거래소 포지션 = 기록 수량인지 확인, 다르면 새 주문 멈춤 (`okxsplit resume` 으로 풂)
- 24시간 거래 (OKX 는 미국 장이 닫혀도 거래됨)

명령: python -m binancebot.okxsplit check | run | status | resume  [--env .env.okx]
OKX 실제 주문 응답은 2026-11-25 기준 아직 실제로 확인하지 못함 (가짜 응답 테스트만).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from pathlib import Path

from tossbot.config import KST, _bool, _get, load_dotenv

log = logging.getLogger("binancebot.okxsplit")

DONE = {"filled", "canceled", "mmp_canceled"}


def q(x, step: str, rounding=ROUND_DOWN) -> Decimal:
    return (Decimal(str(x)) / Decimal(step)).quantize(Decimal(1), rounding=rounding) * Decimal(step)


@dataclass
class OkxSplitConfig:
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    demo: bool = False
    dry_run: bool = True
    symbol: str = "KORUUSDT"
    start: str = ""  # "2026-11-25 23:30" 한국 시각, 비우면 바로
    first_qty: float = 3
    qty: float = 1
    drop_pct: float = 3.0
    rise_pct: float = 2.9
    levels: int = 10
    band_pct: float = 1.8
    leverage: int = 3  # 거래소 배율 설정(증거금만 바뀜, 주문 수량은 주식 수 그대로)
    interval: float = 5.0
    state_dir: str = "state_okx_split"

    @classmethod
    def from_env(cls) -> "OkxSplitConfig":
        def num(name, attr, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            api_key=os.environ.get("OKX_API_KEY", ""), api_secret=os.environ.get("OKX_API_SECRET", ""),
            passphrase=os.environ.get("OKX_PASSPHRASE", ""), demo=_bool(os.environ.get("OKX_DEMO"), False),
            dry_run=_bool(os.environ.get("OS_DRY_RUN"), True),
            symbol=_get("OS_SYMBOL", cls.symbol), start=_get("OS_START", cls.start),
            first_qty=num("OS_FIRST_QTY", "first_qty"), qty=num("OS_QTY", "qty"),
            drop_pct=num("OS_DROP", "drop_pct"), rise_pct=num("OS_RISE", "rise_pct"),
            levels=num("OS_LEVELS", "levels", int), band_pct=num("OS_BAND", "band_pct"),
            leverage=num("OS_LEVERAGE", "leverage", int), interval=num("OS_INTERVAL", "interval"),
            state_dir=_get("OS_STATE_DIR", cls.state_dir),
        )

    def start_at(self) -> datetime | None:
        if not self.start:
            return None
        return datetime.strptime(self.start, "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    def describe(self) -> str:
        return (f"{self.symbol} {'드라이런' if self.dry_run else '실제 주문'}, 시작 {self.start or '바로'}, "
                f"1차 {self.first_qty:g}주(매도 없음), 2~{self.levels}차 -{self.drop_pct:g}%마다 {self.qty:g}주, "
                f"+{self.rise_pct:g}% 매도, 손절 없음")


@dataclass
class Lot:
    level: int
    price: str = "0"
    qty: float = 0.0
    cost: float = 0.0


@dataclass
class State:
    symbol: str
    lots: list[Lot] = field(default_factory=list)
    base: str = ""
    buy: dict | None = None  # {"id", "level", "price", "qty"}
    sell: dict | None = None
    realized: float = 0.0
    fees_note: str = "수수료·펀딩비는 손익에 안 들어감"
    paused: str = ""
    history: list[dict] = field(default_factory=list)


class Store:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["lots"] = [Lot(**x) for x in raw.get("lots", [])]
        return State(**raw)

    def save(self, st: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class PaperOkx:
    """드라이런: 진짜 시세로 지정가 체결을 흉내 냄 (키 필요 없음)."""

    def __init__(self, real):
        self.real = real
        self.orders: dict[str, dict] = {}
        self.pos = 0.0
        self.n = 0

    def last(self, symbol: str) -> float:
        return self.real.last(symbol)

    def place(self, symbol, side, qty, price=None, reduce_only=False) -> dict:
        self.n += 1
        oid = f"DRY{self.n}"
        o = {"ordId": oid, "side": side, "sz": qty, "px": price, "state": "live", "accFillSz": 0, "avgPx": ""}
        self.orders[oid] = o
        if price is None:
            p = self.last(symbol)
            o.update(state="filled", accFillSz=qty, avgPx=str(p))
            self.pos += qty if side == "buy" else -qty
        return o

    def query(self, symbol, oid) -> dict:
        o = self.orders.get(oid) or {"ordId": oid, "state": "canceled", "accFillSz": 0}
        if o["state"] == "live":
            p = self.last(symbol)
            px = float(o["px"])
            if (o["side"] == "buy" and p <= px) or (o["side"] == "sell" and p >= px):
                o.update(state="filled", accFillSz=o["sz"], avgPx=str(min(px, p) if o["side"] == "buy" else max(px, p)))
                self.pos += o["sz"] if o["side"] == "buy" else -o["sz"]
        return o

    def cancel(self, symbol, oid) -> None:
        o = self.orders.get(oid)
        if o and o["state"] == "live":
            o["state"] = "canceled"

    def position(self, symbol) -> float:
        return self.pos


class RealOkx:
    """OkxClient 위에 이 봇이 쓰는 몇 가지만 (주식 수 = 계약 수 x ctVal)."""

    def __init__(self, client):
        self.c = client

    def last(self, symbol: str) -> float:
        from .okx_client import to_inst

        return float(self.c._req("GET", "/api/v5/market/ticker", {"instId": to_inst(symbol)})[0]["last"])

    def place(self, symbol, side, qty, price=None, reduce_only=False) -> dict:
        p = {"symbol": symbol, "side": side.upper(), "quantity": qty}
        if price is None:
            p["type"] = "MARKET"
        else:
            p.update(type="LIMIT", timeInForce="GTC", price=price)
        if reduce_only:
            p["reduceOnly"] = "true"
        r = self.c.order(**p)
        if price is None:
            return {"ordId": r["orderId"], "state": r.get("status", "").lower(), "accFillSz": r.get("executedQty", 0),
                    "avgPx": r.get("avgPrice", "")}
        return {"ordId": r["orderId"], "state": "live"}

    def query(self, symbol, oid) -> dict:
        o = dict(self.c.query(symbol, oid))
        o["accFillSz"] = float(o.get("accFillSz") or 0) * self.c._ct(symbol)
        return o

    def cancel(self, symbol, oid) -> None:
        from .okx_client import to_inst

        self.c._req("POST", "/api/v5/trade/cancel-order", {"instId": to_inst(symbol), "ordId": oid}, signed=True)

    def position(self, symbol) -> float:
        return self.c.position_amt(symbol)[0]


class OkxSplit:
    def __init__(self, cfg: OkxSplitConfig, ex, store: Store, tick: str = "0.001", lot: str = "0.01"):
        self.cfg, self.ex, self.store = cfg, ex, store
        self.tick_sz, self.lot_sz = tick, lot
        st = store.load()
        if st is None:
            st = State(symbol=cfg.symbol, lots=[Lot(level=k) for k in range(1, cfg.levels + 1)])
            store.save(st)
        self.st = st
        self.last_pos_check = 0.0
        self.pos_mismatch = 0

    # ------------------------------------------------------------ helpers
    def lot(self, level: int) -> Lot:
        return self.st.lots[level - 1]

    def held_qty(self) -> float:
        return round(sum(x.qty for x in self.st.lots), 8)

    def level_price(self, level: int) -> Decimal:
        f = Decimal(str(1 - self.cfg.drop_pct / 100)) ** (level - 1)
        return q(Decimal(self.st.base) * f, self.tick_sz)

    def sell_price(self, lot: Lot) -> Decimal:
        return q(Decimal(str(lot.cost)) * Decimal(str(1 + self.cfg.rise_pct / 100)), self.tick_sz, ROUND_UP)

    def targets(self) -> tuple[Lot | None, Lot | None]:
        held = [x for x in self.st.lots if x.qty > 0]
        sells = [x for x in held if x.level >= 2]
        sell = max(sells, key=lambda x: x.level) if sells else None
        deepest = max((x.level for x in held), default=1)
        empty = [x for x in self.st.lots if x.qty == 0 and x.level > max(deepest, 1)]
        buy = min(empty, key=lambda x: x.level) if empty else None
        return buy, sell

    def record(self, side: str, lot: Lot, qty: float, price: float, pnl: float | None = None) -> None:
        row = {"at": datetime.now(KST).isoformat(timespec="seconds"), "side": side, "level": lot.level,
               "qty": qty, "price": price}
        if pnl is not None:
            row["pnl"] = round(pnl, 4)
        self.st.history.append(row)
        log.info("%s %d차 %s %g주 @ %g%s", self.st.symbol, lot.level, "매수" if side == "BUY" else "매도", qty, price,
                 f" (손익 {pnl:+.2f} USDT)" if pnl is not None else "")

    def apply_buy(self, lot: Lot, qty: float, price: float) -> None:
        lot.cost = (lot.cost * lot.qty + price * qty) / (lot.qty + qty)
        lot.qty = round(lot.qty + qty, 8)
        if lot.level == 1 and not self.st.base:
            self.st.base = str(q(price, self.tick_sz))
            lot.price = self.st.base
            for x in self.st.lots[1:]:
                x.price = str(self.level_price(x.level))
            log.info("%s 1차 체결가 %s → 2~%d차 %s ~ %s", self.st.symbol, self.st.base, self.cfg.levels,
                     self.st.lots[1].price, self.st.lots[-1].price)
        self.record("BUY", lot, qty, price)

    # ------------------------------------------------------------- orders
    def sync(self) -> bool:
        changed = False
        for key in ("buy", "sell"):
            o = getattr(self.st, key)
            if not o:
                continue
            info = self.ex.query(self.st.symbol, o["id"])
            state = info.get("state", "")
            filled = float(info.get("accFillSz") or 0) - o.get("done", 0.0)
            lot = self.lot(o["level"])
            if filled > 1e-9:
                price = float(info.get("avgPx") or o["price"])
                if key == "buy":
                    self.apply_buy(lot, filled, price)
                else:
                    pnl = (price - lot.cost) * filled
                    self.st.realized += pnl
                    lot.qty = round(lot.qty - filled, 8)
                    if lot.qty <= 1e-9:
                        lot.qty, lot.cost = 0.0, 0.0
                    self.record("SELL", lot, filled, price, pnl)
                o["done"] = o.get("done", 0.0) + filled
                changed = True
            if state in DONE:
                setattr(self.st, key, None)
                changed = True
        return changed

    def cancel(self, key: str) -> None:
        o = getattr(self.st, key)
        if not o:
            return
        try:
            self.ex.cancel(self.st.symbol, o["id"])
        except Exception as exc:  # noqa: BLE001  이미 체결됐을 수 있음 → 다음 sync 에서 처리
            log.warning("%s 주문 취소 응답 %s: %s", self.st.symbol, o["id"], exc)

    def place(self, key: str, lot: Lot, price: Decimal, qty: float) -> None:
        side = "buy" if key == "buy" else "sell"
        try:
            r = self.ex.place(self.st.symbol, side, qty, str(price), reduce_only=(key == "sell"))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s %d차 %s 주문 실패: %s", self.st.symbol, lot.level, "매수" if side == "buy" else "매도", exc)
            return
        setattr(self.st, key, {"id": r["ordId"], "level": lot.level, "price": str(price), "qty": qty})
        log.info("%s %d차 %s 지정가 %g주 @ %s", self.st.symbol, lot.level, "매수" if side == "buy" else "매도", qty, price)

    # --------------------------------------------------------------- tick
    def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(KST)
        if self.sync():
            self.store.save(self.st)
        if self.st.paused:
            return
        start = self.cfg.start_at()
        if start and now < start:
            return
        if not self.cfg.dry_run and time.time() - self.last_pos_check >= 30:
            self.last_pos_check = time.time()
            pos = self.ex.position(self.st.symbol)
            # 확인 사이에 체결이 끼면 잠깐 다를 수 있음 → 두 번 연속 다를 때만 멈춤
            self.pos_mismatch = self.pos_mismatch + 1 if abs(pos - self.held_qty()) > 1e-6 else 0
            if self.pos_mismatch >= 2:
                self.st.paused = f"거래소 포지션 {pos:g} ≠ 기록 {self.held_qty():g} ({now:%m-%d %H:%M})"
                log.error("%s 멈춤: %s", self.st.symbol, self.st.paused)
                self.store.save(self.st)
                return
        if not self.st.base:
            if self.lot(1).qty == 0 and not self.st.buy:
                r = self.ex.place(self.st.symbol, "buy", self.cfg.first_qty)  # 시장가
                filled = float(r.get("accFillSz") or 0)
                if filled > 0:
                    self.apply_buy(self.lot(1), filled, float(r.get("avgPx") or self.ex.last(self.st.symbol)))
                else:
                    log.warning("%s 1차 시장가 매수가 체결되지 않음: %s", self.st.symbol, r)
                self.store.save(self.st)
            return
        price = Decimal(str(self.ex.last(self.st.symbol)))
        band = Decimal(str(self.cfg.band_pct / 100))
        buy, sell = self.targets()
        changed = False
        for key, lot, px, qty in (("buy", buy, Decimal(buy.price) if buy else None, self.cfg.qty),
                                  ("sell", sell, self.sell_price(sell) if sell else None, sell.qty if sell else 0)):
            o = getattr(self.st, key)
            if o and (lot is None or o["level"] != lot.level):
                self.cancel(key)  # 원하는 차수가 바뀜 → 다음 tick 에 새로 검
                changed = True
                continue
            if o or lot is None:
                continue
            if key == "buy":
                if px < price * (1 - band):
                    continue  # 아직 멂 (지정가 허용 범위 밖)
                px = min(px, q(price * (1 + band), self.tick_sz))  # 이미 지나쳤으면 범위 안 가격 (그 아래로 체결)
            else:
                if px > price * (1 + band):
                    continue
                px = max(px, q(price * (1 - band), self.tick_sz, ROUND_UP))
            self.place(key, lot, px, qty)
            changed = True
        if changed:
            self.store.save(self.st)

    def status(self) -> str:
        st = self.st
        lines = [f"OKX 스플릿 {st.symbol}  1차 {st.base or '아직 안 삼'}, 보유 {self.held_qty():g}주, "
                 f"확정 손익 {st.realized:+.2f} USDT (수수료·펀딩 제외){'  [멈춤: ' + st.paused + ']' if st.paused else ''}"]
        for x in st.lots:
            tgt = "매도 없음" if x.level == 1 else (f"매도가 {self.sell_price(x)}" if x.qty else "")
            lines.append(f"  {x.level:2d}차 {x.price if x.price != '0' else '-':>9} "
                         f"{('보유 ' + format(x.qty, 'g') + '주') if x.qty else '-':<9} {tgt}")
        for key in ("buy", "sell"):
            o = getattr(st, key)
            if o:
                lines.append(f"  걸린 주문: {o['level']}차 {'매수' if key == 'buy' else '매도'} {o['qty']:g}주 @ {o['price']}")
        return "\n".join(lines)


def build(cfg: OkxSplitConfig):
    from .okx_client import OkxClient

    client = OkxClient(cfg.api_key, cfg.api_secret, cfg.passphrase, demo=cfg.demo)
    real = RealOkx(client)
    client.exchange_info()
    inst = client.inst.get(cfg.symbol, {})
    ex = PaperOkx(real) if cfg.dry_run else real
    name = "state.dry.json" if cfg.dry_run else "state.json"
    return OkxSplit(cfg, ex, Store(Path(cfg.state_dir) / name), inst.get("tickSz", "0.001"), inst.get("lotSz", "0.01")), client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["check", "run", "status", "resume"])
    ap.add_argument("--env", default=".env.okx")
    a = ap.parse_args()
    load_dotenv(a.env)
    cfg = OkxSplitConfig.from_env()
    keys = bool(cfg.api_key and cfg.api_secret and cfg.passphrase)
    if not cfg.dry_run and not keys:
        print(f"실제 주문(OS_DRY_RUN=false)인데 OKX 키 3개 중 빈 것이 있어요. 메모장으로 {a.env} 를 열어 넣어 주세요.")
        return
    bot, client = build(cfg)
    if a.action == "status":
        print(bot.status())
        return
    if a.action == "resume":
        bot.st.paused = ""
        bot.store.save(bot.st)
        print("멈춤을 풀었어요 (켜져 있는 봇은 다시 켜야 반영)\n" + bot.status())
        return
    if a.action == "check":
        print("설정:", cfg.describe())
        p = bot.ex.last(cfg.symbol)
        print(f"{cfg.symbol} 현재가 {p:g}, 가격 단위 {bot.tick_sz}, 수량 단위 {bot.lot_sz}")
        print(f"지금 시작하면 1차 {p:g} x {cfg.first_qty:g}주, 10차 {p * (1 - cfg.drop_pct / 100) ** (cfg.levels - 1):.3f}, "
              f"다 사면 약 {p * cfg.first_qty + sum(p * (1 - cfg.drop_pct / 100) ** k * cfg.qty for k in range(1, cfg.levels)):.2f} USDT")
        if keys:
            try:
                print(f"계좌 평가금액 {client.equity():,.2f} USDT, 포지션 모드 {'양방향(안 됨)' if client.hedge_mode() else '단방향'}, "
                      f"{cfg.symbol} 포지션 {client.position_amt(cfg.symbol)[0]:g}")
            except Exception as exc:  # noqa: BLE001
                print("OKX 계좌 조회 실패:", exc)
        else:
            print("OKX 키 3개가 다 있지 않아 계좌는 확인 못 했어요")
        print("확인 끝 (주문은 보내지 않았어요)")
        return
    from kiwoombot.__main__ import acquire_lock

    from .botmain import setup_logging

    setup_logging(cfg.state_dir)
    lock = acquire_lock(cfg.state_dir)
    if lock is None:
        print("OKX 스플릿 봇이 이미 켜져 있어요. 이 창은 닫아도 됩니다.")
        return
    if not cfg.dry_run:
        if client.hedge_mode():
            print("OKX 계정이 양방향(롱/숏) 포지션 모드예요. 단방향(순포지션)으로 바꾼 뒤 다시 켜 주세요.")
            return
        try:
            client.set_leverage(cfg.symbol, cfg.leverage)
        except Exception as exc:  # noqa: BLE001
            log.warning("배율 설정 응답: %s", exc)
    log.info("OKX 스플릿 시작: %s\n%s", cfg.describe(), bot.status())
    while True:
        try:
            bot.tick()
        except Exception:  # noqa: BLE001
            log.exception("OKX 스플릿 처리 중 오류")
        time.sleep(cfg.interval)


if __name__ == "__main__":
    main()
