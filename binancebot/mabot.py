"""이평선 눌림 매매 실거래 봇 (바이낸스 USDT 선물, 한 종목씩).

- 이평선 매매 화면(maweb)에서 고른 설정을 그대로 따라 주문한다. 계산은 화면의 모의 매매(maweb.run)와 같은 함수.
- 거래소에 주문을 내는 것은 이 봇 하나뿐. 화면은 요청 파일(state_ma/req/*.json)만 남기고, 키를 갖지 않는다.
- 설정 .env.ma (예시 .env.ma.example): BINANCE_API_KEY/SECRET (서브 계정 권장), MA_DRY_RUN(기본 true = 주문 없이
  가상 계좌), MA_PIN (화면에서 수동 주문·자동 매매 시작 때 넣는 비밀번호), MA_MAX_USDT·MA_MAX_LEV (한 번 투입금·배율 상한).
- 진입: '선에 닿으면 바로' = 진입 지정가를 미리 걸어 둠(봉마다 선 값으로 옮김), '종가 확인' = 다음 봉 시가에 시장가.
- 보유 중: 손절(거래소 STOP_MARKET 전량)·익절(지정가 reduceOnly)을 봉마다 선 값으로 옮기고,
  종가 판단(손절선 마감 이탈·청산선 이탈·최대 보유 봉)은 봉이 끝날 때 봇이 시장가로 정리.
- 수동 주문(롱·숏 시장가)으로 잡은 포지션도 같은 손절·익절 규칙으로 관리. 전부 청산 버튼은 PIN 없이.
- 실행: python -m binancebot.mabot run --env .env.ma  /  확인: python -m binancebot.mabot check --env .env.ma
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .client import BinanceClient, BinanceError, filters_of, fmt, round_step, round_tick
from .maweb import Market, run

log = logging.getLogger("mabot")

STEP_SEC = 3          # 시세·주문 확인 간격
STATUS_FILE = "status.json"
STATE_FILE = "state.json"
REQ_DIR = "req"
REQ_MAX_AGE = 60      # 이보다 오래된 요청은 버림 (봇이 꺼져 있다 켜졌을 때 엉뚱한 주문 방지)
ALIVE_SEC = 20        # 화면: 상태 파일이 이 안에 갱신됐으면 '봇 켜짐'


def _f(v, d=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


@dataclass
class Settings:
    api_key: str = ""
    api_secret: str = ""
    dry_run: bool = True
    pin: str = ""
    max_usdt: float = 100.0
    max_lev: float = 5.0
    paper_equity: float = 1000.0
    safety_stop: float = 10.0   # 규칙 손절이 없을 때(선이 반대쪽 등) 거는 비상 손절 % (0 = 끔)
    # 거래소 배율 (0 = 화면 배율과 같게). 크게 두면 주문 크기(투입금 x 화면 배율)는 그대로이고
    # 거래소가 잡는 증거금만 줄어듦 (예: 50 USDT x 3배 = 150 USDT 주문, 거래소 5배면 증거금 30) — 대신 강제청산가가 가까워짐
    exchange_lev: float = 0.0
    # 한 거래 최대 손실 USDT (0 = 끔). 손절가를 '이만큼 잃는 가격'(수수료 추정 포함)보다 멀리 두지 않음 —
    # 거래소 손절도 그 가격에 걸고, 봇도 가격이 거기 닿으면 시장가 정리. 급변(갭)이면 조금 넘을 수 있음
    max_loss_usdt: float = 0.0
    state_dir: str = "state_ma"
    # 바이낸스 FVG 봇과 같은 계정을 쓸 때: FVG 봇이 켜져 있으면 미국 정규장 시작 N분 전에 자동 매매를 멈추고 포지션 정리
    # (2026-11-27 사용자 결정, 0 = 끔, FVG 봇 켜짐 = 그 상태 폴더의 bot.lock 이 잡혀 있음)
    fvg_stop_min: int = 10
    fvg_state_dir: str = "state_binance"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(api_key=e("BINANCE_API_KEY", ""), api_secret=e("BINANCE_API_SECRET", ""),
                   dry_run=(e("MA_DRY_RUN", "true").strip().lower() not in ("false", "0", "no")),
                   pin=e("MA_PIN", ""), max_usdt=_f(e("MA_MAX_USDT"), 100.0), max_lev=_f(e("MA_MAX_LEV"), 5.0),
                   paper_equity=_f(e("MA_PAPER_EQUITY"), 1000.0),
                   safety_stop=_f(e("MA_SAFETY_STOP"), 10.0), state_dir=e("MA_STATE_DIR", "state_ma"),
                   exchange_lev=_f(e("MA_EXCHANGE_LEV"), 0.0), max_loss_usdt=_f(e("MA_MAX_LOSS_USDT"), 0.0),
                   fvg_stop_min=int(_f(e("MA_FVG_STOP_MIN"), 10)), fvg_state_dir=e("MA_FVG_STATE_DIR", "state_binance"))


# ------------------------------------------------------------ 거래소 (실제 / 가상)
class LiveExchange:
    """실제 바이낸스 주문. 종목 하나."""
    paper = False

    def __init__(self, client: BinanceClient, symbol: str):
        self.c, self.sym = client, symbol
        meta = next(s for s in client.exchange_info()["symbols"] if s["symbol"] == symbol)
        self.f = filters_of(meta)

    def price(self) -> float:
        return float(self.c._req("GET", "/fapi/v1/ticker/price", {"symbol": self.sym})["price"])

    def position(self) -> tuple[float, float]:
        return self.c.position_amt(self.sym)

    def equity(self) -> float:
        return self.c.equity()

    def setup(self, lev: int) -> None:
        try:
            self.c.set_margin_type(self.sym, "ISOLATED")
        except BinanceError as exc:  # 주문·포지션이 있으면 바꿀 수 없음 (-4067·-4048) → 그대로
            log.info("격리 마진 설정 건너뜀: %s", exc)
        self.c.set_leverage(self.sym, lev)

    def market(self, side: str, qty: float, reduce: bool = False) -> float:
        r = self.c.order(symbol=self.sym, side=side, type="MARKET", quantity=fmt(qty),
                         reduceOnly="true" if reduce else None, newOrderRespType="RESULT")
        return _f(r.get("avgPrice"))

    def limit(self, side: str, qty: float, price: float, cid: str, reduce: bool = False) -> None:
        self.c.order(symbol=self.sym, side=side, type="LIMIT", timeInForce="GTC", quantity=fmt(qty),
                     price=fmt(price), newClientOrderId=cid, reduceOnly="true" if reduce else None)

    def cancel(self, cid: str) -> None:
        try:
            self.c.cancel_order(self.sym, cid)
        except BinanceError as exc:  # 이미 체결·취소
            log.info("주문 취소 응답 %s: %s", cid, exc)

    def cancel_all(self) -> None:
        self.c.cancel_all(self.sym)

    def stop(self, side: str, price: float) -> None:
        self.c.stop_market(self.sym, side, price)

    def exit_price(self, since_ms: int, side: str) -> float:
        """since_ms 뒤 side 쪽 체결 평균가 (정리 가격)."""
        rows = [r for r in self.c.fills(self.sym, start_ms=since_ms) if r.get("side") == side]
        q = sum(_f(r["qty"]) for r in rows)
        return sum(_f(r["qty"]) * _f(r["price"]) for r in rows) / q if q else 0.0


class PaperExchange:
    """주문 없는 가상 계좌 (드라이런). 시세는 진짜, 체결은 봇이 확인할 때의 가격으로 흉내."""
    paper = True
    FEE_LIMIT, FEE_MARKET = 0.0002, 0.0005

    def __init__(self, symbol: str, price_fn, filters: dict, path: Path, equity: float = 1000.0):
        self.sym, self.price_fn, self.f, self.path = symbol, price_fn, filters, path
        self.s = {"qty": 0.0, "entry": 0.0, "cash": equity, "orders": [], "stop": None, "last_exit": 0.0}
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
                if old.get("sym") == symbol:
                    self.s.update(old)
                else:
                    self.s["cash"] = old.get("cash", equity)
            except ValueError:
                pass
        self.last = 0.0

    def _save(self) -> None:
        self.path.write_text(json.dumps({**self.s, "sym": self.sym}), encoding="utf-8")

    def _fill(self, side: str, qty: float, px: float, fee: float) -> None:
        d = 1 if side == "BUY" else -1
        q0 = self.s["qty"]
        self.s["cash"] -= qty * px * fee
        if q0 == 0 or (q0 > 0) == (d > 0):     # 새로 열거나 늘림
            tot = abs(q0) + qty
            self.s["entry"] = (abs(q0) * self.s["entry"] + qty * px) / tot
            self.s["qty"] = round(q0 + d * qty, 10)
        else:                                   # 줄이거나 정리
            close = min(qty, abs(q0))
            self.s["cash"] += close * (px - self.s["entry"]) * (1 if q0 > 0 else -1)
            self.s["qty"] = round(q0 + d * close, 10)
            if abs(self.s["qty"]) < 1e-12:
                self.s["qty"], self.s["entry"] = 0.0, 0.0
                self.s["last_exit"] = px
        self._save()

    def price(self) -> float:
        p = self.price_fn()
        self.last = p
        q = self.s["qty"]
        st = self.s.get("stop")
        if st and q and ((st["side"] == "SELL" and p <= st["price"]) or (st["side"] == "BUY" and p >= st["price"])):
            self._fill(st["side"], abs(q), p, self.FEE_MARKET)
            self.s["stop"] = None
            self.s["orders"] = [o for o in self.s["orders"] if not o["reduce"]]
        for o in list(self.s["orders"]):
            hit = p <= o["price"] if o["side"] == "BUY" else p >= o["price"]
            if not hit:
                continue
            self.s["orders"].remove(o)
            q = self.s["qty"]
            if o["reduce"]:
                if not q or (q > 0) == (o["side"] == "BUY"):
                    continue
                qty = min(o["qty"], abs(q))
            else:
                qty = o["qty"]
            px = min(p, o["price"]) if o["side"] == "BUY" else max(p, o["price"])
            self._fill(o["side"], qty, px, self.FEE_LIMIT)
            if not self.s["qty"]:
                self.s["stop"] = None
                self.s["orders"] = [x for x in self.s["orders"] if not x["reduce"]]
        self._save()
        return p

    def position(self) -> tuple[float, float]:
        return self.s["qty"], self.s["entry"]

    def equity(self) -> float:
        q = self.s["qty"]
        return self.s["cash"] + (q * (self.last - self.s["entry"]) if q else 0.0)

    def setup(self, lev: int) -> None:
        pass

    def market(self, side: str, qty: float, reduce: bool = False) -> float:
        p = self.last or self.price_fn()
        px = p * (1.0005 if side == "BUY" else 0.9995)
        if reduce:
            qty = min(qty, abs(self.s["qty"]))
        self._fill(side, qty, px, self.FEE_MARKET)
        return px

    def limit(self, side: str, qty: float, price: float, cid: str, reduce: bool = False) -> None:
        self.s["orders"].append({"cid": cid, "side": side, "qty": qty, "price": price, "reduce": reduce})
        self._save()

    def cancel(self, cid: str) -> None:
        self.s["orders"] = [o for o in self.s["orders"] if o["cid"] != cid]
        self._save()

    def cancel_all(self) -> None:
        self.s["orders"], self.s["stop"] = [], None
        self._save()

    def stop(self, side: str, price: float) -> None:
        self.s["stop"] = {"side": side, "price": price}
        self._save()

    def exit_price(self, since_ms: int, side: str) -> float:
        return self.s.get("last_exit", 0.0)


# ------------------------------------------------------------ 봇
class MaBot:
    def __init__(self, settings: Settings, market: Market | None = None, make_exchange=None, now=time.time):
        self.cfg = settings
        self.market = market or Market()
        self.now = now
        self.dir = Path(settings.state_dir)
        (self.dir / REQ_DIR).mkdir(parents=True, exist_ok=True)
        self.make_exchange = make_exchange or self._default_exchange
        self.ex = None
        self.st = {"cfg": None, "enabled": False, "pos": None, "entry_orders": {}, "exit": {},
                   "block_bar": 0, "confirm_bar": 0, "lev_set": "", "msgs": [], "trades": []}
        p = self.dir / STATE_FILE
        if p.exists():
            try:
                self.st.update(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                log.warning("상태 파일을 못 읽어 새로 시작")
        self.last_price = 0.0
        self.last_error = ""

    # ---------------------------------------------------------- 도우미
    def _default_exchange(self, symbol: str):
        if self.cfg.dry_run:
            pub = BinanceClient()
            meta = next(s for s in pub.exchange_info()["symbols"] if s["symbol"] == symbol)
            price_fn = lambda: float(pub._req("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])  # noqa: E731
            return PaperExchange(symbol, price_fn, filters_of(meta), self.dir / "paper.json", self.cfg.paper_equity)
        c = BinanceClient(self.cfg.api_key, self.cfg.api_secret)
        c.sync_time()
        if c.hedge_mode():
            raise RuntimeError("바이낸스 계정이 양방향(헤지) 모드예요. 선물 설정에서 '단방향(One-way)'으로 바꿔 주세요.")
        return LiveExchange(c, symbol)

    def msg(self, text: str) -> None:
        log.info(text)
        self.st["msgs"] = (self.st["msgs"] + [{"t": int(self.now()), "text": text}])[-30:]

    def save(self) -> None:
        (self.dir / STATE_FILE).write_text(json.dumps(self.st, ensure_ascii=False), encoding="utf-8")

    def exchange(self, symbol: str):
        if self.ex is None or self.ex.sym != symbol:
            if self.ex is not None and self.st.get("entry_orders"):
                # 종목을 바꾸면 예전 종목에 걸어 둔 진입 지정가는 그 종목에서 취소 (안 그러면 기록만 지워지고 주문이 남음)
                for o in self.st["entry_orders"].values():
                    self.ex.cancel(o["cid"])
                self.st["entry_orders"] = {}
            self.ex = self.make_exchange(symbol)
            self.st["lev_set"] = ""
        return self.ex

    def qty_for(self, usdt: float, lev: float, price: float) -> float:
        f = self.ex.f
        qty = round(round_step(usdt * lev / price, f["step"]), 8)
        if qty <= 0 or qty * price < max(f.get("min_notional", 0), 0):
            raise ValueError(f"투입금 x 배율({usdt * lev:.2f} USDT)이 이 종목의 최소 주문 금액"
                             f"({f.get('min_notional', 0):g} USDT)·수량 단위보다 작아요")
        return qty

    def ensure_lev(self, lev: float) -> None:
        lev = max(lev, min(self.cfg.exchange_lev, 20))   # 거래소 배율은 주문 배율 이상 (증거금만 줄어듦)
        key = f"{self.ex.sym}:{int(lev)}"
        if self.st.get("lev_set") != key:
            self.ex.setup(int(lev))
            self.st["lev_set"] = key

    def check_limits(self, usdt: float, lev: float) -> None:
        if usdt <= 0 or lev < 1:
            raise ValueError("투입금은 0 보다, 배율은 1 이상으로 넣어 주세요")
        if usdt > self.cfg.max_usdt:
            raise ValueError(f"투입금 {usdt:g} USDT 가 상한 {self.cfg.max_usdt:g} 보다 커요 (.env.ma 의 MA_MAX_USDT)")
        if lev > self.cfg.max_lev:
            raise ValueError(f"배율 {lev:g} 이 상한 {self.cfg.max_lev:g} 보다 커요 (.env.ma 의 MA_MAX_LEV)")
        if int(lev) != lev:
            raise ValueError("배율은 정수로 넣어 주세요")

    def check_pin(self, pin: str) -> None:
        if self.cfg.dry_run and not self.cfg.pin:
            return
        if not self.cfg.pin:
            raise ValueError("실제 주문은 PIN 이 있어야 해요. .env.ma 의 MA_PIN 에 숫자를 넣고 봇을 다시 켜 주세요")
        if str(pin) != self.cfg.pin:
            raise ValueError("PIN 이 맞지 않아요")

    # ---------------------------------------------------------- 화면 요청
    def process_requests(self) -> None:
        for p in sorted((self.dir / REQ_DIR).glob("*.json")):
            try:
                req = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                req = None
            try:
                p.unlink()
            except OSError:
                pass
            if not req or self.now() - _f(req.get("time")) > REQ_MAX_AGE:
                continue
            try:
                self.handle(req)
            except Exception as exc:  # noqa: BLE001 요청 하나 실패해도 봇은 계속
                self.msg(f"요청 실패 ({req.get('cmd')}): {exc}")
        self.save()

    def _new_cfg(self, req: dict) -> dict:
        q = dict(req.get("q") or {})
        if q.get("strat") != "pullback":
            raise ValueError("실거래는 '눌림 매매'만 돼요")
        usdt, lev = _f(req.get("usdt")), _f(req.get("lev"), 1)
        self.check_limits(usdt, lev)
        sym = str(q.get("symbol", "")).upper()
        if not sym:
            raise ValueError("종목이 비어 있어요")
        old = self.st.get("cfg")
        if self.st.get("pos") and old and old["symbol"] != sym:
            raise ValueError(f"{old['symbol']} 포지션이 남아 있어요. 먼저 정리한 뒤 종목을 바꿔 주세요")
        q["bars"] = str(min(int(_f(q.get("bars"), 500)), 1000))
        return {"symbol": sym, "interval": q.get("interval", "5m"), "q": q, "usdt": usdt, "lev": lev}

    def handle(self, req: dict) -> None:
        cmd = req.get("cmd")
        if cmd == "start":
            self.check_pin(req.get("pin", ""))
            self.st["cfg"] = self._new_cfg(req)
            self.st["enabled"] = True
            # 1회 매매: 이 뒤에 새로 연 거래 하나가 정리되면 자동으로 멈춤
            self.st["once"] = bool(req.get("once"))
            self.st["once_since"] = int(self.now() * 1000)
            # 켠 시각부터 진입을 찾음 (그 전에 모의 매매가 이미 들어간 자리는 따라가지 않음)
            self.st["entry_from"] = int(self.now())
            c = self.st["cfg"]
            self.msg(f"{'1회 매매' if self.st['once'] else '자동 매매'} 시작: {c['symbol']} {c['interval']} · "
                     f"투입 {c['usdt']:g} USDT x {c['lev']:g}배")
        elif cmd == "stop":
            self.st["enabled"] = False
            self.st["once"] = False
            self._cancel_entries()
            self.msg("자동 매매 멈춤 (새 진입 안 함, 보유 중이면 손절·익절은 계속 관리)")
        elif cmd in ("long", "short"):
            self.check_pin(req.get("pin", ""))
            cfg = self._new_cfg(req)
            ex = self.exchange(cfg["symbol"])
            qty0, _ = ex.position()
            if qty0:
                raise ValueError("이미 포지션이 있어요. 정리한 뒤 다시 눌러 주세요")
            self.st["cfg"] = cfg   # 이 포지션의 손절·익절은 지금 화면 설정으로 관리
            self._cancel_entries()
            self.ensure_lev(cfg["lev"])
            price = ex.price()
            qty = self.qty_for(cfg["usdt"], cfg["lev"], price)
            px = ex.market("BUY" if cmd == "long" else "SELL", qty)
            self.msg(f"수동 {'롱' if cmd == 'long' else '숏'} 시장가 {qty:g}개 @ {px or price:.6g}")
        elif cmd == "close":
            c = self.st.get("cfg")
            sym = str(req.get("symbol") or (c and c["symbol"]) or "").upper()
            if not sym:
                raise ValueError("정리할 종목을 모르겠어요")
            self.close_now(sym, "manual")
        else:
            raise ValueError(f"모르는 요청: {cmd}")

    # ---------------------------------------------------------- 주문 관리
    def _cancel_entries(self) -> None:
        if self.ex is not None:
            for d, o in list(self.st["entry_orders"].items()):
                self.ex.cancel(o["cid"])
        self.st["entry_orders"] = {}

    def close_now(self, symbol: str, reason: str) -> None:
        ex = self.exchange(symbol)
        ex.cancel_all()
        self.st["entry_orders"], self.st["exit"] = {}, {}
        qty, _ = ex.position()
        if qty:
            ex.market("SELL" if qty > 0 else "BUY", abs(qty), reduce=True)
            if self.st.get("pos"):
                self.st["pos"]["reason"] = reason
            self.msg(f"정리 ({reason}): {abs(qty):g}개 시장가")
        else:
            self.msg("정리할 포지션이 없어요 (남은 주문만 취소)")

    def fvg_running(self) -> bool:
        """바이낸스 FVG 봇(loop/run)이 켜져 있으면 그 상태 폴더의 bot.lock 이 잡혀 있음."""
        d = Path(self.cfg.fvg_state_dir)
        if not (d / "bot.lock").exists():
            return False
        from kiwoombot.__main__ import acquire_lock
        f = acquire_lock(str(d))
        if f is None:
            return True
        f.close()   # 잡혔으면 아무도 안 쓰는 것 → 바로 놓음
        return False

    def fvg_guard(self) -> None:
        """미국 거래일 정규장 시작 N분 전(동부 9:30 - N분 ~ 16:00)에 FVG 봇이 켜져 있으면 하루 한 번
        자동 매매를 멈추고 포지션·주문을 정리 (같은 계정에서 포지션이 섞이지 않게)."""
        if self.cfg.fvg_stop_min <= 0:
            return
        from datetime import datetime, timedelta
        from .backtest import ET, US_EARLY_CLOSE
        from .bot import trading_day
        t = datetime.fromtimestamp(self.now(), ET)
        d = t.date()
        if not trading_day(d) or d in US_EARLY_CLOSE or self.st.get("fvg_stop_day") == d.isoformat():
            return
        start = datetime.combine(d, datetime.min.time(), ET).replace(hour=9, minute=30) - timedelta(minutes=self.cfg.fvg_stop_min)
        if not (start <= t < start.replace(hour=16, minute=0)):
            return
        c = self.st.get("cfg")
        busy = self.st.get("enabled") or self.st.get("pos") or self.st.get("entry_orders")
        if not busy or not self.fvg_running():
            return
        self.st["fvg_stop_day"] = d.isoformat()
        self.st["enabled"] = False
        self.st["once"] = False
        self.msg(f"FVG 봇이 켜져 있어 정규장 {self.cfg.fvg_stop_min}분 전에 이평선 매매를 끝냄 (자동 매매 멈춤·포지션 정리)")
        if c:
            self.close_now(c["symbol"], "fvg")
        self.save()

    def step(self) -> None:
        self.process_requests()
        self.fvg_guard()
        c = self.st.get("cfg")
        if not c:
            self.write_status()
            return
        ex = self.exchange(c["symbol"])
        price = ex.price()
        self.last_price = price
        qty, entry = ex.position()
        res = run(self.market, {**base5(c["q"], c["interval"]), "symbol": c["symbol"], "_live": "1",
                                "_entry_from": str(self.st.get("entry_from", 0))})
        bars = res["bars"]
        if qty == 0:
            self.flat(res, price)
        else:
            self.holding(res, price, qty, entry)
        self.save()
        self.write_status(res)

    def flat(self, res: dict, price: float) -> None:
        bars, sim, c = res["bars"], res["sim"], self.st["cfg"]
        if self.st.get("pos"):
            self.record_close(price)
            self.ex.cancel_all()
            self.st["entry_orders"], self.st["exit"] = {}, {}
            self.st["block_bar"] = bars[-1]["t"]   # 정리한 봉에서는 다시 안 들어감 (모의 매매와 같게)
        if not self.st.get("enabled") or bars[-1]["t"] == self.st.get("block_bar"):
            self._cancel_entries()
            return
        q = c["q"]
        fo = (sim or {}).get("far_open")
        if fo and fo["t"] == bars[-1]["t"] and self.st.get("far_bar") != bars[-1]["t"]:
            # 반대 진입: 기다리던 진입선에서 far_pct% 넘게 벗어난 5분봉이 끝남 → 반대 방향 시장가
            self.st["far_bar"] = bars[-1]["t"]
            self._cancel_entries()
            self.ensure_lev(c["lev"])
            qty = self.qty_for(c["usdt"], c["lev"], price)
            d = 1 if fo["dir"] == "long" else -1
            self.st["far_pos"] = {"dir": d, "t_ms": int(self.now() * 1000)}
            px = self.ex.market("BUY" if d > 0 else "SELL", qty)
            self.msg(f"{'롱' if d > 0 else '숏'} 반대 진입 (진입선에서 {_f(q.get('far_pct')):g}% 벗어남) 시장가 {qty:g}개 @ {px or price:.6g}")
            return
        if q.get("entry") == "confirm":
            o = sim.get("open") if sim else None
            if o and o["entry_time"] == bars[-1]["t"] and self.st.get("confirm_bar") != bars[-1]["t"]:
                self.st["confirm_bar"] = bars[-1]["t"]
                self.ensure_lev(c["lev"])
                qty = self.qty_for(c["usdt"], c["lev"], price)
                px = self.ex.market("BUY" if o["dir"] == "long" else "SELL", qty)
                self.msg(f"{'롱' if o['dir'] == 'long' else '숏'} 진입 (종가 확인) 시장가 {qty:g}개 @ {px or price:.6g}")
            return
        tick = self.ex.f.get("tick", 0)
        waits = (sim or {}).get("wait_orders", [])
        want = {str(w["dir"]): round_tick(w["price"], tick) for w in waits}
        want_line = {str(w["dir"]): w.get("line", 0) for w in waits}
        have = self.st["entry_orders"]
        for d, o in list(have.items()):
            if d not in want or abs(o["price"] - want[d]) >= max(tick, 1e-12) * 0.5:
                self.ex.cancel(o["cid"])
                del have[d]
        for d, px in want.items():
            if d in have:
                continue
            try:
                self.ensure_lev(c["lev"])
                qty = self.qty_for(c["usdt"], c["lev"], px)
                cid = f"ma_{'L' if d == '1' else 'S'}_{uuid.uuid4().hex[:12]}"
                self.ex.limit("BUY" if d == "1" else "SELL", qty, px, cid)
                have[d] = {"cid": cid, "price": px, "qty": qty, "line": want_line.get(d, 0)}
                self.msg(f"{'롱' if d == '1' else '숏'} 진입 지정가 {qty:g}개 @ {px:.6g}")
            except Exception as exc:  # noqa: BLE001
                key = f"{d}:{bars[-1]['t']}"
                if self.last_error != key:
                    self.msg(f"진입 지정가 실패: {exc}")
                    self.last_error = key

    def holding(self, res: dict, price: float, qty: float, entry: float) -> None:
        bars, live, c = res["bars"], res.get("live") or {}, self.st["cfg"]
        q = c["q"]
        n = len(bars)
        d = 1 if qty > 0 else -1
        side_close = "SELL" if d > 0 else "BUY"
        b5 = bars   # 봇은 늘 5분봉으로 계산 (base5)
        pos = self.st.get("pos")
        if not pos or pos.get("dir") != d:
            li = self._entry_line(res, d, entry)
            self._cancel_entries()
            self.ex.cancel_all()
            pos = self.st["pos"] = {"dir": d, "entry": entry, "qty": abs(qty), "t_ms": int(self.now() * 1000),
                                    "bar": bars[-1]["t"], "last_closed": bars[n - 2]["t"], "armed": False,
                                    "line": li, "opp": self._opp_line(res, li, d, entry),
                                    "bar5": b5[-1]["t"], "last5": b5[-2]["t"]}
            fp = self.st.pop("far_pos", None)
            if fp and fp["dir"] == d and self.now() * 1000 - fp["t_ms"] < 600_000:
                pos["far"] = True   # 반대 진입: 손절·익절 % 만
            self.st["exit"] = {}
            self.msg(f"{'롱' if d > 0 else '숏'} 보유 시작 {abs(qty):g}개 @ {entry:.6g}")
        pos["entry"], pos["qty"] = entry, abs(qty)
        pos.setdefault("bar5", b5[-1]["t"])
        pos.setdefault("last5", b5[-2]["t"])
        sl, tpp = _f(q.get("sl")), _f(q.get("tp"))
        stop_mode, tp_mode = q.get("stop_mode", "pct"), q.get("tp_mode", "pct")
        prev_c = b5[-2]["c"]   # 직전에 끝난 5분봉 종가
        stop_px = tp_px = None
        if live.get("stop_same"):   # 손절선 = 진입한 그 진입선
            li = min(pos.get("line", 0), len(live["pb_lines"]) - 1)
            live = {**live, "stop": live["pb_lines"][li], "stop_ends": live["pb_ends"][li]}
        far = bool(pos.get("far"))
        if far:
            stop_px = entry * (1 - d * _f(q.get("far_sl") or 1) / 100)
        elif stop_mode == "pct" and sl > 0:
            stop_px = entry * (1 - d * sl / 100)
        elif stop_mode == "touch" and live.get("stop") and live["stop"][n - 2] is not None:
            v = live["stop"][n - 2] * (1 - d * sl / 100)
            first = b5[-1]["t"] == pos["bar5"]
            if (first and d * (entry - v) > 0) or (not first and d * (prev_c - v) > 0):
                stop_px = v
        safety = False
        if stop_px is None and self.cfg.safety_stop > 0:   # 규칙 손절이 안 걸리는 동안 비상 손절
            stop_px, safety = entry * (1 - d * self.cfg.safety_stop / 100), True
        cap = self.cfg.max_loss_usdt
        if cap > 0 and abs(qty) > 0:   # 최대 손실 제한: 잃는 돈 = 수량 x 가격 차 + 왕복 수수료(시장가 0.05% x 2 추정)
            room = (cap - abs(qty) * entry * 0.001) / abs(qty)
            cap_px = entry - d * max(room, entry * 0.001)
            if stop_px is None or d * (cap_px - stop_px) > 0:
                stop_px, safety = cap_px, "maxloss"
        if far:
            tp_px = entry * (1 + d * _f(q.get("far_tp") or 3) / 100)
        elif b5[-1]["t"] > pos["bar5"]:   # 익절은 산 다음 5분봉부터
            if tp_mode == "pct" and tpp > 0:
                tp_px = entry * (1 + d * tpp / 100)
            elif tp_mode == "touch" and live.get("tp") and live["tp"][n - 2] is not None:
                v = live["tp"][n - 2]
                if d * (v - entry) > 0 and d * (v - prev_c) > 0:
                    tp_px = v
            oj, pls = pos.get("opp"), live.get("pb_lines") or []
            if oj is not None and oj < len(pls) and pls[oj] and pls[oj][n - 2] is not None:
                v = pls[oj][n - 2]   # 반대 방향 진입선에서 익절 (원래 익절이 더 가까우면 그것)
                if d * (v - entry) > 0 and d * (v - prev_c) > 0 and (tp_px is None or d * (v - tp_px) < 0):
                    tp_px = v
        k = n - 2
        # 반대 진입(far)은 손절·익절 % 만 (선 손절·청산선·최대 보유 없음)
        # 손절선 종가 이탈: 5분봉이 끝날 때마다 그 종가로 판단 (산 5분봉부터)
        if not far and b5[-2]["t"] > pos["last5"] and b5[-2]["t"] >= pos["bar5"]:
            pos["last5"] = b5[-2]["t"]
            sv, se = live.get("stop"), live.get("stop_ends")
            if stop_mode == "line" and sv and sv[k] is not None and (se is None or se[k]) \
                    and d * (b5[-2]["c"] - sv[k] * (1 - d * sl / 100)) < 0:
                self.close_now(self.ex.sym, "line")
                return
        # 차트 봉이 끝났을 때 종가로 판단하는 것들 (청산선·최대 보유 봉, 산 봉부터)
        if not far and bars[k]["t"] > pos["last_closed"] and bars[k]["t"] >= pos["bar"]:
            pos["last_closed"] = bars[k]["t"]
            cl = bars[k]["c"]
            reason = None
            xv, xe = live.get("exit"), live.get("exit_ends")
            if not reason and xv and xv[k] is not None and (xe is None or xe[k]):
                lim = xv[k] * (1 - d * _f(q.get("exit_pct")) / 100)
                if d * (cl - lim) >= 0:
                    pos["armed"] = True
                elif pos.get("armed"):
                    reason = "exit"
            mh = int(_f(q.get("max_hold")))
            if not reason and mh > 0 and (bars[k]["t"] - pos["bar"]) // live.get("chart_sec", 1) + 1 >= mh:
                reason = "time"
            if reason:
                self.close_now(self.ex.sym, reason)
                return
        # 손절가를 이미 넘었으면 바로 정리 (거래소 손절 주문이 못 걸릴 때 대비)
        if stop_px is not None and d * (price - stop_px) <= 0:
            self.close_now(self.ex.sym, safety if isinstance(safety, str) else ("safety" if safety else "stop"))
            return
        tick = self.ex.f.get("tick", 0)
        want = {"safety": safety, "stop": round_tick(stop_px, tick) if stop_px else None,
                "tp": round_tick(tp_px, tick) if tp_px else None, "qty": abs(qty)}
        if want != self.st.get("exit"):
            self.ex.cancel_all()
            if want["stop"]:
                try:
                    self.ex.stop(side_close, want["stop"])
                except Exception as exc:  # noqa: BLE001 봇이 가격 보고 직접 손절 (위)
                    self.msg(f"거래소 손절 주문 실패 (봇이 가격 보고 손절): {exc}")
            if want["tp"]:
                try:
                    self.ex.limit(side_close, abs(qty), want["tp"], f"ma_T_{uuid.uuid4().hex[:12]}", reduce=True)
                except Exception as exc:  # noqa: BLE001
                    self.msg(f"익절 지정가 실패: {exc}")
            if (want["stop"], want["tp"]) != (self.st.get("exit", {}).get("stop"), self.st.get("exit", {}).get("tp")):
                tag = f" (최대 손실 {cap:g} USDT)" if safety == "maxloss" else (" (비상)" if safety else "")
                self.msg(f"손절 {want['stop'] or '-'}{tag} · 익절 {want['tp'] or '-'}")
            self.st["exit"] = want

    def _opp_line(self, res: dict, li: int, d: int, entry: float):
        """진입 순간 이익 쪽에 있는 가장 가까운 다른 진입선이 opp_tp% 안이면 그 선 번호 (거기서 익절)."""
        lim = _f(self.st["cfg"]["q"].get("opp_tp"))
        pls = (res.get("live") or {}).get("pb_lines") or []
        k = len(res["bars"]) - 2
        if lim <= 0 or k < 0:
            return None
        c = [(d * (pl[k] / entry - 1) * 100, lj) for lj, pl in enumerate(pls)
             if lj != li and pl and pl[k] is not None and d * (pl[k] - entry) > 0]
        return min(c)[1] if c and min(c)[0] <= lim else None

    def _entry_line(self, res: dict, d: int, entry: float) -> int:
        """새로 생긴 포지션이 어느 진입선으로 들어왔는지: 걸어 둔 지정가 → 모의 매매 → 진입가에 가장 가까운 선."""
        o = self.st.get("entry_orders", {}).get(str(d))
        if o and "line" in o:
            return int(o["line"])
        sim_open = (res.get("sim") or {}).get("open")
        if sim_open and sim_open.get("dir") == ("long" if d > 0 else "short") and "line" in sim_open:
            return int(sim_open["line"])
        pls = (res.get("live") or {}).get("pb_lines") or []
        k = len(res["bars"]) - 2
        near = [(abs(pl[k] - entry), li) for li, pl in enumerate(pls) if pl and pl[k] is not None]
        return min(near)[1] if near else 0

    def record_close(self, price: float) -> None:
        pos = self.st["pos"]
        d = pos["dir"]
        ex_px = 0.0
        try:
            ex_px = self.ex.exit_price(pos["t_ms"], "SELL" if d > 0 else "BUY")
        except Exception as exc:  # noqa: BLE001
            log.info("정리 가격 조회 실패: %s", exc)
        px = ex_px or price
        reason = pos.get("reason")
        far = pos.get("far")
        if not reason:   # 거래소 손절·익절 주문으로 정리됨 → 가까운 쪽
            ex = self.st.get("exit", {})
            cands = [(abs(px - v), r) for r, v in (("stop", ex.get("stop")), ("take", ex.get("tp"))) if v]
            reason = min(cands)[1] if cands else "?"
            if reason == "stop" and ex.get("safety"):
                reason = ex["safety"] if isinstance(ex["safety"], str) else "safety"
        if far and reason in ("stop", "take"):
            reason = "far_" + reason   # 반대 진입 손절·익절
        c = self.st["cfg"]
        ret = d * (px / pos["entry"] - 1) * 100
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "symbol": self.ex.sym, "dir": "long" if d > 0 else "short",
               "qty": pos["qty"], "entry": pos["entry"], "exit": px, "reason": reason, "ret_pct": round(ret, 3),
               "pnl_usdt": round(d * (px - pos["entry"]) * pos["qty"], 4), "lev": c["lev"] if c else "",
               "dry": self.cfg.dry_run}
        self.st["trades"] = (self.st["trades"] + [row])[-50:]
        path = self.dir / "trades.csv"
        new = not path.exists()
        with path.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
        self.msg(f"정리됨 ({reason}) @ {px:.6g} · {ret:+.2f}% (배율 전) · {row['pnl_usdt']:+.4f} USDT")
        if self.st.get("once") and pos.get("t_ms", 0) >= self.st.get("once_since", 0):
            self.st["enabled"], self.st["once"] = False, False
            self.msg("1회 매매 끝 — 자동 매매 멈춤 (다시 하려면 버튼을 다시 눌러 주세요)")
        self.st["pos"] = None

    def write_status(self, res: dict | None = None) -> None:
        c = self.st.get("cfg")
        pos = self.st.get("pos")
        out = {"time": int(self.now()), "dry": self.cfg.dry_run, "enabled": self.st.get("enabled"), "once": bool(self.st.get("once")),
               "max_usdt": self.cfg.max_usdt, "max_lev": self.cfg.max_lev, "safety_stop": self.cfg.safety_stop, "max_loss_usdt": self.cfg.max_loss_usdt, "pin_set": bool(self.cfg.pin),
               "cfg": {k: c[k] for k in ("symbol", "interval", "usdt", "lev")} if c else None,
               "price": self.last_price, "msgs": self.st["msgs"][-15:], "trades": self.st["trades"][-20:],
               "entry_orders": list(self.st["entry_orders"].values()), "exit": self.st.get("exit") or {}}
        if c:
            q = c["q"]
            out["rule"] = {k: q.get(k) for k in ("pb_ma", "pb2_ma", "pb3_ma", "stop_mode", "stop_ma", "sl", "tp_mode", "tp_ma", "tp",
                                                 "exit_ma", "entry", "entry_pct", "side", "max_hold", "win_reset", "opp_tp", "opp_block", "far_pct", "far_sl", "far_tp", "dir_filter", "dfast_ma", "dslow_ma")}
        if pos and self.last_price:
            d = pos["dir"]
            out["pos"] = {"dir": "long" if d > 0 else "short", "qty": pos["qty"], "entry": pos["entry"],
                          "ret_pct": d * (self.last_price / pos["entry"] - 1) * 100,
                          "pnl_usdt": d * (self.last_price - pos["entry"]) * pos["qty"]}
        try:
            if self.ex is not None:
                out["equity"] = self.ex.equity()
        except Exception:  # noqa: BLE001
            pass
        if res and res.get("sim"):
            self._view = {"pending": res["sim"].get("pending"), "lines": line_values(res, c["q"] if c else {})}
        if c:
            out["rule_text"] = rule_summary(c["q"], c["interval"])
            view = getattr(self, "_view", None) or {}
            out["pending"] = view.get("pending")
            out["line_now"] = view.get("lines") or []
        tmp = self.dir / (STATUS_FILE + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / STATUS_FILE)

    def loop(self) -> None:
        last_step = 0.0
        while True:
            try:
                if self.now() - last_step >= STEP_SEC:
                    last_step = self.now()
                    self.step()
                else:
                    self.process_requests()
            except Exception as exc:  # noqa: BLE001 네트워크 오류 등 → 잠깐 쉬고 계속
                log.exception("오류: %s", exc)
                self.msg(f"오류: {exc}")
                try:
                    self.save()
                    self.write_status()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(5)
            time.sleep(0.5)


# ------------------------------------------------------------ 지금 쓰는 조건 (화면 '진행 중인 조건' 칸)
TF_NAME = {"1m": "1분", "3m": "3분", "5m": "5분", "15m": "15분", "30m": "30분", "1h": "1시간", "2h": "2시간",
           "4h": "4시간", "6h": "6시간", "12h": "12시간", "1d": "일"}
KIND_NAME = {"sma": "MA", "ema": "EMA", "vwma": "VWMA"}


def _line_name(q: dict, name: str, interval: str) -> str:
    ma = q.get(f"{name}_ma") or ""
    if ":" not in ma:
        return ""
    kind, n = ma.split(":", 1)
    tf = q.get(f"{name}_tf") or interval
    return f"{TF_NAME.get(tf, tf)}봉 {KIND_NAME.get(kind, kind)}{n}"


def rule_summary(q: dict, interval: str) -> list[list[str]]:
    """봇이 지금 쓰는 규칙을 [항목, 내용] 줄로. 봉 수는 늘 5분봉 기준 (base5)."""
    ln = lambda name: _line_name(q, name, interval)  # noqa: E731
    num = lambda k: float(q.get(k) or 0)  # noqa: E731
    pbs = [ln(k) for k in ("pb", "pb2", "pb3") if q.get(f"{k}_kind", "on") != "off" and ln(k)]
    side = {"long": "롱만", "short": "숏만", "both": "롱·숏"}.get(q.get("side", "long"), q.get("side", ""))
    rows = [["방향", side]]
    ent = " / ".join(pbs) or "-"
    how = "닿으면 지정가" if q.get("entry", "touch") == "touch" else f"닿은 뒤 5분봉 종가 확인 ({int(num('confirm_bars') or 5)}봉 안)"
    pre = f", 선 {num('entry_pct'):g}% 앞에 미리 걸기" if num("entry_pct") else ""
    rows.append(["진입", f"{ent} {how}{pre}"])
    rows.append(["진입 조건", f"직전 5분봉 {int(num('min_above') or 5)}개 종가가 선 위(숏은 아래)"])
    trend = q.get("trend", "none")
    if trend != "none":
        t = {"slope": "진입선 기울기 같은 방향", "above": f"가격이 {ln('trend')} 위(숏은 아래)",
             "both": f"진입선 기울기 + {ln('trend')} 위(숏은 아래)"}.get(trend, trend)
        rows.append(["추세 조건", t])
    if q.get("dir_filter") == "align":
        rows.append(["방향 필터", f"{ln('dfast')} > {ln('dslow')} 이면 롱만, 반대면 숏만"])
    if num("win_reset"):
        rows.append(["이익 뒤", f"직전 5분봉 {int(num('win_reset'))}개 고점(롱)·저점(숏)을 먼저 넘어야 다시 진입"])
    if len(pbs) > 1 and (num("opp_tp") or num("opp_block")):
        rows.append(["반대 진입선", f"{num('opp_tp'):g}% 이하로 가까우면 거기서 익절, {num('opp_block'):g}% 미만이면 진입 안 함"])
    sm = q.get("stop_mode", "pct")
    sline = "진입한 선" if q.get("stop_ma") == "same" else ln("stop")
    stop = {"none": "없음", "pct": f"진입가 -{num('sl'):g}% 닿으면",
            "line": f"5분봉 종가가 {sline}에서 {num('sl'):g}% 넘게 벗어나면 시장가",
            "touch": f"{sline} -{num('sl'):g}% 닿으면 (거래소 손절)"}.get(sm, sm)
    rows.append(["손절", stop])
    tm = q.get("tp_mode", "pct")
    tp = {"none": "없음", "pct": f"진입가 +{num('tp'):g}% 지정가", "touch": f"{ln('tp')} 닿으면"}.get(tm, tm)
    rows.append(["익절", tp])
    if q.get("exit_ma") and q.get("exit_ma") != "off" and ln("exit"):
        rows.append(["추가 청산", f"5분봉 종가가 {ln('exit')}에서 {num('exit_pct'):g}% 넘게 벗어나면"])
    if num("far_pct"):
        rows.append(["반대 진입", f"가격이 진입선 전부의 바깥일 때 마지막 선(숏 대기면 가장 아래 선)에서 5분봉 종가가 {num('far_pct'):g}% 넘게 벗어나면 "
                                f"반대 방향 시장가 · 손절 -{float(q.get('far_sl') or 1):g}% · 익절 +{float(q.get('far_tp') or 3):g}%"])
    if num("max_hold"):
        mh = int(num("max_hold"))
        rows.append(["최대 보유", f"5분봉 {mh}개 ({mh * 5}분) 지나면 정리"])
    return rows


def line_values(res: dict, q: dict) -> list[list]:
    """지금 선 값 [이름, 값] (진행 중인 봉 포함)."""
    live = res.get("live") or {}
    out = []
    for i, v in enumerate(live.get("pb_lines") or []):
        if v and v[-1] is not None:
            out.append([f"진입선 {i + 1}", v[-1]])
    for key, name in (("stop", "손절선"), ("tp", "익절선"), ("exit", "청산선")):
        v = live.get(key)
        if key == "stop" and (live.get("stop_same") or q.get("stop_mode") not in ("line", "touch")):
            continue
        if key == "tp" and q.get("tp_mode") != "touch":
            continue
        if v and v[-1] is not None:
            out.append([name, v[-1]])
    return out


# ------------------------------------------------------------ 화면(maweb) 쪽: 요청 파일·상태 읽기
FIVE_TF_KEYS = ("tf1", "tf2", "tf3", "tf4") + tuple(f"{n}_tf" for n in
                                               ("pb", "pb2", "pb3", "trend", "stop", "tp", "exit", "dfast", "dslow"))


def base5(q: dict, interval: str) -> dict:
    """봇 계산은 차트 봉과 상관없이 늘 5분봉으로: 봉을 비워 둔 선(= 차트 봉)은 원래 차트 봉 선으로 남기고
    진입 조건 봉 수·최대 보유 봉·손절·익절·청산 판단은 5분봉마다 (큰 봉 선도 5분봉 종가로 판단)."""
    out = dict(q)
    for k in FIVE_TF_KEYS:
        if not out.get(k):
            out[k] = interval
    out["interval"], out["ends5"] = "5m", "1"
    return out


def write_request(state_dir: str, req: dict) -> None:
    d = Path(state_dir) / REQ_DIR
    d.mkdir(parents=True, exist_ok=True)
    req = {**req, "time": time.time()}
    name = f"{time.time():.3f}_{uuid.uuid4().hex[:6]}.json"
    tmp = d / (name + ".tmp")
    tmp.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, d / name)


def read_status(state_dir: str) -> dict:
    p = Path(state_dir) / STATUS_FILE
    if not p.exists():
        return {"alive": False}
    try:
        s = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return {"alive": False}
    s["alive"] = time.time() - s.get("time", 0) < ALIVE_SEC
    return s


# ------------------------------------------------------------ 명령
def main() -> None:
    from tossbot.config import load_dotenv

    ap = argparse.ArgumentParser(description="이평선 눌림 매매 실거래 봇")
    ap.add_argument("action", choices=["run", "check"])
    ap.add_argument("--env", default=".env.ma")
    a = ap.parse_args()
    load_dotenv(a.env)
    s = Settings.from_env()
    if a.action == "check":
        print(f"모드: {'드라이런 (주문 없음, 가상 계좌)' if s.dry_run else '실제 주문'} · 투입 상한 {s.max_usdt:g} USDT · "
              f"배율 상한 {s.max_lev:g} · PIN {'있음' if s.pin else '없음'}")
        if not s.dry_run:
            if not (s.api_key and s.api_secret):
                print(f"실제 주문인데 API 키가 비어 있어요. 메모장으로 {a.env} 를 열어 키를 넣어 주세요.")
                return
            c = BinanceClient(s.api_key, s.api_secret)
            c.sync_time()
            print(f"서버 시각 차이 {c.offset_ms} ms · 선물 평가금액 {c.equity():.2f} USDT · "
                  f"{'양방향(헤지) 모드 → 단방향으로 바꿔야 해요' if c.hedge_mode() else '단방향 모드 정상'}")
        return
    from .botmain import setup_logging
    from kiwoombot.__main__ import acquire_lock
    setup_logging(s.state_dir)
    if not s.dry_run and not (s.api_key and s.api_secret):
        print(f"실제 주문(MA_DRY_RUN=false)인데 API 키가 비어 있어요. 메모장으로 {a.env} 를 열어 키를 넣어 주세요.")
        return
    if not s.dry_run and not s.pin:
        print(f"실제 주문은 PIN 이 필요해요. {a.env} 의 MA_PIN 에 비밀번호(숫자)를 넣어 주세요.")
        return
    lock = acquire_lock(s.state_dir)
    if lock is None:
        print("이평선 봇이 이미 다른 창에서 켜져 있어요.")
        return
    log.info("이평선 봇 시작: %s · 투입 상한 %g USDT · 배율 상한 %g",
             "드라이런(가상 계좌)" if s.dry_run else "실제 주문", s.max_usdt, s.max_lev)
    log.info("이평선 매매 화면(http://localhost:8790)에서 '자동 매매 시작' 또는 롱·숏 버튼을 누르면 이 봇이 주문해요.")
    MaBot(s).loop()


if __name__ == "__main__":
    main()
