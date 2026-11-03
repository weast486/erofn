r"""SOXL 한 번 롱 3분할 (2026-11-03 사용자 요청): 71.0 / 70.8 / 70.6 지정가 매수(평가금액 x 2배를 3등분),
손절 70.4 · 익절 73.5 (둘 다 거래소 조건 주문 closePosition — 첫 체결 뒤 바로 걸어서 이 창이 꺼져도 작동).

정리되면(손절·익절·앱에서 정리) 남은 매수 주문까지 모두 취소하고 끝. 하나도 안 산 채 가격이 익절가에 닿으면
매수 주문을 취소하고 끝.

  python scripts/soxl_ladder3.py run     # 주문 걸고 지켜봄 (windows\soxl_ladder3.bat), 다시 켜면 이어서
  python scripts/soxl_ladder3.py check   # 지금 포지션·걸린 주문만 보여 줌 (주문 없음)

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

SYMBOL, LEVERAGE = "SOXLUSDT", 2.0
LEVELS, STOP, TP = (71.0, 70.8, 70.6), 70.4, 73.5
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "ladder3.json", DIR / "ladder3_trades.csv"
log = logging.getLogger("ladder3")


def setup() -> BinanceClient:
    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    f = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "ladder3.log", encoding="utf-8")):
        h.setFormatter(f)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def record(event: str, price: float, qty: float, pnl: float) -> None:
    new = not TRADES.exists()
    with open(TRADES, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "event", "price", "qty", "pnl"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), event, f"{price:.4f}", f"{qty:.6f}", f"{pnl:.4f}"])


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st), encoding="utf-8")


def place(c: BinanceClient) -> dict:
    amt, _ = c.position_amt(SYMBOL)
    if amt != 0:
        raise SystemExit(f"이미 포지션 {amt} 이 있어 시작 안 함")
    f = filters_of(next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL))
    eq = c.equity()
    st = {"start": int(time.time() * 1000), "equity": eq, "orders": [], "protected": False}
    for k, px in enumerate(LEVELS, 1):
        px = round_tick(px, f["tick"])
        qty = round_step(eq * LEVERAGE / len(LEVELS) / px, f["step"])
        cid = f"lad3_b{k}_{int(time.time())}"
        c.order(symbol=SYMBOL, side="BUY", type="LIMIT", timeInForce="GTC", price=fmt(px), quantity=fmt(qty),
                newClientOrderId=cid)
        st["orders"].append(cid)
        log.info("%d차 매수 지정가 %.2f x %g (%.2f USDT)", k, px, qty, px * qty)
    save(st)
    log.info("평가금액 %.2f x %g배를 3등분, 손절 %.2f · 익절 %.2f 는 첫 체결 뒤 거래소에 걸어 둠", eq, LEVERAGE, STOP, TP)
    return st


def protect(c: BinanceClient, st: dict) -> None:
    for kind, px in (("STOP_MARKET", STOP), ("TAKE_PROFIT_MARKET", TP)):
        try:
            c.stop_market(SYMBOL, "SELL", px, kind=kind)
            log.info("거래소 %s %.2f 걸어 둠 (전량)", "손절" if kind == "STOP_MARKET" else "익절", px)
        except Exception as exc:  # noqa: BLE001
            log.warning("거래소 %s 실패 (%s) → 이 창이 가격 보고 처리", kind, exc)
    st["protected"] = True
    save(st)


def finish(c: BinanceClient, st: dict) -> None:
    c.cancel_all(SYMBOL)  # 남은 매수·손절·익절 주문 모두 정리
    rows = c.fills(SYMBOL, start_ms=st["start"] - 60_000)
    buys = [t for t in rows if t["side"] == "BUY"]
    sells = [t for t in rows if t["side"] == "SELL"]
    bq, sq = sum(float(t["qty"]) for t in buys), sum(float(t["qty"]) for t in sells)
    if bq:
        bp = sum(float(t["price"]) * float(t["qty"]) for t in buys) / bq
        sp = sum(float(t["price"]) * float(t["qty"]) for t in sells) / sq if sq else 0.0
        fee = sum(float(t.get("commission") or 0) for t in rows)
        pnl = sq * sp - bq * bp - fee
        record("buy", bp, bq, 0.0)
        record("tp" if sp >= TP * 0.998 else "stop" if sp and sp <= STOP * 1.002 else "close", sp, sq, pnl)
        log.info("정리 끝: 평균 매수 %.4f x %g → 평균 매도 %.4f, 손익 %+.4f USDT (수수료 포함)", bp, bq, sp, pnl)
    STATE.unlink(missing_ok=True)


def run(c: BinanceClient) -> None:
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else place(c)
    held = 0.0
    while True:
        try:
            amt, _ = c.position_amt(SYMBOL)
            p = c.prices()[SYMBOL]
            if amt > 0:
                if amt != held:
                    log.info("보유 %g (현재가 %.4f)", amt, p)
                    held = amt
                if not st["protected"]:
                    protect(c, st)
                if p <= STOP or p >= TP:  # 거래소 주문이 안 걸렸거나 아직 → 직접 시장가
                    c.cancel_all(SYMBOL)
                    c.order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=fmt(amt), reduceOnly="true",
                            newClientOrderId=f"lad3_x_{int(time.time())}")
                    log.info("현재가 %.4f 가 %s → 시장가 정리", p, "손절가 이하" if p <= STOP else "익절가 이상")
            elif st["protected"]:
                log.info("포지션 0 (손절·익절·앱에서 정리) → 남은 주문 취소하고 끝")
                finish(c, st)
                return
            elif p >= TP:
                log.info("하나도 안 산 채 현재가 %.4f 가 익절가 %.2f 이상 → 매수 주문 취소하고 끝", p, TP)
                c.cancel_all(SYMBOL)
                STATE.unlink(missing_ok=True)
                return
        except Exception as exc:  # noqa: BLE001
            log.warning("확인 실패, 5초 뒤 다시: %s", exc)
        time.sleep(5)


if __name__ == "__main__":
    client = setup()
    if (sys.argv[1] if len(sys.argv) > 1 else "check") == "run":
        run(client)
    else:
        print("포지션", client.position_amt(SYMBOL), "현재가", client.prices()[SYMBOL])
        for o in client.open_orders(SYMBOL):
            print(o.get("clientOrderId"), o.get("side"), o.get("type"), o.get("price"), o.get("origQty"))
