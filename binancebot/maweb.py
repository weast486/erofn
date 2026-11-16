"""이평선 매매 웹 프로그램 (바이낸스 선물 시세, 키 필요 없음·주문 없음).

    python -m binancebot.maweb             # http://localhost:8790 을 브라우저로 열기
    python -m binancebot.maweb --port 8790 --no-open
    python -m binancebot.maweb --lan       # 같은 와이파이의 폰에서도 열기

종목: BTC·ETH + 바이낸스 미국 주식 토큰(EQUITY), 봉: 1분·5분·15분·1시간·4시간·일봉,
이평선: 단순(MA)·지수(EMA)·거래량가중(VWMA) 3개까지.
매매 규칙: 빠른 선이 느린 선을 위로 넘으면 롱 / 아래로 넘으면 청산(또는 숏).
끝난 봉에서 교차를 확인하고 다음 봉 시가에 체결한 것으로 계산(모의 매매). 손절·익절 % 선택.
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
KINDS = ("sma", "ema", "vwma")
MAX_BARS = 5000
HTML = Path(__file__).with_name("maweb.html")


# ------------------------------------------------------------ 이평선
def moving_average(kind: str, n: int, close: list[float], volume: list[float] | None = None) -> list[float | None]:
    """봉마다 이평선 값 (앞쪽 n-1 봉은 None)."""
    out: list[float | None] = [None] * len(close)
    if n < 1 or len(close) < n:
        return out
    if kind == "sma":
        s = sum(close[:n])
        out[n - 1] = s / n
        for i in range(n, len(close)):
            s += close[i] - close[i - n]
            out[i] = s / n
    elif kind == "ema":
        k = 2 / (n + 1)
        e = sum(close[:n]) / n  # 처음 값은 단순 평균으로 시작
        out[n - 1] = e
        for i in range(n, len(close)):
            e = close[i] * k + e * (1 - k)
            out[i] = e
    elif kind == "vwma":
        vol = volume or [1.0] * len(close)
        pv = sum(c * v for c, v in zip(close[:n], vol[:n]))
        vv = sum(vol[:n])
        out[n - 1] = pv / vv if vv > 0 else close[n - 1]
        for i in range(n, len(close)):
            pv += close[i] * vol[i] - close[i - n] * vol[i - n]
            vv += vol[i] - vol[i - n]
            out[i] = pv / vv if vv > 1e-12 else close[i]
    else:
        raise ValueError(f"이평선 종류 {kind}")
    return out


def parse_ma(text: str) -> tuple[str, int] | None:
    """'ema:20' → ('ema', 20). 빈 값·'off' 는 None."""
    if not text or text == "off":
        return None
    kind, _, n = text.partition(":")
    kind = kind.lower()
    if kind == "ma":
        kind = "sma"
    if kind not in KINDS:
        raise ValueError(f"이평선 종류는 ma/ema/vwma: {text}")
    n = int(n)
    if not 1 <= n <= 1000:
        raise ValueError("이평선 기간은 1~1000")
    return kind, n


# ------------------------------------------------------------ 모의 매매 (교차)
def backtest(bars: list[dict], fast: list, slow: list, side: str = "long", stop_pct: float = 0.0,
             tp_pct: float = 0.0, fee_pct: float = 0.05, leverage: float = 1.0) -> dict:
    """빠른 선(fast)이 느린 선(slow)을 넘는 교차 매매.

    side: long(골든크로스 롱·데드크로스 청산) / short(데드크로스 숏·골든크로스 청산) / both(교차마다 뒤집기).
    교차는 i 번째 봉이 끝났을 때 확인 → i+1 번째 봉 시가에 체결. 손절·익절은 봉의 고가·저가로 판정
    (같은 봉에서 둘 다 닿으면 손절). 수수료는 한쪽 fee_pct %. 손익 % 는 배율 반영 전(가격 기준).
    마지막 봉은 아직 안 끝난 봉일 수 있어 신호 확인에 쓰지 않는다.
    """
    trades: list[dict] = []
    pos = None  # {"dir": 1/-1, "entry": 가격, "t": 시각, "i": 봉}
    fee = fee_pct / 100
    n = len(bars)
    last_closed = n - 2  # n-1 은 진행 중인 봉

    def close_pos(i: int, price: float, reason: str) -> None:
        nonlocal pos
        d = pos["dir"]
        ret = d * (price / pos["entry"] - 1) - 2 * fee
        trades.append({"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                       "exit_time": bars[i]["t"], "exit": price, "reason": reason,
                       "ret_pct": ret * 100, "bars": i - pos["i"]})
        pos = None

    def open_pos(i: int, d: int) -> None:
        nonlocal pos
        pos = {"dir": d, "entry": bars[i]["o"], "t": bars[i]["t"], "i": i}

    pending = None  # 다음 봉 시가에 할 일: ("open", dir) / ("close",) / ("flip", dir)
    for i in range(1, n):
        b = bars[i]
        # 1) 앞 봉에서 나온 신호를 이 봉 시가에 체결
        if pending:
            act = pending
            pending = None
            if act[0] in ("close", "flip") and pos:
                close_pos(i, b["o"], "cross")
            if act[0] in ("open", "flip") and pos is None:
                open_pos(i, act[1])
        # 2) 손절·익절 (진입한 봉부터)
        if pos:
            d, e = pos["dir"], pos["entry"]
            sl = e * (1 - d * stop_pct / 100) if stop_pct > 0 else None
            tp = e * (1 + d * tp_pct / 100) if tp_pct > 0 else None
            hit_sl = sl is not None and (b["l"] <= sl if d > 0 else b["h"] >= sl)
            hit_tp = tp is not None and (b["h"] >= tp if d > 0 else b["l"] <= tp)
            if hit_sl:
                # 시가가 이미 손절가 너머면 시가로
                px = min(b["o"], sl) if d > 0 else max(b["o"], sl)
                close_pos(i, px, "stop")
            elif hit_tp:
                px = max(b["o"], tp) if d > 0 else min(b["o"], tp)
                close_pos(i, px, "take")
        # 3) 이 봉이 끝났을 때 교차 확인 → 다음 봉에서 체결
        if i > last_closed:
            continue
        f0, s0, f1, s1 = fast[i - 1], slow[i - 1], fast[i], slow[i]
        if None in (f0, s0, f1, s1):
            continue
        up = f0 <= s0 and f1 > s1
        down = f0 >= s0 and f1 < s1
        if not (up or down):
            continue
        d = 1 if up else -1
        if side == "long":
            pending = ("open", 1) if up and pos is None else ("close",) if down and pos and pos["dir"] > 0 else None
        elif side == "short":
            pending = ("open", -1) if down and pos is None else ("close",) if up and pos and pos["dir"] < 0 else None
        else:  # both
            if pos is None:
                pending = ("open", d)
            elif pos["dir"] != d:
                pending = ("flip", d)

    open_pos_info = None
    if pos:
        last = bars[-1]["c"]
        d = pos["dir"]
        open_pos_info = {"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                         "price": last, "ret_pct": (d * (last / pos["entry"] - 1) - fee) * 100}
    pending_info = None
    if pending:
        pending_info = {"close": "청산", "open": "진입", "flip": "뒤집기"}[pending[0]]
        if len(pending) > 1:
            pending_info += " (롱)" if pending[1] > 0 else " (숏)"
    return {"trades": trades, "open": open_pos_info, "pending": pending_info,
            "stats": stats(trades, leverage)}


def stats(trades: list[dict], leverage: float = 1.0) -> dict:
    eq, peak, mdd = 1.0, 1.0, 0.0
    for t in trades:
        eq *= max(0.0, 1 + leverage * t["ret_pct"] / 100)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    wins = [t for t in trades if t["ret_pct"] > 0]
    n = len(trades)
    return {"n": n, "win_rate": len(wins) / n * 100 if n else 0.0,
            "avg_pct": sum(t["ret_pct"] for t in trades) / n if n else 0.0,
            "total_pct": (eq - 1) * 100, "mdd_pct": mdd * 100, "leverage": leverage}


# ------------------------------------------------------------ 모의 매매 (눌림)
def pullback(bars: list[dict], line: list, trend_line: list | None = None, stop_line: list | None = None,
             tp_line: list | None = None, tp_mode: str = "pct",
             side: str = "long", min_above: int = 5, trend: str = "none", slope_bars: int = 5,
             entry: str = "touch", confirm_bars: int = 5, stop_mode: str = "pct", stop_pct: float = 2.0,
             tp_pct: float = 0.0, max_hold: int = 0, fee_pct: float = 0.05, leverage: float = 1.0) -> dict:
    """이평선 눌림 매매.

    롱: 직전 min_above 봉의 종가가 모두 눌림선 위 + 추세 조건 → 가격이 눌림선까지 내려오면 매수.
      entry=touch   : 직전 봉까지 끝난 눌림선 값에 지정가 (시가가 이미 아래면 시가)
      entry=confirm : 닿은 뒤 confirm_bars 봉 안에 종가가 눌림선 위로 마감하면 다음 봉 시가
    trend: none / above(눌림선이 추세선 위) / rising(눌림선이 slope_bars 봉 전보다 높음) / both
    손절: stop_mode=pct → 진입가 -stop_pct% (봉 저가로 판정) /
          line → 종가가 손절선(기본 눌림선) x (1-stop_pct%) 아래로 마감하면 다음 봉 시가 /
          touch → 직전 봉 종가가 손절선 x (1-stop_pct%) 위에 있다가 가격이 닿으면 그 가격
                  (선 값은 직전 끝난 봉 값, 시가가 이미 아래면 시가)
    익절: tp_mode=pct → tp_pct% 지정가 / touch → 가격이 익절선에 닿으면 그 가격 (선이 진입가보다 이익 쪽에 있을 때만),
    max_hold 봉 지나면 그 봉 종가에 정리 (0 = 없음). 숏은 모두 반대. max_hold 봉 지나면 그 봉 종가에 정리 (0 = 없음). 숏은 모두 반대.
    진입한 봉에서는 손절만 확인 (익절은 다음 봉부터, 봉 안 순서를 모르므로 보수적으로).
    """
    n = len(bars)
    last_closed = n - 2
    fee = fee_pct / 100
    stop_line = stop_line or line
    dirs = {"long": [1], "short": [-1], "both": [1, -1]}[side]
    trades: list[dict] = []
    pos = None
    pend_close = False
    pend_open = None   # confirm 진입: 다음 봉 시가에 열 방향
    armed: dict[int, int] = {}  # confirm: 방향 → 닿은 봉 번호

    def ok_setup(k: int, d: int) -> bool:
        """k 번째 봉이 끝난 시점에서 d 방향 눌림 대기 조건."""
        if k - min_above + 1 < 0 or line[k] is None:
            return False
        for j in range(k - min_above + 1, k + 1):
            if line[j] is None or d * (bars[j]["c"] - line[j]) <= 0:
                return False
        if trend in ("above", "both"):
            if trend_line is None or trend_line[k] is None or d * (line[k] - trend_line[k]) <= 0:
                return False
        if trend in ("rising", "both"):
            if k - slope_bars < 0 or line[k - slope_bars] is None or d * (line[k] - line[k - slope_bars]) <= 0:
                return False
        return True

    def close_pos(i: int, price: float, reason: str) -> None:
        nonlocal pos
        d = pos["dir"]
        trades.append({"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                       "exit_time": bars[i]["t"], "exit": price, "reason": reason,
                       "ret_pct": (d * (price / pos["entry"] - 1) - 2 * fee) * 100, "bars": i - pos["i"]})
        pos = None

    def open_pos(i: int, d: int, price: float) -> None:
        nonlocal pos
        pos = {"dir": d, "entry": price, "t": bars[i]["t"], "i": i}
        if stop_mode == "pct" and stop_pct > 0:
            pos["sl"] = price * (1 - d * stop_pct / 100)
        if tp_mode == "pct" and tp_pct > 0:
            pos["tp"] = price * (1 + d * tp_pct / 100)

    def stop_level(i: int) -> float | None:
        if stop_mode == "touch":
            v = stop_line[i - 1] if i >= 1 else None
            return None if v is None else v * (1 - pos["dir"] * stop_pct / 100)
        return pos.get("sl")

    def tp_level(i: int) -> float | None:
        if tp_mode == "touch":
            v = tp_line[i - 1] if (tp_line is not None and i >= 1) else None
            d = pos["dir"]
            # 선이 진입가보다 이익 쪽이고, 직전 봉 종가가 아직 선에 못 미쳤을 때 (닿으러 가는 중)
            if v is None or d * (v - pos["entry"]) <= 0 or (i < n and d * (v - bars[i - 1]["c"]) <= 0):
                return None
            return v
        return pos.get("tp")

    def pct_stop_hit(b: dict, i: int, entry_bar: bool = False) -> float | None:
        sl = stop_level(i)
        if sl is None:
            return None
        d = pos["dir"]
        if entry_bar and d * (pos["entry"] - sl) <= 0:  # 진입한 봉: 손절가가 진입가보다 불리한 쪽일 때만
            return None
        # 선 닿음 손절: 직전 봉 종가가 손절선 안전한 쪽(롱이면 위)에 있다가 내려와 닿을 때만
        if stop_mode == "touch" and not entry_bar and d * (bars[i - 1]["c"] - sl) <= 0:
            return None
        if (b["l"] <= sl) if d > 0 else (b["h"] >= sl):
            return min(b["o"], sl) if d > 0 else max(b["o"], sl)
        return None

    for i in range(1, n):
        b = bars[i]
        exited = False
        # 1) 앞 봉에서 정한 것을 시가에 체결
        if pend_close and pos:
            close_pos(i, b["o"], "line")
            exited = True
        pend_close = False
        if pend_open is not None and pos is None:
            open_pos(i, pend_open, b["o"])
            entered_now = True
        else:
            entered_now = False
        pend_open = None
        # 2) 보유 중: 손절·익절
        if pos and not entered_now:
            px = pct_stop_hit(b, i)
            tp = tp_level(i) if px is None else None
            if px is not None:
                close_pos(i, px, "line" if stop_mode == "touch" else "stop")
                exited = True
            elif tp is not None:
                d = pos["dir"]
                if (b["h"] >= tp) if d > 0 else (b["l"] <= tp):
                    close_pos(i, max(b["o"], tp) if d > 0 else min(b["o"], tp), "take")
                    exited = True
        # 3) 진입 (touch: 지정가는 진행 중인 봉에서도 체결될 수 있음)
        if pos is None and not exited and entry == "touch":
            for d in dirs:
                if not ok_setup(i - 1, d):
                    continue
                lv = line[i - 1]
                if (b["l"] <= lv) if d > 0 else (b["h"] >= lv):
                    open_pos(i, d, min(b["o"], lv) if d > 0 else max(b["o"], lv))
                    entered_now = True
                    px = pct_stop_hit(b, i, entry_bar=True)
                    if px is not None:
                        close_pos(i, px, "stop")
                    break
        if i > last_closed:
            continue
        # 4) 봉이 끝난 뒤 판단: 손절선 이탈·보유 기간·confirm 진입
        if pos:
            d = pos["dir"]
            if stop_mode == "line" and stop_line[i] is not None:
                lim = stop_line[i] * (1 - d * stop_pct / 100)
                if d * (b["c"] - lim) < 0:
                    pend_close = True
            if not pend_close and max_hold > 0 and i - pos["i"] + 1 >= max_hold:
                close_pos(i, b["c"], "time")
            continue
        if entry == "confirm" and not exited:
            for d in dirs:
                lv = line[i - 1]
                if ok_setup(i - 1, d) and lv is not None and ((b["l"] <= lv) if d > 0 else (b["h"] >= lv)):
                    armed.setdefault(d, i)
                if d in armed:
                    if i - armed[d] >= confirm_bars:
                        del armed[d]
                    elif line[i] is not None and d * (b["c"] - line[i]) > 0:  # 닿은 봉이 선 위로 마감해도 인정
                        pend_open = d
                        armed.clear()
                        break

    open_info = None
    if pos:
        last = bars[-1]["c"]
        d = pos["dir"]
        open_info = {"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                     "price": last, "ret_pct": (d * (last / pos["entry"] - 1) - fee) * 100,
                     "sl": stop_level(n) if stop_mode != "line" else None, "tp": tp_level(n)}
    pending = None
    if pend_close:
        pending = "다음 봉 시가에 청산 (손절선 이탈)"
    elif pend_open is not None:
        pending = f"다음 봉 시가에 {'롱' if pend_open > 0 else '숏'} 진입 (눌림 확인)"
    elif pos is None and n >= 2:
        waits = []
        for d in dirs:
            if entry == "touch" and ok_setup(n - 2, d):
                waits.append(f"{'롱' if d > 0 else '숏'} 지정가 대기 {line[n - 2]:.6g}")
            elif entry == "confirm" and d in armed:
                waits.append(f"{'롱' if d > 0 else '숏'}: 눌림선 닿음, 종가가 선 {'위' if d > 0 else '아래'}로 마감하면 진입")
        pending = " / ".join(waits) or None
    return {"trades": trades, "open": open_info, "pending": pending, "stats": stats(trades, leverage)}


# ------------------------------------------------------------ 바이낸스 시세
class Market:
    """공개 시세만 사용 (키 없음). 종목 목록은 10분 캐시."""

    def __init__(self, client=None):
        if client is None:
            from .client import BinanceClient
            client = BinanceClient()
        self.c = client
        self._sym = (0.0, [])
        self._lock = threading.Lock()

    def symbols(self) -> list[dict]:
        with self._lock:
            if time.time() - self._sym[0] < 600 and self._sym[1]:
                return self._sym[1]
            info = self.c.exchange_info()
            vol = {r["symbol"]: float(r.get("quoteVolume", 0)) for r in self.c._req("GET", "/fapi/v1/ticker/24hr")}
            out = []
            for s in info["symbols"]:
                sym = s["symbol"]
                if s.get("status") != "TRADING" or "PERPETUAL" not in s.get("contractType", ""):
                    # 코인은 PERPETUAL, 주식 토큰은 TRADIFI_PERPETUAL
                    continue
                if sym in ("BTCUSDT", "ETHUSDT"):
                    group = "코인"
                elif s.get("underlyingType") == "EQUITY" and s.get("quoteAsset") == "USDT":
                    group = "미국 주식"
                else:
                    continue
                out.append({"symbol": sym, "group": group, "name": s.get("baseAsset", sym), "qv": vol.get(sym, 0.0)})
            out.sort(key=lambda r: (r["group"] != "코인", -r["qv"]))
            self._sym = (time.time(), out)
            return out

    def bars(self, symbol: str, interval: str, count: int) -> list[dict]:
        """최근 count 개 봉 (마지막은 진행 중인 봉). 1500개씩 뒤로 이어 받음."""
        count = max(2, min(MAX_BARS, count))
        rows: list[list] = []
        end = None
        while len(rows) < count:
            params = {"symbol": symbol, "interval": interval, "limit": min(1500, count - len(rows))}
            if end is not None:
                params["endTime"] = end
            chunk = self.c._req("GET", "/fapi/v1/klines", params)
            if not chunk:
                break
            rows = chunk + rows
            end = chunk[0][0] - 1
            if len(chunk) < params["limit"]:
                break
        return [{"t": r[0] // 1000, "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                 "c": float(r[4]), "v": float(r[5])} for r in rows[-count:]]


def run(market: Market, q: dict) -> dict:
    """화면 요청 하나 처리: 봉 + 이평선 + 모의 매매 결과."""
    symbol = q.get("symbol", "BTCUSDT").upper()
    interval = q.get("interval", "1h")
    if interval not in INTERVALS:
        raise ValueError(f"봉은 {', '.join(INTERVALS)} 중 하나")
    count = int(q.get("bars", 1000))
    mas = [parse_ma(q.get(f"ma{k}", "")) for k in (1, 2, 3, 4)]
    bars = market.bars(symbol, interval, count)
    close = [b["c"] for b in bars]
    vol = [b["v"] for b in bars]
    lines = [moving_average(m[0], m[1], close, vol) if m else None for m in mas]
    fast_i, slow_i = int(q.get("fast", 1)) - 1, int(q.get("slow", 2)) - 1
    result = {"symbol": symbol, "interval": interval, "bars": bars, "strat": q.get("strat", "cross"),
              "lines": [{"kind": m[0], "n": m[1], "values": v} if m else None for m, v in zip(mas, lines)]}
    num = lambda k, dflt: float(q.get(k, dflt) or 0)  # noqa: E731
    if result["strat"] == "pullback":
        pick = lambda k, dflt: lines[int(q.get(k, dflt)) - 1]  # noqa: E731
        line = pick("pb_line", 1)
        trend = q.get("trend", "none")
        if line is None:
            raise ValueError("눌림선으로 고른 이평선이 꺼져 있어요")
        if q.get("tp_mode") == "touch" and pick("tp_line", 3) is None:
            raise ValueError("익절선으로 고른 이평선이 꺼져 있어요")
        if trend in ("above", "both") and pick("trend_line", 2) is None:
            raise ValueError("추세선으로 고른 이평선이 꺼져 있어요")
        result["sim"] = pullback(
            bars, line, trend_line=pick("trend_line", 2), stop_line=pick("stop_line", q.get("pb_line", 1)),
            tp_line=pick("tp_line", 3), tp_mode=q.get("tp_mode", "pct"),
            side=q.get("side", "long"), min_above=int(num("min_above", 5)), trend=trend,
            slope_bars=int(num("slope_bars", 5)) or 1, entry=q.get("entry", "touch"),
            confirm_bars=int(num("confirm_bars", 5)) or 1, stop_mode=q.get("stop_mode", "pct"),
            stop_pct=num("sl", 2), tp_pct=num("tp", 0), max_hold=int(num("max_hold", 0)),
            fee_pct=num("fee", 0.05), leverage=num("lev", 1) or 1)
    elif lines[fast_i] is not None and lines[slow_i] is not None and fast_i != slow_i:
        result["sim"] = backtest(bars, lines[fast_i], lines[slow_i], side=q.get("side", "long"),
                                 stop_pct=float(q.get("sl", 0) or 0), tp_pct=float(q.get("tp", 0) or 0),
                                 fee_pct=float(q.get("fee", 0.05) or 0), leverage=float(q.get("lev", 1) or 1))
    else:
        result["sim"] = None
    return result


# ------------------------------------------------------------ 종목 찾기
def near_line(bars: list[dict], kind: str, n: int) -> dict | None:
    """마지막으로 끝난 봉까지의 이평선 값과 지금 가격(진행 중인 봉 종가)의 거리 %."""
    if len(bars) < n + 1:
        return None
    done = bars[:-1]
    ma = moving_average(kind, n, [b["c"] for b in done], [b["v"] for b in done])[-1]
    if not ma:
        return None
    price = bars[-1]["c"]
    return {"ma": ma, "price": price, "dist_pct": (price / ma - 1) * 100}


def atr_pct(bars: list[dict], n: int) -> float | None:
    """끝난 봉 n개의 평균 실제 변동폭(ATR, 단순 평균)을 지금 가격 대비 % 로."""
    done = bars[:-1]
    if len(done) < n + 1 or not bars[-1]["c"]:
        return None
    trs = [max(b["h"], p["c"]) - min(b["l"], p["c"]) for p, b in zip(done[-n - 1:-1], done[-n:])]
    return sum(trs) / n / bars[-1]["c"] * 100


def scan(market: Market, q: dict) -> dict:
    """모든 종목에서 지금 가격이 이평선(기본 거래량가중 100선) ±pct% 안에 있는 종목 찾기.

    tfs: 봉 목록 (기본 5m,15m), mode: all = 모든 봉에서 범위 안 / any = 하나라도.
    atr_tf·atr_n: 평균 ATR 을 잴 봉·개수 (기본 일봉 14개), atr_min: 평균 ATR 이 가격의 이 % 이상인 종목만 (0 = 끔).
    """
    ma = parse_ma(q.get("ma", "vwma:100"))
    if ma is None:
        raise ValueError("이평선을 골라 주세요")
    tfs = [t for t in q.get("tfs", "5m,15m").split(",") if t]
    if not tfs or any(t not in INTERVALS for t in tfs):
        raise ValueError(f"봉은 {', '.join(INTERVALS)} 중에서")
    pct = float(q.get("pct", 1) or 1)
    mode = q.get("mode", "all")
    side = q.get("side", "both")  # both / above(선 위) / below(선 아래)
    min_qv = float(q.get("min_qv", 0) or 0) * 1e6  # 24시간 거래대금 하한 (백만 달러)
    atr_tf = q.get("atr_tf", "1d") or "1d"
    if atr_tf not in INTERVALS:
        raise ValueError(f"ATR 봉은 {', '.join(INTERVALS)} 중에서")
    atr_n = max(1, int(float(q.get("atr_n", 14) or 14)))
    atr_min = float(q.get("atr_min", 0) or 0)
    syms = [s for s in market.symbols() if s["qv"] >= min_qv]

    def one(s: dict) -> dict | None:
        res = {}
        for tf in tfs:
            try:
                res[tf] = near_line(market.bars(s["symbol"], tf, ma[1] + 2), ma[0], ma[1])
            except Exception:  # noqa: BLE001 한 종목 오류는 건너뜀
                res[tf] = None
        def ok(r):
            if r is None or abs(r["dist_pct"]) > pct:
                return False
            return side == "both" or (side == "above") == (r["dist_pct"] >= 0)
        hits = [ok(res[tf]) for tf in tfs]
        if not (all(hits) if mode == "all" else any(hits)):
            return None
        try:  # 이평선 조건을 통과한 종목만 ATR 을 잼 (요청 수 절약)
            atr = atr_pct(market.bars(s["symbol"], atr_tf, atr_n + 2), atr_n)
        except Exception:  # noqa: BLE001
            atr = None
        if atr_min > 0 and (atr is None or atr < atr_min):
            return None
        return {"symbol": s["symbol"], "group": s["group"], "qv": s["qv"], "atr_pct": atr,
                "price": next(r["price"] for r in res.values() if r),
                "tf": {tf: (None if r is None else {"ma": r["ma"], "dist_pct": r["dist_pct"], "in": h})
                       for (tf, r), h in zip(res.items(), hits)}}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [r for r in ex.map(one, syms) if r]
    found.sort(key=lambda r: min(abs(v["dist_pct"]) for v in r["tf"].values() if v))
    return {"ma": {"kind": ma[0], "n": ma[1]}, "tfs": tfs, "pct": pct, "mode": mode,
            "atr": {"tf": atr_tf, "n": atr_n, "min": atr_min},
            "checked": len(syms), "found": found, "time": int(time.time())}


# ------------------------------------------------------------ 웹 서버
class Handler(BaseHTTPRequestHandler):
    market: Market

    def log_message(self, fmt, *args):  # noqa: A003 조용히
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
                return
            if u.path == "/api/symbols":
                data = self.market.symbols()
            elif u.path == "/api/run":
                data = run(self.market, q)
            elif u.path == "/api/scan":
                data = scan(self.market, q)
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as exc:  # noqa: BLE001 화면에 오류 표시
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")


def lan_ip() -> str | None:
    """이 PC 의 집 안 네트워크 주소 (예 192.168.0.12). 실제로 보내는 것은 없음."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


