r"""SOXL 10분할 스플릿 (2026-11-04 사용자 요청): 기존 보유(앱에서 산 1.5개)는 그대로 두고,
기준가 68.52 아래 -0.5% 간격 10차 지정가 매수(68.18 ~ 65.09), 차수마다 바로 위 차수 가격 +0.1% 에 지정가 익절
(1차는 기준가 68.52 +0.1%), 익절되면 같은 가격에 다시 매수 주문. 배율 5배(격리), 차수당 = 주문가능 금액 x 5 / 10.
손절 없음.

  python scripts/soxl_grid10.py init    # 기존 sp10_ 매수 주문 취소 → 배율 5 → 10차 매수 걸기 (한 번만)
  python scripts/soxl_grid10.py run     # 체결 확인하며 익절·재매수 (windows\soxl_grid10.bat), 다시 켜면 이어서
  python scripts/soxl_grid10.py check   # 차수별 상태만 보여 줌

키는 .env.binance_split. 스플릿 봇과 같은 계정·같은 종목이라 스플릿 봇은 꺼 둬야 함.
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

SYMBOL, LEVERAGE, BASE, GAP, TP_ADD, N = "SOXLUSDT", 5, 68.52, 0.005, 0.001, 10
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "grid10.json", DIR / "grid10_trades.csv"
log = logging.getLogger("grid10")


def setup() -> BinanceClient:
    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    f = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "grid10.log", encoding="utf-8")):
        h.setFormatter(f)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def save(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE)


def record(k: int, event: str, price: float, qty: float, pnl: float) -> None:
    new = not TRADES.exists()
    with open(TRADES, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "level", "event", "price", "qty", "pnl"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), k, event, f"{price:.4f}", f"{qty:.6f}", f"{pnl:.4f}"])


def send(c: BinanceClient, lv: dict) -> None:
    side = "BUY" if lv["side"] == "buy" else "SELL"
    px = lv["buy"] if side == "BUY" else lv["tp"]
    cid = f"g10_{lv['side'][0]}{lv['k']}_{int(time.time() * 1000) % 10**9}"
    extra = {"reduceOnly": "true"} if side == "SELL" else {}
    c.order(symbol=SYMBOL, side=side, type="LIMIT", timeInForce="GTC", price=fmt(px), quantity=fmt(lv["qty"]),
            newClientOrderId=cid, **extra)
    lv["cid"] = cid
    log.info("%d차 %s 지정가 %.2f x %g", lv["k"], "매수" if side == "BUY" else "익절", px, lv["qty"])


def init(c: BinanceClient) -> None:
    if STATE.exists():
        raise SystemExit("이미 시작함 (grid10.json 있음)")
    alive = set()
    for o in c.open_orders(SYMBOL):
        cid = str(o.get("clientOrderId", ""))
        if cid.startswith("sp10_"):
            alive.add(int(cid.split("_")[1][1:]))
            c.cancel_order(SYMBOL, cid)
    time.sleep(1)
    c.set_leverage(SYMBOL, LEVERAGE)
    f = filters_of(next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL))
    avail = float(c.account()["availableBalance"])
    per = avail * LEVERAGE * 0.97 / max(len(alive), 1)  # 아직 안 산 차수끼리 나눔
    levels, prev = [], BASE
    for k in range(1, N + 1):
        buy = round_tick(BASE * (1 - GAP * k), f["tick"])
        levels.append({"k": k, "buy": buy, "tp": round_tick(prev * (1 + TP_ADD), f["tick"]),
                       "qty": round_step(per / buy, f["step"]), "side": "buy", "cid": ""})
        prev = buy
    for lv in levels:  # 앞서 0.10개씩 건 sp10_ 매수 중 이미 체결된 차수 → 그 수량으로 익절부터
        if lv["k"] not in alive:
            lv.update(side="sell", qty=0.10, filled=lv["buy"])
            log.info("%d차는 이미 %.2f x 0.10 체결됨 → 익절 주문부터", lv["k"], lv["buy"])
    st = {"levels": levels, "avail": avail}
    for lv in levels:
        send(c, lv)
    save(st)
    log.info("주문가능 %.2f x %d배 / %d = 차수당 약 %.2f USDT, 손절 없음", avail, LEVERAGE, N, per)


def run(c: BinanceClient) -> None:
    st = json.loads(STATE.read_text(encoding="utf-8"))
    log.info("감시 시작: %s", ", ".join(f"{lv['k']}차 {lv['side']}" for lv in st["levels"]))
    while True:
        for lv in st["levels"]:
            try:
                o = c.query_order(SYMBOL, lv["cid"])
                status = o.get("status")
                if status == "FILLED":
                    px = float(o.get("avgPrice") or 0) or (lv["buy"] if lv["side"] == "buy" else lv["tp"])
                    q = float(o.get("executedQty") or lv["qty"])
                    if lv["side"] == "buy":
                        lv["filled"] = px
                        record(lv["k"], "buy", px, q, 0.0)
                        log.info("%d차 매수 체결 %.4f x %g", lv["k"], px, q)
                        lv["side"] = "sell"
                    else:
                        pnl = q * (px - lv.get("filled", lv["buy"])) - (px + lv.get("filled", lv["buy"])) * q * 0.0002
                        record(lv["k"], "tp", px, q, pnl)
                        log.info("%d차 익절 체결 %.4f x %g, 손익 %+.4f USDT", lv["k"], px, q, pnl)
                        lv["side"] = "buy"
                    send(c, lv)
                    save(st)
                elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                    log.info("%d차 주문 %s → 다시 걺", lv["k"], status)
                    send(c, lv)
                    save(st)
            except BinanceError as exc:
                log.warning("%d차 확인 실패: %s", lv["k"], exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("%d차 확인 실패: %s", lv["k"], exc)
        time.sleep(10)


if __name__ == "__main__":
    client = setup()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "init":
        init(client)
    elif cmd == "run":
        run(client)
    else:
        print("포지션", client.position_amt(SYMBOL), "현재가", client.prices()[SYMBOL])
        for lv in json.loads(STATE.read_text(encoding="utf-8"))["levels"]:
            print(lv["k"], lv["side"], "매수", lv["buy"], "익절", lv["tp"], lv["qty"])
