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
    state_dir: str = "state_ma"

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(api_key=e("BINANCE_API_KEY", ""), api_secret=e("BINANCE_API_SECRET", ""),
                   dry_run=(e("MA_DRY_RUN", "true").strip().lower() not in ("false", "0", "no")),
                   pin=e("MA_PIN", ""), max_usdt=_f(e("MA_MAX_USDT"), 100.0), max_lev=_f(e("MA_MAX_LEV"), 5.0),
                   paper_equity=_f(e("MA_PAPER_EQUITY"), 1000.0),
                   safety_stop=_f(e("MA_SAFETY_STOP"), 10.0), state_dir=e("MA_STATE_DIR", "state_ma"))


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
            c = self.st["cfg"]
            self.msg(f"자동 매매 시작: {c['symbol']} {c['interval']} · 투입 {c['usdt']:g} USDT x {c['lev']:g}배")
        elif cmd == "stop":
            self.st["enabled"] = False
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

    def step(self) -> None:
        self.process_requests()
        c = self.st.get("cfg")
        if not c:
            self.write_status()
            return
        ex = self.exchange(c["symbol"])
        price = ex.price()
        self.last_price = price
        qty, entry = ex.position()
        res = run(self.market, {**c["q"], "symbol": c["symbol"], "interval": c["interval"], "_live": "1"})
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
        want = {str(w["dir"]): round_tick(w["price"], tick) for w in (sim or {}).get("wait_orders", [])}
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
                have[d] = {"cid": cid, "price": px, "qty": qty}
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
        pos = self.st.get("pos")
        if not pos or pos.get("dir") != d:
            self._cancel_entries()
            self.ex.cancel_all()
            pos = self.st["pos"] = {"dir": d, "entry": entry, "qty": abs(qty), "t_ms": int(self.now() * 1000),
                                    "bar": bars[-1]["t"], "last_closed": bars[n - 2]["t"], "armed": False}
            self.st["exit"] = {}
            self.msg(f"{'롱' if d > 0 else '숏'} 보유 시작 {abs(qty):g}개 @ {entry:.6g}")
        pos["entry"], pos["qty"] = entry, abs(qty)
        sl, tpp = _f(q.get("sl")), _f(q.get("tp"))
        stop_mode, tp_mode = q.get("stop_mode", "pct"), q.get("tp_mode", "pct")
        prev_c = bars[n - 2]["c"]
        stop_px = tp_px = None
        if stop_mode == "pct" and sl > 0:
            stop_px = entry * (1 - d * sl / 100)
        elif stop_mode == "touch" and live.get("stop") and live["stop"][n - 2] is not None:
            v = live["stop"][n - 2] * (1 - d * sl / 100)
            first = bars[-1]["t"] == pos["bar"]
            if (first and d * (entry - v) > 0) or (not first and d * (prev_c - v) > 0):
                stop_px = v
        safety = False
        if stop_px is None and self.cfg.safety_stop > 0:   # 규칙 손절이 안 걸리는 동안 비상 손절
            stop_px, safety = entry * (1 - d * self.cfg.safety_stop / 100), True
        if bars[-1]["t"] > pos["bar"]:   # 익절은 산 다음 봉부터 (모의 매매와 같게)
            if tp_mode == "pct" and tpp > 0:
                tp_px = entry * (1 + d * tpp / 100)
            elif tp_mode == "touch" and live.get("tp") and live["tp"][n - 2] is not None:
                v = live["tp"][n - 2]
                if d * (v - entry) > 0 and d * (v - prev_c) > 0:
                    tp_px = v
        # 봉이 끝났을 때 종가로 판단하는 것들 (산 봉부터)
        k = n - 2
        if bars[k]["t"] > pos["last_closed"] and bars[k]["t"] >= pos["bar"]:
            pos["last_closed"] = bars[k]["t"]
            cl = bars[k]["c"]
            reason = None
            sv, se = live.get("stop"), live.get("stop_ends")
            if stop_mode == "line" and sv and sv[k] is not None and (se is None or se[k]) \
                    and d * (cl - sv[k] * (1 - d * sl / 100)) < 0:
                reason = "line"
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
            self.close_now(self.ex.sym, "safety" if safety else "stop")
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
                self.msg(f"손절 {want['stop'] or '-'}{' (비상)' if safety else ''} · 익절 {want['tp'] or '-'}")
            self.st["exit"] = want

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
        if not reason:   # 거래소 손절·익절 주문으로 정리됨 → 가까운 쪽
            ex = self.st.get("exit", {})
            cands = [(abs(px - v), r) for r, v in (("stop", ex.get("stop")), ("take", ex.get("tp"))) if v]
            reason = min(cands)[1] if cands else "?"
            if reason == "stop" and ex.get("safety"):
                reason = "safety"
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
        self.st["pos"] = None

    def write_status(self, res: dict | None = None) -> None:
        c = self.st.get("cfg")
        pos = self.st.get("pos")
        out = {"time": int(self.now()), "dry": self.cfg.dry_run, "enabled": self.st.get("enabled"),
               "max_usdt": self.cfg.max_usdt, "max_lev": self.cfg.max_lev, "safety_stop": self.cfg.safety_stop, "pin_set": bool(self.cfg.pin),
               "cfg": {k: c[k] for k in ("symbol", "interval", "usdt", "lev")} if c else None,
               "price": self.last_price, "msgs": self.st["msgs"][-15:], "trades": self.st["trades"][-20:],
               "entry_orders": list(self.st["entry_orders"].values()), "exit": self.st.get("exit") or {}}
        if c:
            q = c["q"]
            out["rule"] = {k: q.get(k) for k in ("pb_ma", "stop_mode", "stop_ma", "sl", "tp_mode", "tp_ma", "tp",
                                                 "exit_ma", "entry", "entry_pct", "side", "max_hold")}
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
            out["pending"] = res["sim"].get("pending")
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


# ------------------------------------------------------------ 화면(maweb) 쪽: 요청 파일·상태 읽기
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
