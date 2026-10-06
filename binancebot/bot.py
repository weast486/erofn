"""바이낸스 미국 주식 선물 자동매매 — 첫 5분봉 + 1분봉 FVG (사용자 선택 기준, 2026-10-05 백테스트 +500%).

백테스트 하루 2종목 x 5배(2026-06~09, 1000달러): 67,673달러, 강제청산 없음, MDD -69%, 하루 최악 -52%.

규칙 (시각은 미국 동부):
  1. 장 전: 바이낸스 주식 토큰(EQUITY) 중 전날 정규장 거래대금 상위 N(기본 10) + 추가 종목(BN_EXTRA_SYMBOLS)
  2. 첫 5분봉(9:30~9:35) 고가·저가
  3. 9:35 부터 1분봉 FVG(3봉 갭)가 고가 위에 생기면 롱, 저가 아래면 숏 — 종목마다 그날 첫 FVG 만
  4. 가격이 갭을 다 메운 곳(롱 = 1번째 봉 고가)에 닿으면 그 가격 지정가(IOC)로 진입 — 하루 최대 2종목(먼저 닿은 순,
     같은 순간이면 ADR = 20일 평균 변동폭 높은 순)
  5. 손절 = 롱은 첫 5분봉 저가, 숏은 고가 (거래소 STOP_MARKET + 봇이 가격 보고 한 번 더 확인)
     익절 = 진입가 ± 손절 거리 x 2 (지정가, 가격 허용 범위 안에 들어오면 주문)
  6. 9:30 + 350분(15:20)에 남은 주문 취소·시장가 정리
수량 = 진입 때 평가금액 x 5배 / 진입가 (2종목이면 동시에 최대 10배). 교차 마진, 바이낸스 레버리지 설정 20
(5배씩 두 종목의 증거금이 모자라지 않게). 기본은 드라이런(주문 없이 로그만).
  7. (BN_ON_ENABLED) 종가 매매: 15:58 에 장중 -10% 이하·저가 근처 마감 종목을 평가금액 x 1배씩 최대 2종목 매수,
     비상 손절 -15%, 다음 거래일 9:30 시장가 정리 — overnight.py
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from tossbot.config import _bool, _get

from .backtest import ET, US_EARLY_CLOSE, US_HOLIDAYS, MinBar, fvg_at
from .client import BinanceClient, BinanceError, filters_of, fmt, round_step, round_tick

log = logging.getLogger("binancebot")


@dataclass
class BotConfig:
    api_key: str = ""
    api_secret: str = ""
    dry_run: bool = True          # true 면 주문 없이 로그만
    leverage: float = 5.0         # 종목마다 평가금액 x 이 배수만큼 진입 (2종목이면 합계 최대 10배)
    exchange_leverage: int = 20   # 바이낸스 레버리지 설정값 (증거금 계산용; 교차 마진이라 청산 위험은 실제 진입 금액이 결정)
    max_trades: int = 2           # 하루 최대 종목 수 (먼저 체결된 순, 2종목이면 동시에 최대 4배)
    top_n: int = 10               # 전날 정규장 거래대금 상위 N
    extra_symbols: str = ""       # 순위와 상관없이 늘 넣을 종목 (예 BTCUSDT,ETHUSDT — 백테스트 안 됨)
    exclude: str = "SOXSUSDT,SQQQUSDT,SKDDUSDT"  # 순위에서 뺄 종목: 롱 짝(SOXL·TQQQ·SKUU)이 있는 인버스 ETF
    target_r: float = 2.0         # 익절 = 손절 거리 x R (0 = 익절 없음)
    exit_minutes: int = 350       # 9:30 부터 N분 뒤 정리 (350 = 15:20)
    side: str = "both"            # both / long / short
    entry: str = "full"           # full = 갭 다 메움 / mid / edge
    loc: str = "zone"             # zone = 갭 전체가 첫 봉 밖 / mid
    poll_seconds: float = 1.0
    dry_equity: float = 1000.0    # 드라이런인데 키가 없을 때 쓸 평가금액 (USDT)
    state_dir: str = "state_binance"
    # 종가 매매(오버나이트, 롱만 — overnight.py): 장중 -on_drop% 이하 + 저가 근처 마감 종목을 15:58 에 사서 다음 거래일 9:30 에 팖
    on_enabled: bool = False
    on_leverage: float = 1.0      # 종목마다 평가금액 x 이 배수
    on_max: int = 2               # 하루 최대 종목 수 (많이 내린 순)
    on_drop: float = 10.0         # 장중 등락(현재가/시가-1) 이 -N% 이하
    on_pos: float = 0.1           # 종가 위치 (현재가-저가)/(고가-저가) 가 이 값 미만 (0 = 저가)
    on_stop: float = 15.0         # 거래소 비상 손절 = 매수가 -N%
    on_min_qv: float = 1_000_000  # 오늘 정규장 거래대금(USDT) 하한

    @classmethod
    def from_env(cls) -> "BotConfig":
        def f(name, attr, typ=float):
            return typ(_get(name, str(getattr(cls, attr))))

        return cls(
            api_key=os.environ.get("BINANCE_API_KEY", ""),
            api_secret=os.environ.get("BINANCE_API_SECRET", ""),
            dry_run=_bool(os.environ.get("BINANCE_DRY_RUN"), True),
            leverage=f("BN_LEVERAGE", "leverage"),
            exchange_leverage=f("BN_EXCHANGE_LEVERAGE", "exchange_leverage", int),
            max_trades=f("BN_MAX_TRADES", "max_trades", int),
            top_n=f("BN_TOP_N", "top_n", int),
            extra_symbols=_get("BN_EXTRA_SYMBOLS", cls.extra_symbols),
            exclude=os.environ.get("BN_EXCLUDE", cls.exclude),
            target_r=f("BN_TARGET_R", "target_r"),
            exit_minutes=f("BN_EXIT_MINUTES", "exit_minutes", int),
            side=_get("BN_SIDE", cls.side),
            entry=_get("BN_ENTRY", cls.entry),
            loc=_get("BN_LOC", cls.loc),
            poll_seconds=f("BN_POLL_SECONDS", "poll_seconds"),
            dry_equity=f("BN_DRY_EQUITY", "dry_equity"),
            state_dir=_get("BN_STATE_DIR", cls.state_dir),
            on_enabled=_bool(os.environ.get("BN_ON_ENABLED"), False),
            on_leverage=f("BN_ON_LEVERAGE", "on_leverage"),
            on_max=f("BN_ON_MAX", "on_max", int),
            on_drop=f("BN_ON_DROP", "on_drop"),
            on_pos=f("BN_ON_POS", "on_pos"),
            on_stop=f("BN_ON_STOP", "on_stop"),
            on_min_qv=f("BN_ON_MIN_QV", "on_min_qv"),
        )

    def describe(self) -> str:
        extra = f" + {self.extra_symbols}" if self.extra_symbols else ""
        return (f"전날 거래대금 상위 {self.top_n}{extra}, 첫 5분봉 + 1분봉 FVG({self.entry}) {self.side}, "
                f"손절 첫 봉 반대편 / 익절 {self.target_r:g}R / 9:30+{self.exit_minutes}분 정리, "
                f"종목당 평가금액 x {self.leverage:g}배, 하루 최대 {self.max_trades}종목"
                + (f" + 종가 매매(장중 -{self.on_drop:g}%↓·저가 근처 마감 {self.on_max}종목 x {self.on_leverage:g}배, "
                   f"손절 -{self.on_stop:g}%, 다음 거래일 9:30 정리)" if self.on_enabled else "")
                + f" ({'드라이런' if self.dry_run else '실제 주문'})")


def trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not trading_day(d):
        d -= timedelta(days=1)
    return d


def at(d: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm, ss), ET)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def to_bars(rows: list[list], now: datetime | None = None) -> list[MinBar]:
    """klines → MinBar (끝난 봉만)."""
    out = []
    for k in rows:
        if now is not None and int(k[6]) >= ms(now):
            continue
        out.append(MinBar(datetime.fromtimestamp(int(k[0]) / 1000, ET), float(k[1]), float(k[2]), float(k[3]),
                          float(k[4]), float(k[5])))
    return out


def first_fvg(bars: list[MinBar], h1: float, l1: float, cfg: BotConfig) -> tuple[int, float, datetime] | None:
    """9:35 이후 봉들에서 그날 첫 FVG (방향, 진입가, 3번째 봉 시각)."""
    for i in range(2, len(bars)):
        found = fvg_at(bars[i - 2], bars[i - 1], bars[i], h1, l1, cfg.loc, cfg.entry, cfg.side)
        if found:
            return found[0][0], found[0][1], bars[i].t
    return None


@dataclass
class DayState:
    day: str
    phase: str = "new"                 # new → watch → position → done
    candidates: list = field(default_factory=list)
    ranges: dict = field(default_factory=dict)     # 종목 → [고가, 저가]
    signals: dict = field(default_factory=dict)    # 종목 → [방향, 진입가, FVG 시각]
    adr: dict = field(default_factory=dict)        # 종목 → 전날까지 20거래일 정규장 (고가/저가-1)% 평균
    dropped: list = field(default_factory=list)
    positions: dict = field(default_factory=dict)  # 종목 → 진입·손절·익절·결과
    result: dict = field(default_factory=dict)


class FvgTrader:
    def __init__(self, client: BinanceClient, cfg: BotConfig, now_fn=None, sleep_fn=_time.sleep):
        self.c, self.cfg = client, cfg
        self.now = now_fn or (lambda: datetime.now(ET))
        self.sleep = sleep_fn
        self.meta: dict[str, dict] = {}
        Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
        from .overnight import Overnight

        self.overnight = Overnight(self)

    # ------------------------------------------------------------ 상태 파일
    def _path(self, day: date) -> Path:
        return Path(self.cfg.state_dir) / f"day_{day.isoformat()}.json"

    def load(self, day: date) -> DayState:
        p = self._path(day)
        if p.exists():
            return DayState(**json.loads(p.read_text(encoding="utf-8")))
        return DayState(day.isoformat())

    def save(self, st: DayState) -> None:
        self._path(date.fromisoformat(st.day)).write_text(json.dumps(asdict(st), ensure_ascii=False, indent=1),
                                                          encoding="utf-8")

    # ------------------------------------------------------------ 준비
    def load_meta(self) -> None:
        info = self.c.exchange_info()
        want = {s.strip().upper() for s in self.cfg.extra_symbols.split(",") if s.strip()}
        self.meta = {s["symbol"]: s for s in info.get("symbols", [])
                     if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT"
                     and (s.get("underlyingType") == "EQUITY" or s["symbol"] in want)}

    def candidates(self, day: date) -> list[str]:
        """전날 정규장(9:30~마감) 거래대금 상위 N + 추가 종목."""
        if not self.meta:
            self.load_meta()
        prev = prev_trading_day(day)
        close = time(13, 0) if prev in US_EARLY_CLOSE else time(16, 0)
        start, end = at(prev, 9, 30), datetime.combine(prev, close, ET)
        n_min = int((end - start).total_seconds() // 60)
        want = {s.strip().upper() for s in self.cfg.extra_symbols.split(",") if s.strip()}
        qv = {}
        skip = {s.strip().upper() for s in self.cfg.exclude.split(",") if s.strip()}
        for sym, m in self.meta.items():
            if m.get("underlyingType") != "EQUITY" or sym in skip:
                continue
            try:
                rows = self.c.klines(sym, ms(start), limit=n_min)
            except BinanceError as exc:  # 한 종목 조회가 계속 실패해도 나머지로 순위 계산
                log.warning("%s 전날 1분봉 조회 실패, 순위에서 빠짐 (%s)", sym, exc)
                continue
            qv[sym] = sum(float(k[7]) for k in rows if int(k[0]) < ms(end))
            self.sleep(0.05)
        top = [s for s, v in sorted(qv.items(), key=lambda kv: -kv[1]) if v > 0][: self.cfg.top_n]
        log.info("전날(%s) 거래대금 상위 %d: %s", prev, self.cfg.top_n,
                 ", ".join(f"{s} {qv[s] / 1e6:,.0f}M" for s in top))
        return top + sorted(want - set(top))

    def adr(self, day: date, symbols: list[str], n: int = 20) -> dict[str, float]:
        """ADR = 전날까지 n 거래일 정규장 (고가/저가 - 1)% 평균 (30분봉으로 계산, 정규장 9:30~16:00 이 30분 단위로 맞음)."""
        start = at(day - timedelta(days=int(n * 1.6) + 7), 9, 30)
        out = {}
        for sym in symbols:
            hl: dict[date, list[float]] = {}
            for k in self.c.klines(sym, ms(start), limit=1500, interval="30m"):
                t = datetime.fromtimestamp(int(k[0]) / 1000, ET)
                d = t.date()
                close = time(13, 0) if d in US_EARLY_CLOSE else time(16, 0)
                if d >= day or not trading_day(d) or not (time(9, 30) <= t.time() < close):
                    continue
                h, l = hl.get(d, [0.0, float("inf")])
                hl[d] = [max(h, float(k[2])), min(l, float(k[3]))]
            days = sorted(hl)[-n:]
            if len(days) >= 5:
                out[sym] = sum((hl[d][0] / hl[d][1] - 1) * 100 for d in days) / len(days)
            self.sleep(0.05)
        log.info("ADR(20일 평균 변동폭): %s", ", ".join(f"{s} {v:.1f}%" for s, v in sorted(out.items(), key=lambda kv: -kv[1])))
        return out

    def equity(self) -> float:
        if self.cfg.api_key and self.cfg.api_secret:
            return self.c.equity()
        return self.cfg.dry_equity

    # ------------------------------------------------------------ 하루 실행
    def wait_until(self, t: datetime) -> None:
        while self.now() < t:
            self.sleep(min(30.0, max(0.2, (t - self.now()).total_seconds())))

    def run(self, day: date | None = None) -> DayState:
        """하루 전체: (9:30 종가 매매 정리) → 낮 FVG 매매 → (15:58 종가 매수)."""
        day = day or self.now().date()
        st = self.run_fvg(day)
        if trading_day(day):
            self.overnight.exit_open(day)   # FVG 쪽이 9:30 전에 끝났거나 쉬는 날(조기 마감 등)이어도 정리
            self.overnight.enter_close(day)
        return st

    def run_fvg(self, day: date) -> DayState:
        st = self.load(day)
        if not trading_day(day):
            log.info("%s 은 미국 휴장일(주말·공휴일)이라 쉽니다", day)
            return st
        if day in US_EARLY_CLOSE:
            log.info("%s 은 13시 조기 마감이라 쉽니다", day)
            return st
        if st.phase == "done":
            log.info("%s 은 이미 끝났어요: %s", day, st.result)
            return st
        exit_t = at(day, 9, 30) + timedelta(minutes=self.cfg.exit_minutes)
        if self.now() >= exit_t and not self.open_positions(st):
            log.info("정리 시각(%s ET)이 지나 오늘은 쉽니다", exit_t.strftime("%H:%M"))
            return st
        log.info("설정: %s", self.cfg.describe())
        if not self.meta:
            self.load_meta()
        if not st.candidates:
            self.wait_until(at(day, 9, 15))
            st.candidates = self.candidates(day)
            st.adr = self.adr(day, st.candidates)
            st.phase = "watch"
            self.save(st)
        self.overnight.exit_open(day)  # 전날 종가에 산 종목은 9:30 에 정리 (없으면 아무것도 안 함)
        self.wait_until(at(day, 9, 35, 3))
        for sym in st.candidates:
            if sym in st.ranges or self.now() >= exit_t:
                continue
            bars = [b for b in to_bars(self.c.klines(sym, ms(at(day, 9, 30)), limit=5), self.now())
                    if b.t < at(day, 9, 35)]
            if bars:
                st.ranges[sym] = [max(b.h for b in bars), min(b.l for b in bars)]
        if st.ranges:
            log.info("첫 5분봉: %s", ", ".join(f"{s} {h:g}~{l:g}" for s, (h, l) in st.ranges.items()))
        self.save(st)
        return self.trade_day(st, day, exit_t)

    @staticmethod
    def open_positions(st: DayState) -> list[dict]:
        return [p for p in st.positions.values() if not p.get("result")]

    def trade_day(self, st: DayState, day: date, exit_t: datetime) -> DayState:
        """FVG 찾기 → 닿으면 진입 (하루 max_trades 종목까지) → 보유 종목 손절·익절 관리 → 정리 시각에 모두 정리."""
        last_scan = None
        while True:
            now = self.now()
            if now >= exit_t:
                for pos in self.open_positions(st):
                    self.close(st, pos, "time")
                break
            slots = len(st.positions) < self.cfg.max_trades
            if slots and now.replace(second=0, microsecond=0) != last_scan and now.second >= 2:
                last_scan = now.replace(second=0, microsecond=0)  # 1분봉이 끝날 때마다 FVG 찾기
                for sym, (h1, l1) in st.ranges.items():
                    if sym in st.signals or sym in st.dropped or sym in st.positions:
                        continue
                    bars = to_bars(self.c.klines(sym, ms(at(day, 9, 35)), limit=400), now)
                    sig = first_fvg(bars, h1, l1, self.cfg)
                    if sig:
                        st.signals[sym] = [sig[0], sig[1], sig[2].strftime("%H:%M")]
                        log.info("%s FVG %s (%s 봉) → %g 에 닿으면 진입, 손절 %g", sym, "롱" if sig[0] == 1 else "숏",
                                 sig[2].strftime("%H:%M"), sig[1], l1 if sig[0] == 1 else h1)
                        self.save(st)
            waiting = slots and any(s not in st.dropped and s not in st.positions for s in st.signals)
            if not waiting and not self.open_positions(st):
                if not slots:
                    break  # 오늘 몫을 다 쓰고 모두 정리됨
            if waiting or self.open_positions(st):
                px = self.c.prices()
                for pos in self.open_positions(st):
                    self.step(st, pos, px.get(pos["symbol"]), now)
                # 같은 순간 여러 종목이 닿으면 ADR(평균 변동폭) 높은 순 (백테스트: 순위 순보다 1종목 +500→+820%)
                order = sorted(st.signals.items(), key=lambda kv: -st.adr.get(kv[0], 0.0))
                for sym, (side, lvl, _) in order:
                    if len(st.positions) >= self.cfg.max_trades:
                        break
                    if sym in st.dropped or sym in st.positions or sym not in px:
                        continue
                    h1, l1 = st.ranges[sym]
                    stop, p = (l1 if side == 1 else h1), px[sym]
                    if side * (p - stop) <= 0:
                        st.dropped.append(sym)
                        log.info("%s 진입 전에 손절선(%g)을 넘어 제외 (현재 %g)", sym, stop, p)
                        self.save(st)
                    elif side * (p - lvl) <= 0:
                        self.enter(st, sym, side, lvl, stop, p)
            self.sleep(self.cfg.poll_seconds)
        st.phase = "done"
        done = [p for p in st.positions.values() if p.get("result")]
        st.result = {"trades": len(done), "pnl": round(sum(p["result"]["pnl"] for p in done), 4),
                     "symbols": [p["symbol"] for p in done]} if done else {"reason": "no_trade"}
        log.info("오늘 끝: %s (FVG %d개, 제외 %d개)", st.result, len(st.signals), len(st.dropped))
        self.save(st)
        return st

    def enter(self, st: DayState, sym: str, side: int, lvl: float, stop: float, price: float) -> bool:
        f = filters_of(self.meta.get(sym, {}))
        eq = self.equity()
        lvl_r = round_tick(lvl, f["tick"])
        qty = round_step(eq * self.cfg.leverage / lvl_r, f["step"])
        if qty <= 0 or qty * lvl_r < f["min_notional"]:
            log.info("%s 수량이 최소 주문 금액보다 작아 제외 (평가금액 %.2f)", sym, eq)
            st.dropped.append(sym)
            return False
        side_s = "BUY" if side == 1 else "SELL"
        if self.cfg.dry_run:
            filled, avg = qty, lvl_r
            log.info("[드라이런] %s %s %s개 @ %g (현재가 %g, 평가금액 %.2f)", sym, side_s, fmt(qty), lvl_r, price, eq)
        else:
            try:
                self.c.set_margin_type(sym, "CROSSED")  # 교차 마진 (격리면 증거금만큼만 버텨 5% 남짓 움직임에 청산될 수 있음)
                self.c.set_leverage(sym, max(self.cfg.exchange_leverage, round(self.cfg.leverage + 0.4999)))
                r = self.c.order(symbol=sym, side=side_s, type="LIMIT", timeInForce="IOC", quantity=fmt(qty),
                                 price=fmt(lvl_r), newOrderRespType="RESULT")
            except BinanceError as exc:
                if exc.code in (-1007, -1001) or exc.status == 408:  # 보냈는지 모름 → 실제 포지션으로 확인
                    self.sleep(2)
                    amt, ep = self.c.position_amt(sym)
                    log.warning("%s 진입 주문 응답 지연 (%s) → 포지션 확인 %s개", sym, exc, fmt(abs(amt)))
                    r = {"executedQty": str(abs(amt)), "avgPrice": str(ep or lvl_r), "status": "확인"}
                else:
                    log.error("%s 진입 주문 실패: %s (바이낸스 앱에서 TradFi 선물 이용 동의가 필요할 수 있어요)", sym, exc)
                    st.dropped.append(sym)
                    self.save(st)
                    return False
            filled, avg = float(r.get("executedQty", 0)), float(r.get("avgPrice", 0) or lvl_r)
            log.info("%s %s IOC %s개 @ %g → 체결 %s개 평균 %g (%s)", sym, side_s, fmt(qty), lvl_r, fmt(filled), avg,
                     r.get("status"))
            if filled <= 0:
                return False  # 그 사이 가격이 비켜 감 → 계속 지켜봄
        risk = abs(avg - stop)
        tp = avg + side * risk * self.cfg.target_r if self.cfg.target_r > 0 else 0.0
        pos = {"symbol": sym, "side": side, "qty": filled, "entry": avg, "stop": round_tick(stop, f["tick"]),
               "tp": round_tick(tp, f["tick"]) if tp else 0.0, "entry_time": self.now().isoformat(),
               "equity": eq, "stop_order": "", "tp_placed": False, "result": {}}
        st.positions[sym] = pos
        st.phase = "position"
        self.save(st)
        if not self.cfg.dry_run:
            try:
                pos["stop_order"] = self.c.stop_market(sym, "SELL" if side == 1 else "BUY", pos["stop"])
                log.info("%s 거래소 손절 주문 %g (%s)", sym, pos["stop"], pos["stop_order"])
            except BinanceError as exc:
                log.warning("%s 거래소 손절 주문 실패 (%s) → 봇이 가격을 보고 시장가로 손절합니다. 창을 닫지 마세요", sym, exc)
            real, _ = self.real_fill(sym, side_s, order_id=r.get("orderId"))
            if real > 0 and real != avg:  # 주문 응답의 평균가가 비어 있거나 주문가와 다르게 체결됨
                log.info("%s 실제 체결 평균가 %g (주문가 %g) 로 기록", sym, real, lvl_r)
                avg = real
                tp = avg + side * abs(avg - stop) * self.cfg.target_r if self.cfg.target_r > 0 else 0.0
                pos["entry"], pos["tp"] = avg, round_tick(tp, f["tick"]) if tp else 0.0
            self.save(st)
        log.info("%s 진입 %s %s개 @ %g, 손절 %g, 익절 %s (오늘 %d/%d번째)", sym, "롱" if side == 1 else "숏",
                 fmt(filled), avg, pos["stop"], fmt(pos["tp"]) if tp else "없음", len(st.positions), self.cfg.max_trades)
        return True

    def step(self, st: DayState, pos: dict, p: float | None, now: datetime) -> None:
        """보유 종목 하나 확인: 거래소에서 정리됐는지(5초마다), 손절선, 익절가."""
        sym, side, stop, tp = pos["symbol"], pos["side"], pos["stop"], pos["tp"]
        if not self.cfg.dry_run:
            last = pos.get("checked")
            if last is None or (now - datetime.fromisoformat(last)).total_seconds() >= 5:
                pos["checked"] = now.isoformat()
                amt, _ = self.c.position_amt(sym)
                if amt == 0:  # 거래소 손절·익절로 이미 정리됨 → 어느 쪽인지는 실제 체결가로 판단 (finish)
                    self.finish(st, pos, "", stop if p is None else p)
                    return
        if p is None:
            return
        if side * (p - stop) <= 0:
            self.close(st, pos, "stop")
            return
        if not tp:
            return
        if self.cfg.dry_run:
            if side * (p - tp) >= 0:
                self.finish(st, pos, "target", tp)
            return
        if not pos["tp_placed"]:
            if side * (p - tp) >= 0:
                self.close(st, pos, "target")  # 익절 주문을 걸기 전에 익절가를 넘어 버림
                return
            f = filters_of(self.meta.get(sym, {}))
            band = (f["pct_up"] if side == 1 else f["pct_down"]) or 0.05
            if abs(tp / p - 1) < band * 0.8:  # 지정가 허용 범위(현재가 ±2% 등) 안에 들어오면 익절 주문
                try:
                    self.c.order(symbol=sym, side="SELL" if side == 1 else "BUY", type="LIMIT", timeInForce="GTC",
                                 quantity=fmt(pos["qty"]), price=fmt(tp), reduceOnly="true")
                    pos["tp_placed"] = True
                    log.info("%s 익절 지정가 %g 주문", sym, tp)
                    self.save(st)
                except BinanceError as exc:
                    log.warning("%s 익절 주문 실패 (%s), 다시 시도", sym, exc)

    def close(self, st: DayState, pos: dict, reason: str) -> None:
        sym = pos["symbol"]
        price = self.c.prices().get(sym, 0.0)
        if self.cfg.dry_run:
            px = pos["stop"] if reason == "stop" else price
            log.info("[드라이런] %s %s 정리 @ %g", sym, {"stop": "손절", "time": "시간", "target": "익절"}[reason], px)
            self.finish(st, pos, reason, px)
            return
        self.c.cancel_all(sym)
        amt, _ = self.c.position_amt(sym)
        for _ in range(3):  # 응답 지연으로 실패해도 포지션이 남아 있으면 다시 정리 (reduceOnly 라 두 번 팔리지 않음)
            if amt == 0:
                break
            try:
                r = self.c.order(symbol=sym, side="SELL" if amt > 0 else "BUY", type="MARKET", quantity=fmt(abs(amt)),
                                 reduceOnly="true", newOrderRespType="RESULT")
                price = float(r.get("avgPrice", 0) or price)
                log.info("%s 시장가 정리 %s개 @ %g (%s)", sym, fmt(abs(amt)), price, reason)
            except BinanceError as exc:
                log.warning("%s 시장가 정리 주문 오류 (%s), 포지션 다시 확인", sym, exc)
                self.sleep(2)
            amt, _ = self.c.position_amt(sym)
        if amt != 0:
            log.error("%s 포지션이 아직 %s개 남아 있어요 — 바이낸스 앱에서 직접 정리해 주세요", sym, fmt(abs(amt)))
        self.finish(st, pos, reason, price)

    def real_fill(self, sym: str, side_s: str, since: datetime | None = None, order_id=None) -> tuple[float, float]:
        """바이낸스 체결 내역으로 본 실제 평균 체결가와 수량 (side_s = BUY / SELL). 못 구하면 (0, 0).
        주문 하나(order_id) 또는 since 이후 그 방향 체결 전부. 봇 시계가 거래소보다 늦을 수 있어 30초 여유."""
        fn = getattr(self.c, "fills", None)
        if self.cfg.dry_run or fn is None or (order_id is None and since is None):
            return 0.0, 0.0
        for _ in range(3):  # 체결 직후에는 내역이 조금 늦게 보일 수 있음
            try:
                rows = fn(sym, start_ms=ms(since) - 30_000 if since else None, order_id=order_id)
            except BinanceError as exc:
                log.warning("%s 체결 내역 조회 실패 (%s) → 주문가·현재가로 기록", sym, exc)
                return 0.0, 0.0
            rows = [x for x in rows if x.get("side") == side_s]
            qty = sum(float(x["qty"]) for x in rows)
            if qty > 0:
                return sum(float(x["price"]) * float(x["qty"]) for x in rows) / qty, qty
            self.sleep(0.5)
        return 0.0, 0.0

    def finish(self, st: DayState, pos: dict, reason: str, exit_px: float) -> None:
        """reason 이 비어 있으면(거래소 주문으로 정리됨) 청산가가 익절가·손절가 중 어디에 가까운지로 정한다."""
        if not self.cfg.dry_run:
            self.c.cancel_all(pos["symbol"])
            real, _ = self.real_fill(pos["symbol"], "SELL" if pos["side"] == 1 else "BUY",
                                     since=datetime.fromisoformat(pos["entry_time"]))
            if real > 0:
                exit_px = real
        if not reason:
            reason = "target" if pos["tp"] and abs(exit_px - pos["tp"]) < abs(exit_px - pos["stop"]) else "stop"
        ret = pos["side"] * (exit_px / pos["entry"] - 1) * 100 if exit_px else 0.0
        pnl = pos["side"] * (exit_px - pos["entry"]) * pos["qty"] if exit_px else 0.0
        pos["result"] = {"reason": reason, "exit": exit_px, "ret_pct": round(ret, 3), "pnl": round(pnl, 4),
                         "exit_time": self.now().isoformat()}
        self.save(st)
        log.info("%s 끝: %s @ %g, %+.2f%% (수수료 전 %+.2f USDT)", pos["symbol"],
                 {"target": "익절", "stop": "손절", "time": "시간 정리"}[reason], exit_px, ret, pnl)
        path = Path(self.cfg.state_dir) / "trades.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["day", "mode", "symbol", "side", "qty", "entry", "stop", "tp", "exit", "reason", "ret_pct", "pnl"])
            w.writerow([st.day, "dry" if self.cfg.dry_run else "live", pos["symbol"], pos["side"], pos["qty"],
                        pos["entry"], pos["stop"], pos["tp"], exit_px, reason, round(ret, 3), round(pnl, 4)])


# ---------------------------------------------------------------- 켜 둔 채로 거래일마다
LOOP_START = (9, 10)  # 미국 동부 (한국 시간 22:10, 서머타임 끝나면 23:10)


def day_end(day: date, exit_minutes: int, overnight: bool) -> datetime:
    """이 시각 전이면 오늘 run 을 시작(재시도)할 만함. 종가 매매를 켜면 15:57 (종가 매수 전)까지."""
    end = at(day, 9, 30) + timedelta(minutes=exit_minutes)
    return max(end, at(day, 15, 57)) if overnight and day not in US_EARLY_CLOSE else end


def next_start(now: datetime, last_run: str, exit_minutes: int = 350, overnight: bool = False) -> datetime:
    """overnight(종가 매매)를 켜면 13시 조기 마감일에도 시작한다 (전날 산 종목을 9:30 에 정리해야 해서)."""
    d = now
    for _ in range(10):
        day = d.date()
        if trading_day(day) and (overnight or day not in US_EARLY_CLOSE) and day.isoformat() != last_run:
            start = at(day, *LOOP_START)
            if day != now.date() or now < start:
                return start
            if now < day_end(day, exit_minutes, overnight):
                return now
        d = datetime.combine(day + timedelta(days=1), time(0, 0), ET)
    raise RuntimeError("다음 거래일을 못 찾음")


def loop(make_trader, cfg: BotConfig, now_fn=None, sleep_fn=_time.sleep, retries: int = 20) -> None:
    now_fn = now_fn or (lambda: datetime.now(ET))
    last_run = ""
    while True:
        start = next_start(now_fn(), last_run, cfg.exit_minutes, cfg.on_enabled)
        if start > now_fn():
            log.info("다음 실행 %s (미국 동부) 까지 대기 — 이 창을 닫지 마세요", start.strftime("%m-%d %H:%M"))
            while now_fn() < start:
                sleep_fn(min(60.0, max(1.0, (start - now_fn()).total_seconds())))
        day = now_fn().date()
        last_run = day.isoformat()
        for attempt in range(retries + 1):
            try:
                make_trader().run(day)
                break
            except Exception:  # noqa: BLE001
                log.exception("실행 중 오류 (%d/%d)", attempt + 1, retries)
                if attempt >= retries or now_fn() >= day_end(day, cfg.exit_minutes, cfg.on_enabled) + timedelta(minutes=5):
                    log.error("오늘은 더 시도하지 않음 — 바이낸스 앱에서 포지션·주문을 직접 확인하세요")
                    break
                sleep_fn(30)
