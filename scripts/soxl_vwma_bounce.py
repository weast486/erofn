r"""SOXL 한 번 롱 (2026-11-03 사용자 요청): 5분봉 거래량가중 100선에서 양봉이 나오면 다음 봉 시가에 평가금액 x 2배 롱,
손절 = 그 양봉 저가 (거래소 STOP_MARKET closePosition — 이 창이 꺼져도 손절은 걸려 있음). 익절은 없음.

신호 봉 = 끝난 5분봉 중 양봉(종가 > 시가)이고 저가 ≤ 그 봉까지의 거래량가중 100선 ≤ 종가 (선에 닿고 위에서 끝남).
이 창을 켠 뒤에 끝난 봉만 봄. 진입 순간 가격이 이미 손절가 이하면 사지 않고 다음 신호를 기다림.

  python scripts/soxl_vwma_bounce.py run     # 신호 기다림 → 진입 → 정리될 때까지 지켜봄 (windows\soxl_vwma_bounce.bat)
  python scripts/soxl_vwma_bounce.py check   # 지금 선·마지막 봉만 보여 줌 (주문 없음)

스플릿 봇과 같은 계정·같은 종목이라 이 롱이 끝날 때까지 스플릿 봇은 꺼 둬야 함. 키는 .env.binance_split.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tossbot.config import load_dotenv  # noqa: E402

from binancebot.client import BinanceClient, filters_of, fmt, round_step, round_tick  # noqa: E402

SYMBOL, N, TF_MS, LEVERAGE = "SOXLUSDT", 100, 5 * 60_000, 2.0
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "vwma_bounce.json", DIR / "vwma_bounce_trades.csv"
log = logging.getLogger("vwma_bounce")


def setup() -> BinanceClient:
    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    f = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "vwma_bounce.log", encoding="utf-8")):
        h.setFormatter(f)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def last_bar(c: BinanceClient) -> dict:
    """마지막 끝난 5분봉과 그 봉까지(포함) 거래량가중 100선."""
    now = int(time.time() * 1000)
    rows = c.klines(SYMBOL, now - TF_MS * (N + 4), limit=N + 6, interval="5m")
    done = [r for r in rows if int(r[6]) < now][-N:]
    vw = sum(float(r[4]) * float(r[5]) for r in done) / sum(float(r[5]) for r in done)
    r = done[-1]
    return {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]), "vw": vw}


def avg_fill(c: BinanceClient, r: dict, fallback: float) -> float:
    for _ in range(3):
        rows = [t for t in c.fills(SYMBOL, order_id=r.get("orderId")) if str(t.get("orderId")) == str(r.get("orderId"))]
        q = sum(float(t["qty"]) for t in rows)
        if q > 0:
            return sum(float(t["price"]) * float(t["qty"]) for t in rows) / q
        time.sleep(0.5)
    return float(r.get("avgPrice") or 0) or fallback


def record(event: str, price: float, qty: float, pnl: float) -> None:
    new = not TRADES.exists()
    with open(TRADES, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "event", "price", "qty", "pnl"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event, f"{price:.4f}", f"{qty:.6f}", f"{pnl:.4f}"])


def hhmm(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def enter(c: BinanceClient, bar: dict, f: dict) -> bool:
    amt, _ = c.position_amt(SYMBOL)
    if amt != 0:
        log.info("이미 포지션 %g 이 있어 진입 안 함", amt)
        return False
    stop = round_tick(bar["l"], f["tick"])
    p = c.prices()[SYMBOL]
    if p <= stop:
        log.info("현재가 %.4f 가 이미 손절가(양봉 저가) %.4f 이하 → 진입 안 하고 다음 신호 기다림", p, stop)
        return False
    eq = c.equity()
    qty = round_step(eq * LEVERAGE / p, f["step"])
    r = c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(qty),
                newClientOrderId=f"vwb_buy_{int(time.time())}", newOrderRespType="RESULT")
    done = float(r.get("executedQty") or 0)
    if done <= 0:
        log.warning("시장가 매수가 체결되지 않음 → 다음 신호 기다림")
        return False
    px = avg_fill(c, r, p)
    try:
        c.stop_market(SYMBOL, "SELL", stop)
        log.info("거래소 손절 %.4f 걸어 둠 (전량)", stop)
    except Exception as exc:  # noqa: BLE001
        log.warning("거래소 손절 실패 (%s) → 이 창이 가격 보고 손절", exc)
    STATE.write_text(json.dumps({"entry": px, "qty": done, "stop": stop, "bar": bar["t"]}), encoding="utf-8")
    record("buy", px, done, -px * done * 0.0005)
    log.info("롱 진입 %.4f x %g (%.2f USDT, 평가금액 %.2f x %g배), 손절 %.4f (-%.2f%%)",
             px, done, px * done, eq, LEVERAGE, stop, (1 - stop / px) * 100)
    return True


def hold(c: BinanceClient) -> None:
    pos = json.loads(STATE.read_text(encoding="utf-8"))
    log.info("보유 감시: 진입 %.4f x %g, 손절 %.4f", pos["entry"], pos["qty"], pos["stop"])
    while True:
        try:
            amt, _ = c.position_amt(SYMBOL)
            if amt <= 0:
                now = time.time() * 1000
                rows = [t for t in c.fills(SYMBOL, start_ms=now - 6 * 3600_000) if t["side"] == "SELL"]
                q = sum(float(t["qty"]) for t in rows)
                px = sum(float(t["price"]) * float(t["qty"]) for t in rows) / q if q else pos["stop"]
                pnl = pos["qty"] * (px - pos["entry"]) - (px + pos["entry"]) * pos["qty"] * 0.0005
                record("stop" if px <= pos["stop"] * 1.002 else "close", px, pos["qty"], pnl)
                log.info("포지션 정리됨 %.4f (손절 또는 앱에서 정리), 손익 %+.4f USDT → 끝", px, pnl)
                c.cancel_all(SYMBOL)  # 남은 거래소 손절 정리
                STATE.unlink(missing_ok=True)
                return
            p = c.prices()[SYMBOL]
            if p <= pos["stop"]:  # 거래소 손절이 안 걸렸거나 아직 → 직접 시장가
                c.cancel_all(SYMBOL)
                amt, _ = c.position_amt(SYMBOL)
                if amt > 0:
                    c.order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=fmt(amt), reduceOnly="true",
                            newClientOrderId=f"vwb_stop_{int(time.time())}", newOrderRespType="RESULT")
                    log.info("손절가 %.4f 이하 (현재가 %.4f) → 시장가 정리", pos["stop"], p)
        except Exception as exc:  # noqa: BLE001
            log.warning("확인 실패, 10초 뒤 다시: %s", exc)
        time.sleep(10)


def run(c: BinanceClient) -> None:
    if STATE.exists():
        hold(c)
        return
    meta = next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL)
    f = filters_of(meta)
    started = int(time.time() * 1000)
    log.info("신호 기다림: 5분봉 양봉이 거래량가중 100선에 닿고(저가 ≤ 선 ≤ 종가) 끝나면 다음 봉 시가에 %g배 롱, 손절 = 그 양봉 저가",
             LEVERAGE)
    seen = None
    while True:
        try:
            b = last_bar(c)
            if b["t"] != seen:
                seen = b["t"]
                sig = b["t"] + TF_MS > started and b["c"] > b["o"] and b["l"] <= b["vw"] <= b["c"]
                log.info("5분봉 %s 시 %.2f 고 %.2f 저 %.2f 종 %.2f / 100선 %.4f%s", hhmm(b["t"]), b["o"], b["h"], b["l"], b["c"],
                         b["vw"], " → 신호" if sig else "")
                if sig and enter(c, b, f):
                    hold(c)
                    return
        except Exception as exc:  # noqa: BLE001
            log.warning("확인 실패, 10초 뒤 다시: %s", exc)
        time.sleep(5)


if __name__ == "__main__":
    client = setup()
    if (sys.argv[1] if len(sys.argv) > 1 else "check") == "run":
        run(client)
    else:
        b = last_bar(client)
        print(f"마지막 끝난 5분봉 {hhmm(b['t'])} 시 {b['o']} 고 {b['h']} 저 {b['l']} 종 {b['c']} / 거래량가중 100선 {b['vw']:.4f}"
              f" / 현재가 {client.prices()[SYMBOL]}")
