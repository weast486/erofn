"""바이낸스 종가 매매 (오버나이트, 롱만) — 낮 FVG 봇과 같은 계좌·같은 돈을 시간만 나눠 쓴다.

규칙 (시각은 미국 동부, 2026-10-06 백테스트: 주식 4년 모두 이익, 바이낸스 선물 8개월 62건 거래당 +2.6%):
  15:56:30  주식 토큰 전체의 오늘 정규장 1분봉을 읽어 시가 대비 많이 내린 종목을 추림
  15:58:30  현재가로 다시 확인: 장중 등락(현재가/시가-1) <= -on_drop% 이고 종가 위치((현재가-저가)/(고가-저가)) < on_pos
            → 많이 내린 순으로 on_max 종목까지 시장가 매수 (종목마다 평가금액 x on_leverage 배)
            → 거래소 손절(STOP_MARKET) = 매수가 -on_stop% (밤사이 비상용; 좁은 손절은 백테스트에서 수익·낙폭 모두 나빴음)
  다음 거래일 9:30  손절 주문 취소, 남은 수량 시장가 매도 (손절로 이미 정리됐으면 기록만)
상태는 state_dir/overnight.json (positions = 보유 중, history = 정리한 거래), 거래 내역은 trades.csv (reason = night / night_stop).
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import date
from pathlib import Path

from .backtest import US_EARLY_CLOSE
from .client import BinanceError, filters_of, fmt, round_step, round_tick

log = logging.getLogger("binancebot")


class Overnight:
    def __init__(self, trader):
        self.t, self.c, self.cfg = trader, trader.c, trader.cfg
        self.path = Path(self.cfg.state_dir) / "overnight.json"

    # ------------------------------------------------------------ 상태 파일
    def load(self) -> dict:
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text(encoding="utf-8"))
                st.setdefault("positions", {})
                st.setdefault("last_scan", "")
                return st
            except ValueError:
                log.warning("overnight.json 을 읽지 못해 새로 시작 (바이낸스 앱에서 보유 종목을 확인하세요)")
        return {"positions": {}, "last_scan": ""}

    def save(self, st: dict) -> None:
        self.path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------ 아침: 9:30 정리
    def exit_open(self, day: date) -> None:
        """전날(또는 그 전) 종가에 산 종목을 오늘 9:30 에 정리. 들고 있는 것이 없으면 아무것도 안 함."""
        from .bot import at

        st = self.load()
        held = {s: p for s, p in st["positions"].items() if p.get("day", "") < day.isoformat()}
        if not held:
            return
        self.t.wait_until(at(day, 9, 30, 1))
        px = self.c.prices()
        for sym, pos in held.items():
            self._close(st, sym, pos, px.get(sym, 0.0))

    def _close(self, st: dict, sym: str, pos: dict, price: float) -> None:
        reason = "night"
        if self.cfg.dry_run:
            if price and price <= pos["stop"]:
                reason, price = "night_stop", pos["stop"]
            log.info("[드라이런] %s 종가 매매 정리 @ %g", sym, price)
        else:
            amt, _ = self.c.position_amt(sym)
            self.c.cancel_all(sym)
            if amt <= 0:  # 밤사이 거래소 손절로 이미 정리됨
                reason, price = "night_stop", pos["stop"]
                log.info("%s 종가 매매: 밤사이 손절(%g)로 이미 정리됨", sym, pos["stop"])
            else:
                left = min(amt, pos["qty"])
                for _ in range(3):  # 응답 지연으로 실패해도 남아 있으면 다시 (reduceOnly 라 더 팔리지 않음)
                    if left <= 0:
                        break
                    try:
                        r = self.c.order(symbol=sym, side="SELL", type="MARKET", quantity=fmt(left), reduceOnly="true",
                                         newOrderRespType="RESULT")
                        price = float(r.get("avgPrice", 0) or price)
                        log.info("%s 종가 매매 시장가 정리 %s개 @ %g", sym, fmt(left), price)
                        left = 0.0
                    except BinanceError as exc:
                        log.warning("%s 종가 매매 정리 주문 오류 (%s), 포지션 다시 확인", sym, exc)
                        self.t.sleep(2)
                        amt, _ = self.c.position_amt(sym)
                        left = min(max(amt, 0.0), pos["qty"])
                amt, _ = self.c.position_amt(sym)
                if amt > 0:
                    log.error("%s 포지션이 아직 %s개 남아 있어요 — 바이낸스 앱에서 직접 정리해 주세요", sym, fmt(amt))
        if not self.cfg.dry_run and pos.get("entry_time"):
            from datetime import datetime

            real, _ = self.t.real_fill(sym, "SELL", since=datetime.fromisoformat(pos["entry_time"]))
            if real > 0:  # 손절가·현재가 대신 실제 체결 평균가로 기록
                price = real
        ret = (price / pos["entry"] - 1) * 100 if price else 0.0
        pnl = (price - pos["entry"]) * pos["qty"] if price else 0.0
        log.info("%s 종가 매매 끝: %s @ %g, %+.2f%% (수수료 전 %+.2f USDT)", sym,
                 "손절" if reason == "night_stop" else "장 시작 정리", price, ret, pnl)
        path = Path(self.cfg.state_dir) / "trades.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["day", "mode", "symbol", "side", "qty", "entry", "stop", "tp", "exit", "reason", "ret_pct", "pnl"])
            w.writerow([pos["day"], "dry" if self.cfg.dry_run else "live", sym, 1, pos["qty"], pos["entry"], pos["stop"], 0.0,
                        price, reason, round(ret, 3), round(pnl, 4)])
        st["positions"].pop(sym, None)
        hist = st.setdefault("history", [])  # 현황 페이지 차트용: 최근 정리한 거래의 시각·가격
        hist.append(dict(pos, exit=price, exit_time=self.t.now().isoformat(), reason=reason, ret_pct=round(ret, 3)))
        del hist[:-200]
        self.save(st)

    # ------------------------------------------------------------ 저녁: 종가 매수
    def enter_close(self, day: date) -> None:
        from .bot import at, ms

        cfg = self.cfg
        if not cfg.on_enabled:
            return
        st = self.load()
        if st["last_scan"] == day.isoformat():
            return
        if day in US_EARLY_CLOSE:
            log.info("%s 은 13시 조기 마감이라 종가 매매를 쉽니다", day)
            return
        close_t = at(day, 16, 0)
        if self.t.now() >= at(day, 15, 59, 30):
            log.info("장 마감 직전·이후라 오늘 종가 매매는 쉽니다")
            return
        if st["positions"]:
            log.info("종가 매매 종목을 아직 들고 있어 새로 사지 않습니다: %s", ", ".join(st["positions"]))
            return
        self.t.wait_until(at(day, 15, 56, 30))
        if not self.t.meta:
            self.t.load_meta()
        open_ms, close_ms = ms(at(day, 9, 30)), ms(close_t)
        short = []  # (종목, 시가, 고가, 저가)
        for sym, m in self.t.meta.items():
            if m.get("underlyingType") != "EQUITY":
                continue
            try:
                rows = [k for k in self.c.klines(sym, open_ms, limit=400) if int(k[0]) < close_ms]
            except BinanceError as exc:
                log.warning("%s 오늘 1분봉 조회 실패, 종가 매매 대상에서 빠짐 (%s)", sym, exc)
                continue
            if not rows or int(rows[0][0]) != open_ms:
                continue
            o, last = float(rows[0][1]), float(rows[-1][4])
            if sum(float(k[7]) for k in rows) < cfg.on_min_qv or o <= 0:
                continue
            if (last / o - 1) * 100 <= -(cfg.on_drop - 2):  # 여유 있게 추리고 15:58:30 현재가로 다시 확인
                short.append((sym, o, max(float(k[2]) for k in rows), min(float(k[3]) for k in rows)))
            self.t.sleep(0.03)
        self.t.wait_until(at(day, 15, 58, 30))
        px = self.c.prices() if short else {}
        picks = []
        for sym, o, h, l in short:
            p = px.get(sym)
            if not p:
                continue
            h, l = max(h, p), min(l, p)
            intra = (p / o - 1) * 100
            pos = (p - l) / (h - l) if h > l else 1.0
            ok = intra <= -cfg.on_drop and pos < cfg.on_pos
            log.info("종가 매매 후보 %s: 장중 %+.1f%%, 종가 위치 %.2f (0 = 저가) → %s", sym, intra, pos, "매수" if ok else "조건 안 맞음")
            if ok:
                picks.append((intra, sym, p))
        picks.sort()
        st["last_scan"] = day.isoformat()
        self.save(st)
        if not picks:
            log.info("오늘 종가 매매 대상 없음 (장중 -%g%% 이하 + 저가 근처 마감)", cfg.on_drop)
            return
        for intra, sym, p in picks[: cfg.on_max]:
            if self.t.now() >= at(day, 16, 0, 30):
                log.info("%s: 장 마감이 지나 종가 매수 안 함", sym)
                break
            self._buy(st, day, sym, p, intra)

    def _buy(self, st: dict, day: date, sym: str, price: float, intra: float) -> bool:
        cfg = self.cfg
        f = filters_of(self.t.meta.get(sym, {}))
        eq = self.t.equity()
        qty = round_step(eq * cfg.on_leverage / price, f["step"])
        if qty <= 0 or qty * price < f["min_notional"]:
            log.info("%s 종가 매수 수량이 최소 주문 금액보다 작아 제외 (평가금액 %.2f)", sym, eq)
            return False
        if cfg.dry_run:
            filled, avg = qty, price
            log.info("[드라이런] %s 종가 매수 %s개 @ %g (장중 %+.1f%%, 평가금액 %.2f)", sym, fmt(qty), price, intra, eq)
        else:
            try:
                self.c.set_margin_type(sym, "CROSSED")
                self.c.set_leverage(sym, max(cfg.exchange_leverage, round(cfg.on_leverage + 0.4999)))
                r = self.c.order(symbol=sym, side="BUY", type="MARKET", quantity=fmt(qty), newOrderRespType="RESULT")
            except BinanceError as exc:
                if exc.code in (-1007, -1001) or exc.status == 408:  # 보냈는지 모름 → 실제 포지션으로 확인
                    self.t.sleep(2)
                    amt, ep = self.c.position_amt(sym)
                    log.warning("%s 종가 매수 응답 지연 (%s) → 포지션 확인 %s개", sym, exc, fmt(abs(amt)))
                    r = {"executedQty": str(max(amt, 0.0)), "avgPrice": str(ep or price)}
                else:
                    log.error("%s 종가 매수 주문 실패: %s", sym, exc)
                    return False
            filled, avg = float(r.get("executedQty", 0)), float(r.get("avgPrice", 0) or price)
            if filled <= 0:
                log.info("%s 종가 매수 체결 없음", sym)
                return False
            real, _ = self.t.real_fill(sym, "BUY", order_id=r.get("orderId"))
            if real > 0:  # 주문 응답의 평균가 대신 실제 체결 평균가로 기록
                avg = real
        stop = round_tick(avg * (1 - cfg.on_stop / 100), f["tick"])
        pos = {"symbol": sym, "day": day.isoformat(), "qty": filled, "entry": avg, "stop": stop,
               "entry_time": self.t.now().isoformat(), "equity": eq, "intra": round(intra, 2), "stop_order": ""}
        st["positions"][sym] = pos
        self.save(st)
        if not cfg.dry_run:
            try:
                pos["stop_order"] = self.c.stop_market(sym, "SELL", stop)
            except BinanceError as exc:
                log.warning("%s 종가 매매 손절 주문 실패 (%s) → 밤사이 손절 없이 보유합니다. 바이낸스 앱에서 직접 걸 수 있어요", sym, exc)
            self.save(st)
        log.info("%s 종가 매수 %s개 @ %g (장중 %+.1f%%), 비상 손절 %g%s → 다음 거래일 9:30 정리", sym, fmt(filled), avg,
                 intra, stop, "" if pos["stop_order"] or cfg.dry_run else " (주문 실패)")
        return True
