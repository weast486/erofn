r"""SOXL 1시간봉 20선 이탈 숏 (2026-11-10 사용자 요청): 끝난 1시간봉 종가가 단순 20선 아래로 내려가면
(직전 봉은 20선 이상) 선물 계좌 평가금액 x 2배를 시장가 숏 → 거래소 손절(진입가 +5%)·익절(진입가 -10%)을
전량 조건 주문(closePosition)으로 걸어 둠. 한 번 매매로 끝.
미국 정규장이 끝나면(동부 16:00, 사용자 요청) 숏은 시장가로 정리하고, 아직 진입 전이면 감시를 끝냄.

  python scripts/soxl_ma20_short.py run     # 감시 (windows\soxl_ma20_short.bat), 다시 켜면 이어서
  python scripts/soxl_ma20_short.py check   # 지금 20선·포지션·주문만 보여 줌 (주문 없음)

키는 .env.binance_split. 같은 계정·같은 종목이라 스플릿 봇은 꺼 둬야 함.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tossbot.config import load_dotenv  # noqa: E402

from binancebot.client import BinanceClient, filters_of, fmt, round_step, round_tick  # noqa: E402

SYMBOL, LEVERAGE, MA, STOP_PCT, TP_PCT = "SOXLUSDT", 2.0, 20, 5.0, 10.0
TP_MA = 50  # 2026-11-10 사용자 요청: 가격이 1시간봉 50선에 닿으면 바로 익절
STOP_MA = 200  # 2026-11-10 사용자 요청: 끝난 1시간봉 종가가 200선 위면 손절 (+5% 거래소 손절 대신)
DIR = ROOT / "state_binance_split"
STATE, TRADES = DIR / "ma20_short.json", DIR / "ma20_short_trades.csv"
log = logging.getLogger("ma20short")
ET = ZoneInfo("America/New_York")


def next_close_ms() -> int:
    """다음 미국 정규장 마감(동부 16:00, 주말이면 다음 평일) 시각."""
    now = datetime.now(ET)
    d = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= d:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return int(d.timestamp() * 1000)


def setup() -> BinanceClient:
    load_dotenv(str(ROOT / ".env.binance_split"))
    DIR.mkdir(exist_ok=True)
    f = logging.Formatter("%(asctime)s %(message)s")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(DIR / "ma20_short.log", encoding="utf-8")):
        h.setFormatter(f)
        log.addHandler(h)
    return BinanceClient(os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"])


def save(st: dict) -> None:
    STATE.write_text(json.dumps(st), encoding="utf-8")


def closed_bars(c: BinanceClient) -> list[tuple[int, float]]:
    """끝난 1시간봉 (시작 ms, 종가) 목록, 마지막 진행 중 봉은 뺌."""
    k = c._req("GET", "/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1h", "limit": MA + 5})
    now = int(time.time() * 1000)
    return [(int(x[0]), float(x[4])) for x in k if int(x[6]) < now]


def ma_now(c: BinanceClient, n: int = TP_MA) -> float:
    """차트에 보이는 지금 1시간봉 n선 (진행 중 봉은 현재가로)."""
    k = c._req("GET", "/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1h", "limit": n})
    return sum(float(x[4]) for x in k[-n:]) / n


def above_stop_ma(c: BinanceClient) -> tuple[int, float, float]:
    """마지막 끝난 1시간봉 (시작 ms, 종가, 200선)."""
    k = c._req("GET", "/fapi/v1/klines", {"symbol": SYMBOL, "interval": "1h", "limit": STOP_MA + 2})
    now = int(time.time() * 1000)
    done = [x for x in k if int(x[6]) < now][-STOP_MA:]
    return int(done[-1][0]), float(done[-1][4]), sum(float(x[4]) for x in done) / len(done)


def signal(bars) -> tuple[bool, int, float, float]:
    closes = [b[1] for b in bars]
    sma = sum(closes[-MA:]) / MA
    prev_sma = sum(closes[-MA - 1:-1]) / MA
    return closes[-1] < sma and closes[-2] >= prev_sma, bars[-1][0], closes[-1], sma


def enter(c: BinanceClient, st: dict) -> None:
    f = filters_of(next(s for s in c.exchange_info()["symbols"] if s["symbol"] == SYMBOL))
    p = c.prices()[SYMBOL]
    eq = c.equity()
    qty = round_step(eq * LEVERAGE / p, f["step"])
    st["start"] = int(time.time() * 1000)
    c.order(symbol=SYMBOL, side="SELL", type="MARKET", quantity=fmt(qty),
            newClientOrderId=f"ma20s_e_{int(time.time())}", newOrderRespType="RESULT")
    time.sleep(1)
    rows = [t for t in c.fills(SYMBOL, start_ms=st["start"] - 5000) if t["side"] == "SELL"]
    q = sum(float(t["qty"]) for t in rows)
    entry = sum(float(t["price"]) * float(t["qty"]) for t in rows) / q if q else p
    st.update(entered=True, entry=entry, qty=q or qty, equity=eq,
              stop=round_tick(entry * (1 + STOP_PCT / 100), f["tick"]),
              tp=round_tick(entry * (1 - TP_PCT / 100), f["tick"]))
    save(st)
    log.info("숏 진입 %.4f x %g (평가금액 %.2f x %g배)", entry, st["qty"], eq, LEVERAGE)
    for kind, px in (("STOP_MARKET", st["stop"]), ("TAKE_PROFIT_MARKET", st["tp"])):
        try:
            c.stop_market(SYMBOL, "BUY", px, kind=kind)
            log.info("거래소 %s %.2f 걸어 둠 (전량)", "손절" if kind == "STOP_MARKET" else "익절", px)
        except Exception as exc:  # noqa: BLE001
            log.warning("거래소 %s 실패 (%s) → 이 창이 가격 보고 처리", kind, exc)


def finish(c: BinanceClient, st: dict) -> None:
    c.cancel_all(SYMBOL)
    rows = [t for t in c.fills(SYMBOL, start_ms=st["start"] - 5000)]
    buys = [t for t in rows if t["side"] == "BUY"]
    bq = sum(float(t["qty"]) for t in buys)
    bp = sum(float(t["price"]) * float(t["qty"]) for t in buys) / bq if bq else 0.0
    fee = sum(float(t.get("commission") or 0) for t in rows)
    pnl = st["qty"] * st["entry"] - bq * bp - fee
    reason = st.get("reason") or ("time" if time.time() * 1000 >= st.get("deadline", 1e18) else "stop" if bp >= st["stop"] * 0.998 else "tp" if bp and bp <= st["tp"] * 1.002 else "close")
    new = not TRADES.exists()
    with open(TRADES, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["time", "entry", "exit", "qty", "pnl", "reason"])
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), f"{st['entry']:.4f}", f"{bp:.4f}",
                    f"{st['qty']:g}", f"{pnl:.4f}", reason])
    log.info("정리 끝 (%s): 숏 %.4f → 되사기 %.4f x %g, 손익 %+.4f USDT (수수료 포함)", reason, st["entry"], bp, bq, pnl)
    STATE.unlink(missing_ok=True)


def run(c: BinanceClient) -> None:
    st = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"entered": False, "last_bar": 0}
    if not st.get("deadline"):
        st["deadline"] = next_close_ms()
        save(st)
    log.info("미국 정규장 마감 %s (한국 시각) 에 정리·종료",
             datetime.fromtimestamp(st["deadline"] / 1000).strftime("%m-%d %H:%M"))
    if not st["entered"]:
        amt, _ = c.position_amt(SYMBOL)
        if amt != 0:
            raise SystemExit(f"이미 포지션 {amt} 이 있어 시작 안 함")
        log.info("감시 시작: 1시간봉 종가가 %d선 아래로 내려가면 평가금액 x %g배 숏, 손절 +%g%% · 익절 -%g%%",
                 MA, LEVERAGE, STOP_PCT, TP_PCT)
    if st["entered"] and st.get("stop_mode") != "ma200":
        c.cancel_all(SYMBOL)
        c.stop_market(SYMBOL, "BUY", st["tp"], kind="TAKE_PROFIT_MARKET")
        st["stop_mode"] = "ma200"
        save(st)
        log.info("거래소 +%g%% 손절 취소, 익절 %.2f 만 다시 걸어 둠 → 손절은 1시간봉 종가가 %d선 위일 때",
                 STOP_PCT, st["tp"], STOP_MA)
    while True:
        try:
            if time.time() * 1000 >= st["deadline"]:
                if not st["entered"]:
                    log.info("미국 정규장 마감 → 진입 없이 감시 끝")
                    STATE.unlink(missing_ok=True)
                    return
                amt, _ = c.position_amt(SYMBOL)
                c.cancel_all(SYMBOL)
                if amt < 0:
                    c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(-amt), reduceOnly="true",
                            newClientOrderId=f"ma20s_t_{int(time.time())}")
                    log.info("미국 정규장 마감 → 숏 %g 시장가 정리", -amt)
                    time.sleep(2)
                finish(c, st)
                return
            if not st["entered"]:
                hit, bar, close, sma = signal(closed_bars(c))
                if bar != st.get("last_bar"):
                    st["last_bar"] = bar
                    save(st)
                    log.info("%s 봉 종가 %.2f / 20선 %.3f → %s",
                             datetime.fromtimestamp(bar / 1000).strftime("%m-%d %H:%M"), close, sma,
                             "이탈! 숏 진입" if hit else "대기")
                    if hit:
                        enter(c, st)
            else:
                amt, _ = c.position_amt(SYMBOL)
                if amt >= 0:
                    log.info("포지션 0 (손절·익절·앱에서 정리) → 남은 주문 취소하고 끝")
                    finish(c, st)
                    return
                bar, close, m200 = above_stop_ma(c)
                if bar != st.get("stop_bar"):
                    st["stop_bar"] = bar
                    save(st)
                    log.info("%s 봉 종가 %.2f / %d선 %.3f → %s", datetime.fromtimestamp(bar / 1000).strftime("%m-%d %H:%M"),
                             close, STOP_MA, m200, "위! 손절" if close > m200 else "보유")
                    if close > m200:
                        c.cancel_all(SYMBOL)
                        c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(-amt), reduceOnly="true",
                                newClientOrderId=f"ma20s_s_{int(time.time())}")
                        st["reason"] = "ma200"
                        save(st)
                        time.sleep(2)
                        finish(c, st)
                        return
                p = c.prices()[SYMBOL]
                m50 = ma_now(c)
                if p <= m50:
                    c.cancel_all(SYMBOL)
                    c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(-amt), reduceOnly="true",
                            newClientOrderId=f"ma20s_m_{int(time.time())}")
                    st["reason"] = "ma50"
                    save(st)
                    log.info("현재가 %.4f 가 %d선 %.4f 에 닿음 → 시장가 익절", p, TP_MA, m50)
                    time.sleep(2)
                    finish(c, st)
                    return
                if (st.get("stop_mode") != "ma200" and p >= st["stop"] * 1.003) or p <= st["tp"] * 0.997:  # 거래소 주문이 안 걸렸을 때 대비
                    c.cancel_all(SYMBOL)
                    c.order(symbol=SYMBOL, side="BUY", type="MARKET", quantity=fmt(-amt), reduceOnly="true",
                            newClientOrderId=f"ma20s_x_{int(time.time())}")
                    log.info("현재가 %.4f → 직접 시장가 정리", p)
        except Exception as exc:  # noqa: BLE001
            log.warning("확인 실패, 다시: %s", exc)
        time.sleep(15)


if __name__ == "__main__":
    client = setup()
    if (sys.argv[1] if len(sys.argv) > 1 else "check") == "run":
        run(client)
    else:
        hit, bar, close, sma = signal(closed_bars(client))
        print("마지막 끝난 봉", datetime.fromtimestamp(bar / 1000).strftime("%m-%d %H:%M"), "종가", close,
              "20선", round(sma, 3), "신호" if hit else "")
        print("포지션", client.position_amt(SYMBOL), "현재가", client.prices()[SYMBOL], f"{TP_MA}선", round(ma_now(client), 3), f"{STOP_MA}선(끝난 봉)", round(above_stop_ma(client)[2], 3))
        for o in client.open_orders(SYMBOL):
            print(o.get("clientOrderId"), o.get("side"), o.get("type"), o.get("price"), o.get("origQty"))
