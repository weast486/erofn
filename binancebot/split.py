"""바이낸스 SOXL 스플릿 매매 봇 (2026-10-24 사용자 요청). 설정 .env.binance_split, 기본 드라이런.

규칙 (백테스트 2026-05-15~10-23 SOXL 1분봉, 박스 10%·15분할·1배·손절 2간격 100 → 157$, 복리):
  1. 1차를 지금 가격(P)에 산다. 간격 d = P x 박스% / N(분할 수), k차 매수가 = P - d x (k-1)
  2. 차수마다 매수가 + d + 매수가 x 0.1% 에 익절(지정가), 판 차수는 그 가격까지 다시 내려오면 또 산다
  3. 손절 = 마지막 차수 - d x SP_STOP_STEPS (기본 2간격). 닿으면 전부 정리하고 그 가격에서 1차부터 다시 시작
     (손절가 위의 차수는 모두 사진 뒤라 '모든 차수가 찬 뒤 손절' 과 같음 → 거래소 손절을 사이클 시작 때 걸어 둠)
  4. 모든 차수가 익절되면 그 뒤 고점에서 SP_REENTRY_DROP%(기본 2%) 내려온 가격에 1차부터 다시 시작
     (SP_REENTRY_DROP=0 이면 기다리지 않고 바로 재진입, SP_FIRST_TP_STEPS=2 면 1차만 2간격에 익절 —
      2026-10-24 백테스트에서 둘 다 기본보다 나빴음: 20분할 3배 634 → 173$, 15분할 1배 손절 2간격 157 → 86$)
  5. 새로 시작할 때만 차수당 금액 = 봇 장부 금액(SP_CAPITAL + 실현 손익) / N x 배율 (복리)
     차수당 금액이 최소 주문 금액 아래로 내려가면 멈춘다

주의 (백테스트 결과): 같은 규칙을 바이낸스 주식 토큰 41개에 넓히면 본전 수준이었고, 지정가 수수료가 0.05% 만 돼도
이익이 크게 준다 → 모든 매수·익절은 가격에 닿기 전에 미리 걸어 두는 지정가(Post Only, 메이커)로 넣고, 가격이 이미
지나쳐 버렸을 때만 IOC 지정가로 받는다. 같은 계좌에서 FVG 봇이 같은 종목을 매매하면 포지션이 섞이므로
서브 계정(다른 API 키)을 쓰거나 .env.binance 의 BN_EXCLUDE 에 이 종목을 넣어야 실제 주문을 시작한다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from tossbot.config import _bool, _get

from .client import BinanceClient, BinanceError, filters_of, fmt, round_step, round_tick

log = logging.getLogger("binancebot")
ET = ZoneInfo("America/New_York")


def us_weekend(now: datetime) -> bool:
    """미국 정규장이 쉬는 주말: 동부 금 16:00 ~ 월 9:30 (휴장일은 따지지 않음)."""
    e = now.astimezone(ET)
    hm = e.hour * 60 + e.minute
    return e.weekday() in (5, 6) or (e.weekday() == 4 and hm >= 16 * 60) or (e.weekday() == 0 and hm < 9 * 60 + 30)


FINAL =("FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH")


@dataclass
class SplitConfig:
    api_key: str = ""
    api_secret: str = ""
    dry_run: bool = True
    symbol: str = "SOXLUSDT"
    capital: float = 100.0        # 봇이 굴릴 시작 금액 (USDT). 계좌 잔고가 아니라 봇 장부로 복리 계산
    splits: int = 15              # 분할 수 N
    leverage: float = 1.0         # 차수당 금액 = 장부 금액 / N x 이 배수
    box_pct: float = 10.0         # 1차 ~ 박스 바닥 폭 (%)
    stop_steps: float = 2.0       # 손절 = 마지막 차수 - 간격 x 이 값
    tp_extra_pct: float = 0.1     # 익절 = 매수가 + 간격 + 매수가 x 이 %
    reentry_drop_pct: float = 2.0  # 전 차수 익절 뒤 고점에서 이 % 내려오면 재진입 (0 = 기다리지 않고 바로 재진입)
    reentry_limit: bool = True    # 재진입 1차를 시장가 대신 미리 거는 Post Only 지정가로 (2026-10-25 사용자 요청)
    weekend_first: bool = True    # 미국 주말(동부 금 16:00 ~ 월 9:30)에는 재진입가 = max(직전 1차 가격, 고점 -N%) (2026-10-26 사용자 요청)
    size_by_account: bool = True  # 새 사이클 차수당 금액을 장부 대신 선물 계좌 잔고(USDT 평가금액)로 (2026-10-27 사용자 요청)
    first_tp_steps: float = 1.0   # 1차만 익절 = 매수가 + 간격 x 이 값 + 0.1% (나머지 차수는 1간격)
    first_mult: float = 1.0       # 1차만 물량 x 이 값, 총 투입은 같게 = 차수당 금액 = 장부 / (N - 1 + 이 값) (2026-10-31 사용자 요청)
    exchange_leverage: int = 5    # 바이낸스 레버리지 설정값 (격리 마진 증거금용)
    margin_type: str = "ISOLATED"
    maker_fee: float = 0.0002     # 장부 계산용 수수료
    taker_fee: float = 0.0005
    poll_seconds: float = 2.0
    dry_equity: float = 0.0       # (사용 안 함, 호환용)
    state_dir: str = "state_binance_split"
    fvg_env: str = ".env.binance"  # 같은 계좌 FVG 봇 설정 (종목 겹침 확인용)

    @classmethod
    def from_env(cls) -> "SplitConfig":
        def f(name, attr, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            dry_run=_bool(os.environ.get("BINANCE_DRY_RUN"), True),
            symbol=_get("SP_SYMBOL", cls.symbol).upper(),
            capital=f("SP_CAPITAL", "capital"),
            splits=f("SP_SPLITS", "splits", int),
            leverage=f("SP_LEVERAGE", "leverage"),
            box_pct=f("SP_BOX_PCT", "box_pct"),
            stop_steps=f("SP_STOP_STEPS", "stop_steps"),
            tp_extra_pct=f("SP_TP_EXTRA_PCT", "tp_extra_pct"),
            reentry_drop_pct=f("SP_REENTRY_DROP", "reentry_drop_pct"),
            reentry_limit=_bool(os.environ.get("SP_REENTRY_LIMIT"), True),
            weekend_first=_bool(os.environ.get("SP_WEEKEND_FIRST"), True),
            size_by_account=_bool(os.environ.get("SP_SIZE_BY_ACCOUNT"), True),
            first_tp_steps=f("SP_FIRST_TP_STEPS", "first_tp_steps"),
            first_mult=f("SP_FIRST_MULT", "first_mult"),
            exchange_leverage=f("SP_EXCHANGE_LEVERAGE", "exchange_leverage", int),
            margin_type=_get("SP_MARGIN_TYPE", cls.margin_type).upper(),
            poll_seconds=f("SP_POLL_SECONDS", "poll_seconds"),
            state_dir=_get("SP_STATE_DIR", cls.state_dir),
            fvg_env=_get("SP_FVG_ENV", cls.fvg_env),
        )

    def describe(self) -> str:
        first = f" (1차는 +{self.first_tp_steps:g}간격)" if self.first_tp_steps != 1 else ""
        mult = f", 1차 물량 {self.first_mult:g}배" if self.first_mult != 1 else ""
        re = (f"전 차수 익절 뒤 고점 -{self.reentry_drop_pct:g}% 재진입" if self.reentry_drop_pct > 0
              else "전 차수 익절 뒤 바로 재진입")
        if self.reentry_drop_pct > 0 and self.reentry_limit:
            re += "(지정가)"
        if self.reentry_drop_pct > 0 and self.weekend_first:
            re += ", 미국 주말엔 직전 1차 가격이 더 높으면 그 가격"
        return (f"{self.symbol} 박스 {self.box_pct:g}% {self.splits}분할 x {self.leverage:g}배{mult}, "
                f"익절 +1간격+{self.tp_extra_pct:g}%{first}, 손절 마지막 차수 -{self.stop_steps:g}간격, "
                f"{re}, " + ("차수당 금액 = 선물 계좌 잔고 기준 " if self.size_by_account and not self.dry_run
                             else f"시작 금액 {self.capital:g} USDT 복리 ") +
                f"({'드라이런' if self.dry_run else '실제 주문'})")


AFTER_STOP_REQUEST = "after_stop_request.json"


@dataclass
class SplitState:
    stake: float = 0.0                 # 봇 장부 금액 (시작 금액 + 실현 손익 - 수수료)
    cycle: int = 0                     # 사이클 번호 (새로 시작할 때마다 +1)
    start_price: float = 0.0
    step: float = 0.0                  # 간격 d
    notional: float = 0.0              # 차수당 금액 (USDT)
    held: list = field(default_factory=list)   # 차수마다 None 또는 {"price", "qty"}
    waiting_high: float | None = None  # 전 차수 익절 뒤 고점 (재진입 대기)
    orders: dict = field(default_factory=dict)  # clientOrderId -> {"kind": buy/tp/entry, "k", "price", "qty"}
    stop_placed: bool = False
    seq: int = 0                       # 주문 번호
    tps: int = 0
    stops: int = 0
    pending_entry: bool = False        # 1차 진입 주문이 실패해서 다음 확인 때 다시 (손절·첫 진입)
    ended: str = ""                    # 멈춘 이유
    paused: str = ""                   # 포지션 불일치 등으로 새 주문 멈춤
    after_stop: dict = field(default_factory=dict)  # 다음 손절 때 한 번: 스플릿 대신 롱 {"lev", "stop_pct", "tp_pct"}
    long: dict = field(default_factory=dict)        # 그 롱 보유 중 {"entry", "qty", "stop", "tp"} (끝나면 재진입 대기로)

    def levels(self, n: int) -> list[float]:
        return [self.start_price - self.step * k for k in range(n)]


class SplitTrader:
    def __init__(self, client: BinanceClient, cfg: SplitConfig, now_fn=None, sleep_fn=_time.sleep):
        self.c, self.cfg = client, cfg
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep_fn
        self.dir = Path(cfg.state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.st = self.load()
        self.f = {"tick": 0.0, "step": 0.0, "min_notional": 5.0, "pct_up": 0.0, "pct_down": 0.0}
        self.last_check = 0.0
        self.last_price = 0.0

    # ------------------------------------------------------------ 장부
    def load(self) -> SplitState:
        if self.path.exists():
            st = SplitState(**json.loads(self.path.read_text(encoding="utf-8")))
        else:
            st = SplitState(stake=self.cfg.capital)
        n = self.cfg.splits
        if len(st.held) != n:
            st.held = (st.held + [None] * n)[:n]
        return st

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.st), ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def record(self, event: str, k: int | None, price: float, qty: float, pnl: float) -> None:
        p = self.dir / "trades.csv"
        new = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["time", "mode", "cycle", "event", "k", "price", "qty", "pnl", "stake"])
            w.writerow([self.now().strftime("%Y-%m-%d %H:%M:%S"), "dry" if self.cfg.dry_run else "live", self.st.cycle,
                        event, "" if k is None else k + 1, f"{price:.4f}", f"{qty:.6f}", f"{pnl:.4f}", f"{self.st.stake:.4f}"])

    # ------------------------------------------------------------ 값 계산
    def tp_price(self, k: int) -> float:
        b = self.st.held[k]["price"]
        steps = self.cfg.first_tp_steps if k == 0 else 1.0
        return round_tick(b + self.st.step * steps + b * self.cfg.tp_extra_pct / 100, self.f["tick"])

    def stop_price(self) -> float:
        return round_tick(self.st.levels(self.cfg.splits)[-1] - self.st.step * self.cfg.stop_steps, self.f["tick"])

    def qty_for(self, price: float, k: int = 1) -> float:
        mult = self.cfg.first_mult if k == 0 else 1.0
        return round_step(self.st.notional * mult / price, self.f["step"])

    def all_empty(self) -> bool:
        return all(h is None for h in self.st.held)

    def held_qty(self) -> float:
        return sum(h["qty"] for h in self.st.held if h)

    # ------------------------------------------------------------ 준비
    def prepare(self) -> None:
        info = self.c.exchange_info()
        for m in info.get("symbols", []):
            if m.get("symbol") == self.cfg.symbol:
                self.f = filters_of(m)
                self.f["min_notional"] = self.f["min_notional"] or 5.0
                break
        else:
            raise RuntimeError(f"바이낸스 선물에 {self.cfg.symbol} 가 없어요")
        if not self.cfg.dry_run:
            self.c.sync_time()
            if self.c.hedge_mode():
                raise RuntimeError("바이낸스 선물이 양방향(Hedge) 포지션 모드예요. 단방향(One-way)으로 바꿔 주세요")
            # 주문·포지션이 남아 있으면 바이낸스가 마진 방식·레버리지 변경을 거절함(-4067 등) → 이미 정해진 값으로 계속
            for fn, arg in ((self.c.set_margin_type, self.cfg.margin_type), (self.c.set_leverage, max(1, self.cfg.exchange_leverage))):
                try:
                    fn(self.cfg.symbol, arg)
                except BinanceError as exc:
                    log.info("마진·레버리지 설정을 바꾸지 못함 (기존 값으로 계속): %s", exc)

    def price(self) -> float:
        return float(self.c.prices()[self.cfg.symbol])

    # ------------------------------------------------------------ 사이클
    def entry_notional(self) -> float | None:
        """새 사이클의 차수당 금액 = 장부(또는 선물 계좌 잔고) / (N - 1 + 1차 배수) x 배율 (1차는 이 금액 x 1차 배수).
        최소 주문 금액 아래면 멈추고 None."""
        st, cfg = self.st, self.cfg
        base = st.stake
        if cfg.size_by_account and not cfg.dry_run:
            try:
                base = self.c.equity()  # 새 사이클 직전엔 SOXL 포지션이 없어 = 지갑 잔고 (입출금이 바로 반영됨)
            except BinanceError as exc:
                log.warning("계좌 잔고 조회 실패, 장부 금액 %.2f 로 계산: %s", st.stake, exc)
        notional = base / (cfg.splits - 1 + cfg.first_mult) * cfg.leverage
        if notional < self.f["min_notional"] * 1.05 or base <= 0:
            st.ended = f"차수당 금액 {notional:.2f} USDT 가 최소 주문 금액({self.f['min_notional']:g}) 아래 → 멈춤"
            log.warning(st.ended)
            self.save()
            return None
        return notional

    def open_cycle(self, price: float, notional: float) -> None:
        st, cfg = self.st, self.cfg
        st.cycle += 1
        st.start_price = round_tick(price, self.f["tick"])
        st.step = st.start_price * cfg.box_pct / 100 / cfg.splits
        st.notional = notional
        st.held = [None] * cfg.splits
        st.waiting_high = None
        st.stop_placed = False

    def log_cycle(self) -> None:
        st, cfg = self.st, self.cfg
        log.info("사이클 %d 시작: 1차 %.4f, 간격 %.4f, %d차 %.4f, 손절 %.4f, 차수당 %.2f USDT (장부 %.2f)",
                 st.cycle, st.held[0]["price"], st.step, cfg.splits, st.levels(cfg.splits)[-1], self.stop_price(),
                 st.notional, st.stake)

    def start_cycle(self, price: float) -> bool:
        """새 사이클: 간격·차수당 금액을 정하고 1차를 산다. 돈이 모자라면 False."""
        st = self.st
        notional = self.entry_notional()
        if notional is None:
            return False
        before = (st.cycle, st.start_price, st.step, st.notional, st.waiting_high)
        self.open_cycle(price, notional)
        if not self.buy_now(0, price, "entry"):  # 주문 오류 → 되돌리고 다음 확인 때 다시
            st.cycle, st.start_price, st.step, st.notional, st.waiting_high = before
            st.pending_entry = True
            self.save()
            return False
        st.pending_entry = False
        self.log_cycle()
        self.save()
        return True

    def apply_buy(self, k: int, price: float, qty: float, maker: bool) -> None:
        if qty <= 0:
            return
        fee = price * qty * (self.cfg.maker_fee if maker else self.cfg.taker_fee)
        self.st.stake -= fee
        h = self.st.held[k]
        if h:  # 부분 체결이 이어서 들어온 경우
            tot = h["qty"] + qty
            h["price"] = (h["price"] * h["qty"] + price * qty) / tot
            h["qty"] = tot
        else:
            self.st.held[k] = {"price": price, "qty": qty}
        log.info("매수 %d차 %.4f x %g (%s)", k + 1, price, qty, "지정가" if maker else "IOC·시장가")
        self.record("buy", k, price, qty, -fee)

    def apply_sell(self, k: int, price: float, qty: float, maker: bool, event: str = "tp") -> float:
        h = self.st.held[k]
        if not h or qty <= 0:
            return 0.0
        qty = min(qty, h["qty"])
        fee = price * qty * (self.cfg.maker_fee if maker else self.cfg.taker_fee)
        pnl = qty * (price - h["price"]) - fee
        self.st.stake += pnl
        h["qty"] -= qty
        if h["qty"] <= max(self.f["step"], 1e-12) / 2:
            self.st.held[k] = None
            if event == "tp":
                self.st.tps += 1
        log.info("%s %d차 %.4f → %.4f x %g 손익 %+.4f (장부 %.2f)", "익절" if event == "tp" else "손절", k + 1,
                 h["price"], price, qty, pnl, self.st.stake)
        self.record(event, k, price, qty, pnl)
        return pnl

    # ------------------------------------------------------------ 한 번 확인
    def tick(self) -> None:
        if self.st.ended:
            return
        self.read_after_stop_request()
        p = self.price()
        self.last_price = p
        if self.st.long:
            self.tick_long(p)
        elif self.cfg.dry_run:
            self.tick_dry(p)
        else:
            self.tick_live(p)
        self.save()

    def read_after_stop_request(self) -> None:
        """`split after-stop` 명령이 남긴 요청 파일을 읽어 다음 손절 때 할 롱을 걸어 두거나(빈 값이면) 끔."""
        req = self.dir / AFTER_STOP_REQUEST
        if not req.exists():
            return
        try:
            plan = json.loads(req.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError) as exc:
            log.warning("손절 뒤 롱 요청 파일을 못 읽음: %s", exc)
            plan = None
        req.unlink(missing_ok=True)
        if plan is None:
            return
        self.st.after_stop = plan
        if plan:
            log.warning("다음 손절 때 한 번: 스플릿을 쉬고 그 자리에서 평가금액 x %g배 롱, 손절 -%g%% / 익절 +%g%%",
                        float(plan["lev"]), float(plan["stop_pct"]), float(plan["tp_pct"]))
        else:
            log.info("손절 뒤 롱 예약 취소")
        self.save()

    def drop_after_stop(self) -> None:
        """손절 뒤 롱 예약은 '이번 사이클이 손절로 끝날 때'만. 모든 차수 익절로 끝나면 예약을 지움 (2026-10-31 사용자 요청)."""
        if self.st.after_stop:
            self.st.after_stop = {}
            log.warning("이번 사이클이 모든 차수 익절로 끝남 → 손절 뒤 롱 예약 취소")
            self.save()

    def tick_dry(self, p: float) -> None:
        st, n = self.st, self.cfg.splits
        if st.cycle == 0:
            self.start_cycle(p)
            return
        if st.waiting_high is not None:
            st.waiting_high = max(st.waiting_high, p)
            if p <= self.reentry_price():
                self.start_cycle(p)
            return
        if p <= self.stop_price() and not self.all_empty():
            self.stop_out(p)
            return
        for k in range(n):
            if st.held[k] and p >= self.tp_price(k):
                self.apply_sell(k, self.tp_price(k), st.held[k]["qty"], True)
        if self.all_empty():
            self.drop_after_stop()
            if self.cfg.reentry_drop_pct <= 0:
                log.info("모든 차수 익절 → 바로 재진입")
                self.start_cycle(p)
                return
            st.waiting_high = p
            log.info("모든 차수 익절 → 고점 -%g%% 에서 재진입 대기", self.cfg.reentry_drop_pct)
            return
        lv = st.levels(n)
        for k in range(1, n):
            if st.held[k] is None and p <= lv[k]:
                self.apply_buy(k, round_tick(lv[k], self.f["tick"]), self.qty_for(lv[k]), True)

    def stop_out(self, p: float, fill_px: float | None = None) -> None:
        """손절: 전부 시장가로 정리하고 그 가격에서 다시 시작.
        fill_px = 거래소 손절이 이미 체결된 것을 알아챘을 때 그 체결가 (새 사이클은 지금 가격에서 시작)."""
        st = self.st
        log.warning("손절선 %.4f 도달 (현재가 %.4f) → 전부 정리", self.stop_price(), p)
        px = p
        restart = p
        if not self.cfg.dry_run:
            stop_px = self.stop_price()
            self.cancel_everything()
            amt, _ = self.c.position_amt(self.cfg.symbol)
            if amt > 0:
                r = self.c.order(symbol=self.cfg.symbol, side="SELL", type="MARKET", quantity=fmt(round_step(amt, self.f["step"])),
                                 reduceOnly="true", newClientOrderId=self.cid("stop", 0), newOrderRespType="RESULT")
                px = self.fill_avg(r, p)
            else:  # 거래소 손절이 먼저 체결됨 → 그 체결가(모르면 손절가)로 기록, 새 사이클은 지금 가격에서
                px = fill_px or stop_px
            if amt <= 0 or fill_px:
                restart = p
            else:
                restart = px
        for k in range(self.cfg.splits):
            if st.held[k]:
                self.apply_sell(k, px, st.held[k]["qty"], False, "stop")
        st.held = [None] * self.cfg.splits
        st.stops += 1
        restart = restart if not self.cfg.dry_run else px
        if st.after_stop:  # 이번 손절 한 번만: 스플릿을 쉬고 그 자리에서 롱 (2026-10-31 사용자 요청)
            self.open_long(restart)
            return
        self.start_cycle(restart)

    # ------------------------------------------------------------ 손절 뒤 한 번 롱
    def open_long(self, p: float) -> None:
        """손절 자리에서 평가금액 x 배율 시장가 롱, 거래소 손절(-stop_pct%)·익절(+tp_pct%) 을 걸어 둠.
        끝나면(손절·익절) 스플릿은 '고점 -N% 재진입 대기'로 돌아감."""
        st, cfg, plan = self.st, self.cfg, self.st.after_stop
        base = st.stake
        if not cfg.dry_run:
            try:
                base = self.c.equity()
            except BinanceError as exc:
                log.warning("계좌 잔고 조회 실패, 장부 금액 %.2f 로 계산: %s", st.stake, exc)
        qty = round_step(base * float(plan["lev"]) / p, self.f["step"])
        if qty * p < self.f["min_notional"]:
            log.warning("손절 뒤 롱 금액 %.2f USDT 가 최소 주문 금액 아래 → 롱 없이 스플릿 계속", qty * p)
            st.after_stop = {}
            self.start_cycle(p)
            return
        entry = round_tick(p, self.f["tick"])
        if not cfg.dry_run:
            try:
                r = self.c.order(symbol=cfg.symbol, side="BUY", type="MARKET", quantity=fmt(qty),
                                 newClientOrderId=self.cid("long", 0), newOrderRespType="RESULT")
            except BinanceError as exc:
                log.warning("손절 뒤 롱 매수 실패, 다음 확인 때 다시: %s", exc)
                st.long = {"pending": True}
                self.save()
                return
            done = float(r.get("executedQty") or 0)
            if done <= 0:
                log.warning("손절 뒤 롱 매수가 체결되지 않음, 다음 확인 때 다시")
                st.long = {"pending": True}
                self.save()
                return
            entry, qty = self.fill_avg(r, p), done
        fee = entry * qty * cfg.taker_fee
        st.stake -= fee
        stop = round_tick(entry * (1 - float(plan["stop_pct"]) / 100), self.f["tick"])
        tp = round_tick(entry * (1 + float(plan["tp_pct"]) / 100), self.f["tick"])
        st.long = {"entry": entry, "qty": qty, "stop": stop, "tp": tp}
        st.after_stop = {}
        st.waiting_high = None
        self.record("long_buy", None, entry, qty, -fee)
        log.warning("스플릿 멈춤 → 손절 자리에서 롱 %.4f x %g (%.2f USDT, 평가금액 %.2f 의 %g배), 손절 %.4f / 익절 %.4f",
                    entry, qty, entry * qty, base, float(plan["lev"]), stop, tp)
        if not cfg.dry_run:
            for kind, px in (("STOP_MARKET", stop), ("TAKE_PROFIT_MARKET", tp)):
                try:
                    self.c.stop_market(cfg.symbol, "SELL", px, kind)
                    log.info("거래소 %s %.4f 걸어 둠", "손절" if kind == "STOP_MARKET" else "익절", px)
                except BinanceError as exc:
                    log.warning("거래소 %s 실패 (%s) → 봇이 가격 보고 정리", kind, exc)
        self.save()

    def tick_long(self, p: float) -> None:
        st, cfg, L = self.st, self.cfg, self.st.long
        if L.get("pending"):
            st.long = {}
            self.open_long(p)
            return
        if cfg.dry_run:
            if p <= L["stop"]:
                self.finish_long(L["stop"], p)
            elif p >= L["tp"]:
                self.finish_long(L["tp"], p)
            return
        amt, _ = self.c.position_amt(cfg.symbol)
        tol = max(self.f["step"] * 2, L["qty"] * 0.01)
        if amt <= tol:  # 거래소 손절·익절이 체결됨 (또는 앱에서 정리)
            px = self.stop_fill_price(L["qty"]) or (L["tp"] if abs(p - L["tp"]) < abs(p - L["stop"]) else L["stop"])
            self.finish_long(px, p)
            return
        if p <= L["stop"] or p >= L["tp"]:  # 거래소 주문이 없거나 아직 → 봇이 시장가로
            self.c.cancel_all(cfg.symbol)
            amt, _ = self.c.position_amt(cfg.symbol)
            px = p
            if amt > 0:
                r = self.c.order(symbol=cfg.symbol, side="SELL", type="MARKET", quantity=fmt(round_step(amt, self.f["step"])),
                                 reduceOnly="true", newClientOrderId=self.cid("longx", 0), newOrderRespType="RESULT")
                px = self.fill_avg(r, p)
            self.finish_long(px, p)

    def finish_long(self, px: float, p: float) -> None:
        st, L = self.st, self.st.long
        if not self.cfg.dry_run:
            self.c.cancel_all(self.cfg.symbol)  # 남은 거래소 손절·익절 취소
        pnl = L["qty"] * (px - L["entry"]) - px * L["qty"] * self.cfg.taker_fee
        st.stake += pnl
        event = "long_tp" if px >= L["entry"] else "long_stop"
        self.record(event, None, px, L["qty"], pnl)
        log.warning("손절 뒤 롱 %s: %.4f → %.4f x %g 손익 %+.4f (장부 %.2f) → 스플릿 재진입 대기",
                    "익절" if event == "long_tp" else "손절", L["entry"], px, L["qty"], pnl, st.stake)
        st.long = {}
        st.held = [None] * self.cfg.splits
        st.orders = {}
        st.stop_placed = False
        st.start_price = 0.0   # 주말 규칙(직전 1차 가격 이상)이 손절 전 사이클 가격을 쓰지 않게
        st.waiting_high = p
        self.save()

    # ------------------------------------------------------------ 실제 주문
    def cid(self, kind: str, k: int) -> str:
        self.st.seq += 1
        return f"sp{self.st.cycle}_{kind}{k}_{self.st.seq}"

    def cancel_everything(self) -> None:
        """이 종목 주문을 모두 취소하고, 취소 직전에 체결된 분량이 있으면 장부에 반영."""
        self.c.cancel_all(self.cfg.symbol)
        for cid, o in list(self.st.orders.items()):
            try:
                r = self.c.query_order(self.cfg.symbol, cid)
            except BinanceError as exc:
                log.warning("주문 조회 실패 %s: %s", cid, exc)
                continue
            if o["kind"] == "entry":
                self.settle_entry(cid, r, place_stop=False)
                continue
            done = float(r.get("executedQty") or 0)
            if done > 0:
                px = float(r.get("avgPrice") or 0) or o["price"]
                (self.apply_buy if o["kind"] == "buy" else self.apply_sell)(o["k"], px, done, True)
        self.st.orders = {}
        self.st.stop_placed = False

    def buy_now(self, k: int, price: float, kind: str) -> bool:
        """지금 바로 사기. 1차 진입(첫 진입·손절 뒤·재진입)은 시장가, 이미 지나친 차수는 그 차수 가격 IOC 지정가."""
        qty = self.qty_for(price, k)
        if self.cfg.dry_run:
            self.apply_buy(k, round_tick(price, self.f["tick"]), qty, False)
            return True
        limit = self.band_price("BUY", price, self.last_price or price)
        params = dict(symbol=self.cfg.symbol, side="BUY", quantity=fmt(qty), newClientOrderId=self.cid(kind, k),
                      newOrderRespType="RESULT")
        if kind == "entry":
            params["type"] = "MARKET"
        else:
            params.update(type="LIMIT", timeInForce="IOC", price=fmt(limit))
        try:
            r = self.c.order(**params)
        except BinanceError as exc:
            log.warning("%d차 매수 실패: %s", k + 1, exc)
            return False
        done = float(r.get("executedQty") or 0)
        if done > 0:
            self.apply_buy(k, self.fill_avg(r, limit), done, False)
            return True
        log.info("%d차 매수가 체결되지 않음 (%.4f)", k + 1, limit)
        return kind != "entry"

    def sync_orders(self) -> None:
        """걸어 둔 주문 중 거래소 미체결 목록에서 사라진 것 → 조회해서 체결분 반영."""
        if not self.st.orders:
            return
        open_ids = {o.get("clientOrderId") for o in self.c.open_orders(self.cfg.symbol)}
        for cid, o in list(self.st.orders.items()):
            if cid in open_ids:
                continue
            try:
                r = self.c.query_order(self.cfg.symbol, cid)
            except BinanceError as exc:
                log.warning("주문 조회 실패 %s: %s", cid, exc)
                continue
            # 방금 낸 주문은 미체결 목록에 늦게 보일 수 있음 → 끝난 주문(체결·취소·만료)만 정리
            # (2026-10-24 실전: 목록에 아직 없던 8차 매수를 사라진 것으로 보고 한 번 더 내서 0.11개가 두 번 사짐)
            if r.get("status") not in FINAL:
                continue
            if o["kind"] == "entry":
                self.settle_entry(cid, r)
                continue
            done = float(r.get("executedQty") or 0)
            px = float(r.get("avgPrice") or 0) or o["price"]
            if done > 0:
                if o["kind"] == "buy":
                    self.apply_buy(o["k"], px, done, True)
                else:
                    self.apply_sell(o["k"], px, done, True)
            del self.st.orders[cid]

    def reentry_price(self) -> float:
        """재진입 1차 가격 = 고점 -N%. 미국 주말에는 직전 사이클 1차 가격이 더 높으면 그 가격 (2026-10-26 사용자 요청,
        백테스트 20분할 3배 634 → 731$ 지만 한국 주말 기준·15분할에서는 나빠지는 등 방향이 일정하지 않았음)."""
        st, tick = self.st, self.f["tick"]
        trig = round_tick(st.waiting_high * (1 - self.cfg.reentry_drop_pct / 100), tick)
        if self.cfg.weekend_first and st.start_price and us_weekend(self.now()):
            trig = max(trig, round_tick(st.start_price, tick))
        return trig

    def reentry_limit(self, p: float) -> None:
        """전 차수 익절 뒤 재진입 1차를 '고점 -N%' 가격에 미리 거는 Post Only 지정가로 (2026-10-25 사용자 요청).
        고점이 올라 재진입 가격이 0.1% 넘게 올라가면 취소하고 새 가격에 다시 건다. 가격이 이미 지나쳤으면 시장가."""
        st, tick = self.st, self.f["tick"]
        trig = self.reentry_price()
        cid = self.order_for("entry", 0)
        if cid:
            o = st.orders[cid]
            moved = trig - o["price"] >= max(tick, o["price"] * 0.001) or trig < o["price"] - tick / 2  # 고점 갱신 · 주말 끝
            if p <= o["price"] * 1.0005 or not moved:
                return  # 체결을 기다림
            self.cancel_entry(cid)
            if st.waiting_high is None:  # 취소 직전에 체결돼서 사이클이 시작됨
                return
        if p <= trig * (1 - 0.0005):
            if self.start_cycle(p):
                self.place_stop()
            return
        if trig < p * (1 - (self.f["pct_down"] or 0.05) * 0.9):
            return  # 지정가 허용 범위 밖 → 가까워지면 건다
        notional = self.entry_notional()
        if notional is None:
            return
        qty = round_step(notional * self.cfg.first_mult / trig, self.f["step"])
        new = self.cid("entry", 0)
        try:
            self.c.order(symbol=self.cfg.symbol, side="BUY", type="LIMIT", timeInForce="GTX", price=fmt(trig),
                         quantity=fmt(qty), newClientOrderId=new)
        except BinanceError as exc:
            log.info("재진입 1차 지정가 %.4f 못 걸음: %s", trig, exc)
            return
        st.orders[new] = {"kind": "entry", "k": 0, "price": trig, "qty": qty, "notional": notional}
        log.info("재진입 1차 지정가 %.4f x %g 걸어 둠 (고점 %.4f)", trig, qty, st.waiting_high)

    def cancel_entry(self, cid: str) -> None:
        try:
            self.c.cancel_order(self.cfg.symbol, cid)
        except BinanceError as exc:
            log.info("재진입 지정가 취소 응답: %s", exc)
        try:
            r = self.c.query_order(self.cfg.symbol, cid)
        except BinanceError as exc:
            log.warning("주문 조회 실패 %s: %s", cid, exc)
            return
        if r.get("status") in FINAL:
            self.settle_entry(cid, r)

    def settle_entry(self, cid: str, r: dict, place_stop: bool = True) -> None:
        """끝난 재진입 지정가: 체결분이 있으면 그 주문 가격을 1차로 새 사이클 시작."""
        o = self.st.orders.pop(cid, None)
        done = float(r.get("executedQty") or 0)
        if not o or done <= 0:
            return
        self.open_cycle(o["price"], o["notional"])
        self.apply_buy(0, float(r.get("avgPrice") or 0) or o["price"], done, True)
        self.log_cycle()
        if place_stop:
            self.place_stop()

    def order_for(self, kind: str, k: int):
        for cid, o in self.st.orders.items():
            if o["kind"] == kind and o["k"] == k:
                return cid
        return None

    def place_limit(self, kind: str, k: int, price: float, qty: float) -> None:
        side = "BUY" if kind == "buy" else "SELL"
        params = dict(symbol=self.cfg.symbol, side=side, type="LIMIT", timeInForce="GTX", price=fmt(price),
                      quantity=fmt(qty), newClientOrderId=self.cid(kind, k))
        if kind == "tp":
            params["reduceOnly"] = "true"
        try:
            self.c.order(**params)
        except BinanceError as exc:
            log.info("%d차 %s 지정가 %.4f 못 걸음: %s", k + 1, "매수" if kind == "buy" else "익절", price, exc)
            return
        self.st.orders[params["newClientOrderId"]] = {"kind": kind, "k": k, "price": price, "qty": qty}

    def check_position(self) -> None:
        """30초마다 거래소 포지션 수량 = 장부 보유 수량인지 확인. 다르면 새 주문을 멈춤."""
        if _time.monotonic() - self.last_check < 30 or self.order_for("entry", 0):
            return  # 재진입 지정가가 걸려 있는 동안은 부분 체결이 있을 수 있어 건너뜀
        self.last_check = _time.monotonic()
        amt, _ = self.c.position_amt(self.cfg.symbol)
        mine = self.held_qty()
        tol = max(self.f["step"] * 2, mine * 0.01)
        if abs(amt - mine) > tol:  # 방금 체결된 주문일 수 있으니 한 번 더 맞춰 보고 판단
            self.sync_orders()
            amt, _ = self.c.position_amt(self.cfg.symbol)
            mine = self.held_qty()
        if abs(amt - mine) > tol and amt <= tol and mine > tol and self.st.stop_placed:
            # 포지션이 0 인데 장부엔 남아 있음 = 거래소 손절이 (봇이 꺼져 있던 사이 등) 먼저 체결됨 → 멈추지 말고 손절로 처리
            self.ex_stop = self.stop_fill_price(mine) or 0.0
            log.warning("거래소 손절이 이미 체결돼 있음 (체결가 %s) → 손절로 기록하고 새 사이클",
                        f"{self.ex_stop:.4f}" if self.ex_stop else "모름")
            if self.st.paused:
                self.st.paused = ""
            return
        if abs(amt - mine) > tol:
            if not self.st.paused:
                self.st.paused = f"거래소 포지션 {amt:g} ≠ 장부 {mine:g} (다른 봇·앱에서 같은 종목을 사고팔았는지 확인)"
                log.error("새 주문 멈춤: %s", self.st.paused)
        elif self.st.paused:
            log.info("포지션이 다시 맞아서 이어서 진행")
            self.st.paused = ""

    def stop_fill_price(self, qty: float) -> float | None:
        """가장 최근 매도 주문(거래소 손절)의 체결 평균가. 수량이 장부와 맞지 않으면 None."""
        now = _time.time() * 1000
        sells = []
        for hours in (3, 24, 24 * 7):  # 최근부터 넓혀 가며 (시작 시각 없이 조회하면 오래된 체결부터 옴)
            try:
                got = self.c.fills(self.cfg.symbol, start_ms=now - hours * 3600_000, limit=1000)
            except BinanceError as exc:
                log.warning("체결 내역 조회 실패: %s", exc)
                return None
            sells = [t for t in got if t.get("side", "SELL") == "SELL"]
            if sells and len(got) < 1000:
                break
        if not sells:
            return None
        oid = sells[-1]["orderId"]
        part = [t for t in sells if t["orderId"] == oid]
        q = sum(float(t["qty"]) for t in part)
        if abs(q - qty) > max(self.f["step"] * 2, qty * 0.01):
            return None
        return sum(float(t["price"]) * float(t["qty"]) for t in part) / q

    def tick_live(self, p: float) -> None:
        st, n, cfg = self.st, self.cfg.splits, self.cfg
        self.sync_orders()
        if st.cycle == 0 or st.pending_entry:
            if self.start_cycle(p):
                self.place_stop()
            return
        self.ex_stop = None
        self.check_position()
        if self.ex_stop is not None:
            self.stop_out(p, self.ex_stop or None)
            if not st.ended and not st.pending_entry:
                self.place_stop()
            return
        if st.paused:
            return
        if st.waiting_high is not None:
            st.waiting_high = max(st.waiting_high, p)
            if cfg.reentry_limit:
                self.reentry_limit(p)
            elif p <= self.reentry_price():
                if self.start_cycle(p):
                    self.place_stop()
            return
        if not self.all_empty() and p <= self.stop_price():
            self.stop_out(p)
            if not st.ended and not st.pending_entry:
                self.place_stop()
            return
        if self.all_empty():
            self.cancel_everything()   # 아래 차수에 걸어 둔 매수·손절 취소 (취소 직전 체결분은 반영)
            if self.all_empty():
                self.drop_after_stop()
                if cfg.reentry_drop_pct <= 0:
                    log.info("모든 차수 익절 → 바로 재진입")
                    if self.start_cycle(p):
                        self.place_stop()
                    return
                st.waiting_high = p
                log.info("모든 차수 익절 → 고점 -%g%% 에서 재진입 대기", cfg.reentry_drop_pct)
                return
            self.place_stop()
        up = (self.f["pct_up"] or 0.05) * 0.9
        down = (self.f["pct_down"] or 0.05) * 0.9
        lv = st.levels(n)
        tick = self.f["tick"]
        for k in range(n):
            h = st.held[k]
            if h:  # 익절
                tp = self.tp_price(k)
                if self.order_for("tp", k) or self.order_for("buy", k):
                    continue
                if p >= tp * (1 + 0.0005):  # 이미 지나침 → IOC
                    self.sell_ioc(k, tp)
                elif tp <= p * (1 + up):
                    self.place_limit("tp", k, tp, round_step(h["qty"], self.f["step"]))
            elif k > 0:  # 매수
                level = round_tick(lv[k], tick)
                if self.order_for("buy", k):
                    continue
                if p <= level * (1 - 0.0005):
                    self.buy_now(k, level, "buy")
                elif level >= p * (1 - down):
                    self.place_limit("buy", k, level, self.qty_for(level))

    def fill_avg(self, r: dict, fallback: float) -> float:
        """시장가·IOC 주문의 실제 평균 체결가: 체결 내역(userTrades)에서, 없으면 응답 avgPrice, 그것도 없으면 주문가.
        (2026-10-25 실전: IOC 응답으로는 82.71 이 기록됐지만 실제 체결은 85.00)"""
        oid = r.get("orderId")
        fn = getattr(self.c, "fills", None)
        if oid and fn:
            try:
                rows = [t for t in fn(self.cfg.symbol, order_id=oid) if str(t.get("orderId")) == str(oid)]
                q = sum(float(t["qty"]) for t in rows)
                if q > 0:
                    return sum(float(t["price"]) * float(t["qty"]) for t in rows) / q
            except BinanceError as exc:
                log.info("체결 내역 조회 실패 (%s), 응답 가격으로 기록", exc)
        avg = float(r.get("avgPrice") or 0)
        return avg if avg > 0 else fallback

    def band_price(self, side: str, price: float, p: float) -> float:
        """지정가 허용 범위(현재가 ±%) 안으로 당김. 이미 지나친 익절·매수를 IOC 로 받을 때 범위 밖이면 거절되기 때문
        (2026-10-25 실전: 81.78 익절 IOC 가 '82.47 보다 낮을 수 없음'(-4024)으로 거절)."""
        if side == "SELL":
            lo = p * (1 - (self.f["pct_down"] or 0.05) * 0.9)
            return round_tick(max(price, lo), self.f["tick"])
        hi = p * (1 + (self.f["pct_up"] or 0.05) * 0.9)
        return round_tick(min(price, hi), self.f["tick"])

    def sell_ioc(self, k: int, price: float) -> None:
        h = self.st.held[k]
        price = self.band_price("SELL", price, self.last_price or price)
        try:
            r = self.c.order(symbol=self.cfg.symbol, side="SELL", type="LIMIT", timeInForce="IOC", price=fmt(price),
                             quantity=fmt(round_step(h["qty"], self.f["step"])), reduceOnly="true",
                             newClientOrderId=self.cid("tpioc", k), newOrderRespType="RESULT")
        except BinanceError as exc:
            log.warning("%d차 익절 IOC 실패: %s", k + 1, exc)
            return
        done = float(r.get("executedQty") or 0)
        if done > 0:
            self.apply_sell(k, self.fill_avg(r, price), done, False)

    def place_stop(self) -> None:
        """거래소 손절(STOP_MARKET 전량)을 사이클 시작 때 걸어 둠. 실패하면 봇이 가격을 보고 직접 손절."""
        if self.cfg.dry_run or self.st.stop_placed or self.st.ended or self.st.long:
            return
        try:
            self.c.stop_market(self.cfg.symbol, "SELL", self.stop_price())
            self.st.stop_placed = True
            log.info("거래소 손절 %.4f 걸어 둠", self.stop_price())
        except BinanceError as exc:
            log.warning("거래소 손절 실패 (%s) → 봇이 가격 보고 손절", exc)

    # ------------------------------------------------------------ 반복
    def run(self, max_ticks: int | None = None) -> None:
        self.prepare()
        log.info("스플릿 봇 시작: %s", self.cfg.describe())
        i = 0
        while not self.st.ended and (max_ticks is None or i < max_ticks):
            try:
                self.tick()
            except BinanceError as exc:
                log.warning("바이낸스 오류, 잠시 뒤 다시: %s", exc)
            except Exception as exc:  # noqa: BLE001 네트워크 등은 이어서
                log.warning("오류, 잠시 뒤 다시: %s", exc)
            i += 1
            self.sleep(self.cfg.poll_seconds)

    def status(self) -> str:
        st, n = self.st, self.cfg.splits
        lines = [f"설정: {self.cfg.describe()}",
                 f"장부 금액 {st.stake:.2f} USDT (시작 {self.cfg.capital:g}), 사이클 {st.cycle}, 익절 {st.tps}번, 손절 {st.stops}번"]
        if st.ended:
            lines.append(f"멈춤: {st.ended}")
        if st.paused:
            lines.append(f"새 주문 멈춤: {st.paused}")
        if st.after_stop:
            a = st.after_stop
            lines.append(f"다음 손절 때 한 번: 스플릿 멈추고 그 자리에서 {float(a['lev']):g}배 롱, "
                         f"손절 -{float(a['stop_pct']):g}% / 익절 +{float(a['tp_pct']):g}%")
        if st.long.get("entry"):
            L = st.long
            lines.append(f"스플릿 멈춤 · 롱 보유 {L['entry']:.4f} x {L['qty']:g}, 손절 {L['stop']:.4f} / 익절 {L['tp']:.4f} "
                         "(끝나면 고점 -N% 재진입 대기)")
            return "\n".join(lines)
        if st.waiting_high is not None:
            wk = " (미국 주말: 직전 1차 가격 이상)" if self.cfg.weekend_first and us_weekend(self.now()) else ""
            lines.append(f"재진입 대기: 고점 {st.waiting_high:.4f} → {self.reentry_price():.4f} 에서 1차{wk}")
            for o in st.orders.values():
                if o["kind"] == "entry":
                    lines.append(f"재진입 1차 지정가 {o['price']:.4f} x {o['qty']:g} 걸려 있음")
        elif st.cycle:
            lv = st.levels(n)
            held = [f"{k + 1}차 {h['price']:.2f}" for k, h in enumerate(st.held) if h]
            lines.append(f"1차 {lv[0]:.4f} ~ {n}차 {lv[-1]:.4f} (간격 {st.step:.4f}), 손절 {self.stop_price():.4f}, "
                         f"차수당 {st.notional:.2f} USDT")
            lines.append("보유: " + (", ".join(held) if held else "없음"))
        return "\n".join(lines)


def fvg_conflict(cfg: SplitConfig) -> str:
    """같은 API 키로 FVG 봇이 돌고 이 종목이 FVG 대상에서 빠져 있지 않으면 경고 문구."""
    p = Path(cfg.fvg_env)
    if not p.exists():
        return ""
    env = {}
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    if not cfg.api_key or env.get("BINANCE_API_KEY") != cfg.api_key:
        return ""
    excl = [s.strip().upper() for s in env.get("BN_EXCLUDE", "SOXSUSDT,SQQQUSDT,SKDDUSDT").split(",")]
    if cfg.symbol in excl:
        return ""
    return (f"FVG 봇({cfg.fvg_env})과 같은 API 키인데 {cfg.symbol} 가 FVG 대상에서 빠져 있지 않아요. "
            f"FVG 봇이 {cfg.symbol} 를 사면 포지션이 섞이고 주문이 취소돼요. "
            f"{cfg.fvg_env} 의 BN_EXCLUDE 에 {cfg.symbol} 를 넣거나 서브 계정 키를 쓰세요.")
