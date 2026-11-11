r"""SOXL 5분봉 50선 4분할 매수 (2026-11-11 사용자 요청): 시작할 때의 5분봉 단순 50선 ±0.5달러 범위를
4등분한 가격(50선+0.5, +0.17, -0.17, -0.5, 안 산 차수는 50선을 따라 계속 옮김)에 평가금액 x 2배를 4분의 1씩 지정가 매수(롱).
체결될 때마다 평균 진입가 기준 거래소 익절 +6%·손절 -2%(포지션 전량 closePosition)를 다시 걸어 둠.
포지션이 0 이 되면(익절·손절·앱에서 정리) 남은 매수 주문을 취소하고 끝. 한 번 매매로 끝.

바이낸스 지정가 허용 범위(현재가 -3% 안)라 범위 밖의 가격은 가격이 내려와 그 안에 들어올 때 이 창이 주문을 냄.

  python scripts/soxl_ma50_ladder.py run      # 감시 (windows\soxl_ma50_ladder.bat), 다시 켜면 이어서
  python scripts/soxl_ma50_ladder.py check    # 지금 상태만 (주문 없음)
  python scripts/soxl_ma50_ladder.py cancel   # 아직 안 산 매수 주문 모두 취소하고 끝 (보유분은 그대로)

키는 .env.binance_split. 같은 계정·같은 종목이라 스플릿 봇·20선 숏 감시는 꺼 둬야 함.
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

from binancebot.client import BinanceClient, BinanceError, filters_of, fmt, round_step, round_tick  # noqa: E402

SYMBOL, LEVERAGE, MA, BAND, PARTS, TP_PCT, SL_PCT = "SOXLUSDT", 2.0, 50, 0.5, 4, 6.0, 2.0
LIMIT_ROOM = 0.972  # 지정가 허용 범위(현재가 -3%)보다 조금 안쪽
MOVE_MIN = 0.05  # 걸린 주문을 옮기는 최소 차이(달러)
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "ma50_ladder.json", DIR / "ma50_ladder_trades.csv"
log = logging.getLogger("ma50ladder")


def setup() -> BinanceClient:
    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    f = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "ma50_ladder.log", encoding="utf-8")):
        h.setFormatter(f)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st), encoding="utf-8")


def sma5m(c: BinanceClient) -> float:
    k = c._req("GET", "/fapi/v1/klines", {"symbol": SYMBOL, "interval": "5m", "limit": MA})
    return sum(float(x[4]) for x in k[-MA:]) / MA


def mark(c: BinanceClient) -> float:
    return float(c._req("GET", "/fapi/v1/premiumIndex", {"symbol": SYMBOL})["markPrice"])


def cancel_algo(c: BinanceClient) -> None:
    try:
        c._req("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": SYMBOL}, signed=True)
    except BinanceError as exc:
        log.info("조건 주문 취소 응답: %s", exc)


def new_plan(c: BinanceClient) -> dict:
    f = filters_of(next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL))
    m = sma5m(c)
    eq = c.equity()
    prices = [round_tick(m + BAND - 2 * BAND * i / (PARTS - 1), f["tick"]) for i in range(PARTS)]
    per = eq * LEVERAGE / PARTS
    levels = [{"price": p, "qty": round_step(per / p, f["step"]), "cid": f"ma50l_{i + 1}_{int(time.time())}",
               "status": "wait"} for i, p in enumerate(prices)]
    st = {"start": int(time.time() * 1000), "ma": m, "equity": eq, "levels": levels, "held": 0.0,
          "avg": 0.0, "tp": 0.0, "sl": 0.0, "tick": f["tick"], "had_pos": False}
    save(st)
    log.info("시작: 5분봉 %d선 %.3f, 평가금액 %.2f x %g배 → %d분할 매수 %s", MA, m, eq, LEVERAGE, PARTS,
             " / ".join(f"{lv['price']:.2f} x {lv['qty']:g}" for lv in levels))
    return st


def offsets() -> list[float]:
    return [BAND - 2 * BAND * i / (PARTS - 1) for i in range(PARTS)]


def follow(c: BinanceClient, st: dict, mp: float) -> None:
    """2026-11-11 사용자 요청: 아직 안 산 차수의 매수 가격을 지금 5분봉 50선(진행 중 봉 포함) ±0.5 로 계속 옮김.
    걸려 있는 주문은 MOVE_MIN 이상 달라지면 취소하고 새 가격에 다시 걺, 일부 체결된 주문은 옮기지 않음."""
    m = sma5m(c)
    st["ma"] = m
    for lv, off in zip(st["levels"], offsets()):
        if lv["status"] == "filled":
            continue
        target = round_tick(m + off, st["tick"])
        if lv["status"] == "open":
            if abs(target - lv["price"]) < MOVE_MIN:
                continue
            o = c.query_order(SYMBOL, lv["cid"])
            if o.get("status") == "FILLED" or float(o.get("executedQty") or 0) > 0:
                if o.get("status") == "FILLED":
                    lv["status"] = "filled"
                    save(st)
                continue
            try:
                c.cancel_order(SYMBOL, lv["cid"])
            except BinanceError as exc:
                log.info("매수 %.2f 취소 실패 (%s) → 다음에 다시 확인", lv["price"], exc)
                continue
            log.info("50선 %.3f → 매수 %.2f 취소, %.2f 로 옮김", m, lv["price"], target)
            lv["status"] = "wait"
        lv["price"] = target
        if target < mp * LIMIT_ROOM:
            continue  # 지정가 허용 범위 밖: 가격이 더 내려오면 냄
        lv["cid"] = f"ma50l_{st['levels'].index(lv) + 1}_{int(time.time() * 1000) % 10**9}"
        try:
            c.order(symbol=SYMBOL, side="BUY", type="LIMIT", timeInForce="GTC", price=fmt(target),
                    quantity=fmt(lv["qty"]), newClientOrderId=lv["cid"], newOrderRespType="RESULT")
            lv["status"] = "open"
            log.info("매수 주문 %.2f x %g 걸어 둠 (50선 %.3f, 현재가 %.2f)", target, lv["qty"], m, mp)
        except BinanceError as exc:
            log.warning("매수 주문 %.2f 실패: %s", target, exc)
    save(st)


def reset_exits(c: BinanceClient, st: dict, amt: float, avg: float) -> None:
    st["held"], st["avg"], st["had_pos"] = amt, avg, True
    st["tp"] = round_tick(avg * (1 + TP_PCT / 100), st["tick"])
    st["sl"] = round_tick(avg * (1 - SL_PCT / 100), st["tick"])
    save(st)
    cancel_algo(c)
    log.info("보유 %g, 평균 진입가 %.4f → 익절 %.2f (+%g%%) · 손절 %.2f (-%g%%)", amt, avg, st["tp"], TP_PCT,
             st["sl"], SL_PCT)
    for kind, px in (("STOP_MARKET", st["sl"]), ("TAKE_PROFIT_MARKET", st["tp"])):
        try:
            c.stop_market(SYMBOL, "SELL", px, kind=kind)
            log.info("거래소 %s %.2f 걸어 둠 (전량)", "손절" if kind == "STOP_MARKET" else "익절", px)
        except Exception as exc:  # noqa: BLE001
            log.warning("거래소 %s 실패 (%s) → 이 창이 가격 보고 처리", kind, exc)


def finish(c: BinanceClient, st: dict) -> None:
    c.cancel_all(SYMBOL)
    rows = c.fills(SYMBOL, start_ms=st["start"] - 5000)
    buys = [t for t in rows if t["side"] == "BUY"]
    sells = [t for t in rows if t["side"] == "SELL"]
    bq = sum(float(t["qty"]) for t in buys)
    sq = sum(float(t["qty"]) for t in sells)
    bp = sum(float(t["price"]) * float(t["qty"]) for t in buys) / bq if bq else 0.0
    sp = sum(float(t["price"]) * float(t["qty"]) for t in sells) / sq if sq else 0.0
    fee = sum(float(t.get("commission") or 0) for t in rows)
    pnl = sq * sp - bq * bp - fee
    reason = "tp" if sp and sp >= st["tp"] * 0.998 else "sl" if sp and sp <= st["sl"] * 1.002 else "close"
    new = not TRADES.exists()
    with open(TRADES, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "entry", "exit", "qty", "pnl", "reason"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"{bp:.4f}", f"{sp:.4f}", f"{bq:g}",
                    f"{pnl:.4f}", reason])
    log.info("끝 (%s): 매수 평균 %.4f x %g → 매도 %.4f, 손익 %+.4f USDT (수수료 포함)", reason, bp, bq, sp, pnl)
    STATE.unlink(missing_ok=True)


def run(c: BinanceClient) -> None:
    if STATE.exists():
        st = json.loads(STATE.read_text(encoding="utf-8"))
        log.info("이어서 감시: 매수 %s", " / ".join(f"{lv['price']:.2f}({lv['status']})" for lv in st["levels"]))
    else:
        amt, _ = c.position_amt(SYMBOL)
        if amt != 0:
            raise SystemExit(f"이미 SOXL 포지션 {amt} 이 있어 시작 안 함")
        st = new_plan(c)
    while True:
        try:
            mp = mark(c)
            follow(c, st, mp)
            amt, avg = c.position_amt(SYMBOL)
            if amt > 0 and abs(amt - st["held"]) > 1e-9:
                reset_exits(c, st, amt, avg)
            elif amt <= 0 and st["had_pos"]:
                log.info("포지션 0 (익절·손절·앱에서 정리) → 남은 매수 주문 취소하고 끝")
                time.sleep(2)
                finish(c, st)
                return
            elif amt > 0 and (mp <= st["sl"] * 0.997 or mp >= st["tp"] * 1.003):  # 거래소 주문이 안 걸렸을 때 대비
                c.cancel_all(SYMBOL)
                c.order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=fmt(amt), reduceOnly="true",
                        newClientOrderId=f"ma50l_x_{int(time.time())}")
                log.info("현재가 %.4f → 직접 시장가 정리", mp)
        except Exception as exc:  # noqa: BLE001
            log.warning("확인 실패, 다시: %s", exc)
        time.sleep(10)


if __name__ == "__main__":
    client = setup()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "run":
        run(client)
    elif cmd == "cancel":
        for o in client.open_orders(SYMBOL):
            if o.get("side") == "BUY":
                client.cancel_order(SYMBOL, o["clientOrderId"])
        print("매수 주문 취소. 감시 창은 직접 닫아 주세요 (보유분이 있으면 익절·손절 주문은 남음).")
    else:
        print("5분봉 50선", round(sma5m(client), 3), "현재가(mark)", mark(client), "평가금액", client.equity(),
              "포지션", client.position_amt(SYMBOL))
        if STATE.exists():
            print(STATE.read_text(encoding="utf-8"))
        for o in client.open_orders(SYMBOL):
            print(o.get("clientOrderId"), o.get("side"), o.get("type"), o.get("price"), o.get("origQty"))
        print(client._req("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL}, signed=True))
