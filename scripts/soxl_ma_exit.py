r"""SOXL 한 번 롱: 지금 시장가로 진입 → 끝난 5분봉 종가가 단순 11선 아래면 시장가로 전량 매도 (2026-11-03 사용자 요청).

  python scripts/soxl_ma_exit.py enter   # 잔고 1/4 x 3배 시장가 롱 (이미 들고 있으면 안 삼)
  python scripts/soxl_ma_exit.py watch   # 10초마다 확인, 이탈하면 매도하고 끝 (windows\soxl_ma_exit.bat)
  python scripts/soxl_ma_exit.py check   # 지금 5분봉 11선·종가만 보여 줌 (주문 없음)

스플릿 봇과 같은 계정·같은 종목이라 이 롱이 끝날 때까지 스플릿 봇은 꺼 둬야 함.
키는 .env.binance_split 에서 읽고 화면·로그에 남기지 않음.
"""
from __future__ import annotations

import csv
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tossbot.config import load_dotenv  # noqa: E402

from binancebot.client import BinanceClient, filters_of, fmt, round_step  # noqa: E402

SYMBOL, MA, TF_MS = "SOXLUSDT", 11, 5 * 60_000
FRACTION, LEVERAGE = 0.25, 3.0
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "ma_exit.json", DIR / "ma_exit_trades.csv"
log = logging.getLogger("ma_exit")


def setup() -> BinanceClient:
    import os

    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    fmt_ = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "ma_exit.log", encoding="utf-8")):
        h.setFormatter(fmt_)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def closed_ma(c: BinanceClient) -> tuple[float, float, int]:
    """(마지막 끝난 5분봉 종가, 그 봉까지의 단순 11선, 그 봉 시작 시각 ms)."""
    now = int(time.time() * 1000)
    rows = c.klines(SYMBOL, now - TF_MS * (MA + 3), limit=MA + 5, interval="5m")
    done = [r for r in rows if int(r[6]) < now]  # 끝난 봉만 (close_time 이 지난 것)
    closes = [float(r[4]) for r in done[-MA:]]
    return closes[-1], sum(closes) / len(closes), int(done[-1][0])


def avg_fill(c: BinanceClient, r: dict, fallback: float) -> float:
    for attempt in range(3):
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


def enter(c: BinanceClient) -> None:
    amt, _ = c.position_amt(SYMBOL)
    if amt != 0:
        log.info("이미 포지션 %g 이 있어 새로 사지 않음", amt)
        return
    meta = next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL)
    step = filters_of(meta)["step"]
    eq, p = c.equity(), c.prices()[SYMBOL]
    qty = round_step(eq * FRACTION * LEVERAGE / p, step)
    r = c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(qty),
                newClientOrderId=f"maexit_buy_{int(time.time())}", newOrderRespType="RESULT")
    done = float(r.get("executedQty") or 0)
    px = avg_fill(c, r, p)
    STATE.write_text(json.dumps({"entry": px, "qty": done, "time": datetime.now().isoformat(timespec="seconds")}),
                     encoding="utf-8")
    record("buy", px, done, -px * done * 0.0005)
    log.info("롱 진입 %.4f x %g (%.2f USDT, 잔고 %.2f 의 1/4 x 3배)", px, done, px * done, eq)


def watch(c: BinanceClient) -> None:
    if not STATE.exists():
        log.info("진입 기록이 없어 감시할 것이 없음")
        return
    pos = json.loads(STATE.read_text(encoding="utf-8"))
    log.info("감시 시작: 진입 %.4f x %g, 끝난 5분봉 종가 < 단순 %d선 이면 시장가 전량 매도", pos["entry"], pos["qty"], MA)
    last_bar = None
    while True:
        try:
            amt, _ = c.position_amt(SYMBOL)
            if amt <= 0:
                log.info("포지션이 없음 (거래소 손절 체결이나 앱에서 정리) → 감시 끝")
                STATE.unlink(missing_ok=True)
                return
            close, ma, bar = closed_ma(c)
            if bar != last_bar:
                log.info("5분봉 %s 종가 %.4f / 11선 %.4f", datetime.fromtimestamp(bar / 1000).strftime("%H:%M"), close, ma)
                last_bar = bar
            if close < ma:
                r = c.order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=fmt(amt), reduceOnly="true",
                            newClientOrderId=f"maexit_sell_{int(time.time())}", newOrderRespType="RESULT")
                px = avg_fill(c, r, close)
                q = float(r.get("executedQty") or amt)
                pnl = q * (px - pos["entry"]) - (px + pos["entry"]) * q * 0.0005
                record("sell", px, q, pnl)
                log.info("11선 이탈 (종가 %.4f < %.4f) → 시장가 매도 %.4f x %g, 손익 %+.4f USDT", close, ma, px, q, pnl)
                STATE.unlink(missing_ok=True)
                return
        except Exception as exc:  # noqa: BLE001 네트워크 오류는 다음 확인 때 다시
            log.warning("확인 실패, 10초 뒤 다시: %s", exc)
        time.sleep(10)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    client = setup()
    if cmd == "enter":
        enter(client)
    elif cmd == "watch":
        watch(client)
    else:
        c_, m_, b_ = closed_ma(client)
        print(f"마지막 끝난 5분봉 {datetime.fromtimestamp(b_ / 1000):%H:%M} 종가 {c_:.4f} / 11선 {m_:.4f} / 현재가 {client.prices()[SYMBOL]}")