class _Server(ThreadingHTTPServer):
    # 윈도우는 주소 재사용을 켜면 같은 포트에 두 번 켜져 버려서 끔 (두 번째 실행은 화면만 열게)
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def main() -> None:
    ap = argparse.ArgumentParser(description="이평선 매매 웹 (바이낸스 시세, 주문 없음)")
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동으로 안 열기")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 폰·태블릿에서도 열 수 있게 (주문 기능은 없음)")
    a = ap.parse_args()
    Handler.market = Market()
    url = f"http://localhost:{a.port}"
    try:
        srv = _Server(("0.0.0.0" if a.lan else "127.0.0.1", a.port), Handler)
    except OSError:
        # 이미 켜져 있으면 (바로가기를 두 번 누름) 화면만 열고 끝냄
        print(f"이미 켜져 있어요. 화면만 엽니다: {url}")
        if not a.no_open:
            webbrowser.open(url)
        return
    print(f"이평선 매매 화면: {url}  (끄려면 이 창에서 Ctrl+C)")
    if a.lan:
        ip = lan_ip()
        if ip:
            print(f"폰에서 열기 (같은 와이파이): http://{ip}:{a.port}")
        else:
            print("이 PC 의 네트워크 주소를 못 찾았어요. 명령 창에서 ipconfig 로 IPv4 주소를 확인하세요.")
        print("처음 한 번 윈도우 방화벽 창이 뜨면 '개인 네트워크' 허용을 눌러 주세요.")
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
