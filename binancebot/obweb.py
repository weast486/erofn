"""오더블록 화면 (바이낸스 선물 시세, 키 필요 없음·주문 없음).

    python -m binancebot.obweb             # http://localhost:8791 을 브라우저로 열기
    python -m binancebot.obweb --lan       # 같은 와이파이의 폰에서도 열기

일봉·4시간·1시간·15분·5분봉에서 장악형 캔들만 오더블록으로 표시한다.
  상승 장악형: 앞 봉이 음봉, 이번 봉이 양봉이고 이번 몸통이 앞 봉 몸통을 감쌈
              (종가 > 앞 봉 시가, 시가 <= 앞 봉 종가) + 이번 봉 거래량 > 앞 봉 거래량 (무조건)
  하락 장악형: 반대.
  오더블록 자리 = 장악당한 앞 봉 (몸통 또는 꼬리 포함, 선택).
  깨짐: 그 뒤 끝난 봉 종가가 자리 아래(상승)·위(하락)로 마감하면 깨진 자리.
겹치는 자리: 서로 다른 봉의 살아 있는 자리가 겹치는 가격 구간 (같은 방향끼리, 선택으로 방향 무관).
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .maweb import Market, _Server, lan_ip

TFS = ("1d", "4h", "1h", "15m", "5m")
TF_SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
HTML = Path(__file__).with_name("obweb.html")


def engulfing(p: dict, b: dict) -> int:
    """앞 봉 p, 이번 봉 b 가 장악형이면 1(상승)/-1(하락), 아니면 0. 거래량 조건 포함."""
    if b["v"] <= p["v"]:
        return 0
    if p["c"] < p["o"] and b["c"] > b["o"] and b["c"] > p["o"] and b["o"] <= p["c"]:
        return 1
    if p["c"] > p["o"] and b["c"] < b["o"] and b["c"] < p["o"] and b["o"] >= p["c"]:
        return -1
    return 0


def order_blocks(bars: list[dict], basis: str = "body") -> list[dict]:
    """끝난 봉에서 찾은 장악형 오더블록 목록 (마지막 봉은 진행 중이라 신호 확인에 안 씀).

    basis: body = 앞 봉 몸통 / wick = 앞 봉 고가~저가.
    각 자리: dir, t(장악당한 앞 봉 시각), t_engulf(장악 봉 시각), top, bottom,
    broken_t(깨진 봉 시각, 없으면 None), touches(만들어진 뒤 가격이 자리에 다시 들어온 끝난 봉 수).
    """
    out = []
    last = len(bars) - 2
    for i in range(1, last + 1):
        p, b = bars[i - 1], bars[i]
        d = engulfing(p, b)
        if not d:
            continue
        if basis == "wick":
            top, bot = p["h"], p["l"]
        else:
            top, bot = max(p["o"], p["c"]), min(p["o"], p["c"])
        z = {"dir": "bull" if d > 0 else "bear", "t": p["t"], "t_engulf": b["t"], "top": top, "bottom": bot,
             "vol_x": b["v"] / p["v"] if p["v"] else None, "broken_t": None, "touches": 0}
        for j in range(i + 1, last + 1):
            c = bars[j]
            if (d > 0 and c["c"] < bot) or (d < 0 and c["c"] > top):
                z["broken_t"] = c["t"]
                break
            if c["l"] <= top and c["h"] >= bot:
                z["touches"] += 1
        out.append(z)
    return out


def overlaps(zones: dict[str, list[dict]], min_tfs: int = 2, same_dir: bool = True) -> list[dict]:
    """서로 다른 봉 min_tfs 개 이상의 살아 있는 자리가 겹치는 가격 구간.

    zones: {봉: [자리, ...]}. 같은 봉 자리끼리 겹치는 건 1개로 셈.
    결과: dir(bull/bear/mixed), bottom, top, tfs(겹친 봉), start(겹침이 생긴 시각 = 겹친 자리 중 가장 늦은 시각).
    """
    alive = [(tf, z) for tf, zs in zones.items() for z in zs if z["broken_t"] is None and z["top"] > z["bottom"]]
    groups = [("bull", [x for x in alive if x[1]["dir"] == "bull"]), ("bear", [x for x in alive if x[1]["dir"] == "bear"])]
    if not same_dir:
        groups = [("mixed", alive)]
    res = []
    for d, items in groups:
        pts = sorted({z["bottom"] for _, z in items} | {z["top"] for _, z in items})
        cur = None
        for a, b in zip(pts, pts[1:]):
            cov = [(tf, z) for tf, z in items if z["bottom"] <= a and z["top"] >= b]
            tfs = sorted({tf for tf, _ in cov}, key=TFS.index)
            if len(tfs) < min_tfs:
                cur = None
                continue
            dirs = {z["dir"] for _, z in cov}
            seg_dir = d if d != "mixed" else (dirs.pop() if len(dirs) == 1 else "mixed")
            start = max(z["t"] for _, z in cov)
            if cur and cur["top"] == a and cur["tfs"] == tfs and cur["dir"] == seg_dir:
                cur["top"] = b
                cur["start"] = max(cur["start"], start)
            else:
                cur = {"dir": seg_dir, "bottom": a, "top": b, "tfs": tfs, "start": start}
                res.append(cur)
    return res


def simulate(bars: list[dict], tf: str, zones: dict[str, list[dict]], use_tfs=TFS, source: str = "zones",
             min_tfs: int = 2, same_dir: bool = True, side: str = "long", entry: str = "edge",
             first_only: bool = True, stop_mode: str = "touch", stop_buf: float = 0.0, tp_r: float = 2.0,
             max_hold: int = 0, fee_pct: float = 0.05, leverage: float = 1.0) -> dict:
    """오더블록 되돌림 모의 매매 (차트로 보는 봉 tf 의 봉으로 계산, 한 번에 한 포지션).

    쓸 자리: source=zones → use_tfs 봉의 살아 있는 자리 / overlap → 그 자리들이 min_tfs 개 봉 이상 겹친 구간.
      자리는 장악 봉이 끝난 뒤부터 쓰고(그 전 봉에서는 모름), 자기 봉에서 깨진 뒤에는 안 씀.
    롱: 직전 봉 종가가 자리 위에 있다가 가격이 자리에 닿으면 지정가 (entry=edge 자리 윗선 / mid 가운데,
        시가가 이미 아래면 시가, 시가가 손절가 이하면 안 삼). 숏은 반대.
      first_only: 자리마다 처음 닿을 때만 (닿았는데 보유 중이라 못 산 경우도 한 번으로 셈).
    손절: stop_mode=touch → 자리 아랫선 -stop_buf% 에 닿으면 그 가격 / close → 종가가 그 아래로 마감하면 다음 봉 시가.
    익절: 진입가 + (진입가 - 손절가) x tp_r (0 = 없음). max_hold 봉 지나면 그 봉 종가 (0 = 없음).
    산 봉에서는 손절만 확인, 같은 봉에서 손절·익절 둘 다 닿으면 손절.
    """
    from .maweb import stats
    sec = TF_SEC
    dirs = {"long": [1], "short": [-1]}.get(side, [1, -1])
    fee = fee_pct / 100
    n = len(bars)
    last_closed = n - 2
    pool = [(t, z) for t in use_tfs for z in zones.get(t, [])]
    used: set = set()
    trades: list[dict] = []
    pos = None
    pend_close = False

    def alive_at(t0: int) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for t, z in pool:
            if z["t_engulf"] + sec[t] > t0:
                continue
            if z["broken_t"] is not None and z["broken_t"] + sec[t] <= t0:
                continue
            out.setdefault(t, []).append({**z, "broken_t": None, "_key": (t, z["t"], z["dir"])})
        return out

    cache = {"sig": None, "zs": []}

    def candidates(t0: int) -> list[dict]:
        al = alive_at(t0)
        sig = tuple(sorted(z["_key"] for zs in al.values() for z in zs))
        if sig != cache["sig"]:
            if source == "overlap":
                zs = [{**o, "_key": ("ov", o["dir"], o["bottom"], o["top"])}
                      for o in overlaps(al, min_tfs, same_dir) if o["dir"] != "mixed"]
            else:
                zs = [z for v in al.values() for z in v]
            cache.update(sig=sig, zs=zs)
        return cache["zs"]

    def levels(z: dict, d: int) -> tuple[float, float]:
        mid = (z["top"] + z["bottom"]) / 2
        px = mid if entry == "mid" else (z["top"] if d > 0 else z["bottom"])
        sl = z["bottom"] * (1 - stop_buf / 100) if d > 0 else z["top"] * (1 + stop_buf / 100)
        return px, sl

    def close_pos(i: int, price: float, reason: str) -> None:
        nonlocal pos
        d = pos["dir"]
        trades.append({"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                       "exit_time": bars[i]["t"], "exit": price, "reason": reason, "zone": pos["zone"],
                       "ret_pct": (d * (price / pos["entry"] - 1) - 2 * fee) * 100, "bars": i - pos["i"]})
        pos = None

    for i in range(1, n):
        b, prev = bars[i], bars[i - 1]
        if pend_close and pos:
            close_pos(i, b["o"], "stop")
        pend_close = False
        # 1) 보유 중: 손절·익절·시간
        if pos and pos["i"] < i:
            d, sl, tp = pos["dir"], pos["sl"], pos["tp"]
            if stop_mode == "touch" and (b["l"] <= sl if d > 0 else b["h"] >= sl):
                close_pos(i, min(b["o"], sl) if d > 0 else max(b["o"], sl), "stop")
            elif tp and (b["h"] >= tp if d > 0 else b["l"] <= tp):
                close_pos(i, max(b["o"], tp) if d > 0 else min(b["o"], tp), "take")
            elif max_hold and i - pos["i"] >= max_hold and i <= last_closed:
                close_pos(i, b["c"], "time")
        # 2) 새 진입 (자리에 닿는지)
        if pos is None:
            hits = []
            for z in candidates(b["t"]):
                d = 1 if z["dir"] == "bull" else -1
                if d not in dirs or (first_only and z["_key"] in used):
                    continue
                came = prev["c"] > z["top"] if d > 0 else prev["c"] < z["bottom"]
                touch = b["l"] <= z["top"] if d > 0 else b["h"] >= z["bottom"]
                if not (came and touch):
                    continue
                used.add(z["_key"])
                px, sl = levels(z, d)
                if not (b["l"] <= px if d > 0 else b["h"] >= px):
                    continue  # 자리 윗선만 스치고 가운데까지는 안 옴
                fill = min(b["o"], px) if d > 0 else max(b["o"], px)
                if (fill <= sl) if d > 0 else (fill >= sl):
                    continue
                hits.append((-d * fill, d, fill, sl, z))  # 롱은 높은 자리부터 닿음
            if hits:
                hits.sort(key=lambda h: h[0])
                _, d, fill, sl, z = hits[0]
                risk = abs(fill - sl)
                tp = fill + d * risk * tp_r if tp_r > 0 else None
                zone = z.get("tfs") or [z["_key"][0]]
                pos = {"dir": d, "entry": fill, "t": b["t"], "i": i, "sl": sl, "tp": tp, "zone": zone}
                if stop_mode == "touch" and (b["l"] <= sl if d > 0 else b["h"] >= sl):
                    close_pos(i, sl, "stop")
        else:
            for z in candidates(b["t"]):  # 보유 중에 닿은 자리도 처음 닿음으로 셈
                d = 1 if z["dir"] == "bull" else -1
                came = prev["c"] > z["top"] if d > 0 else prev["c"] < z["bottom"]
                if came and (b["l"] <= z["top"] if d > 0 else b["h"] >= z["bottom"]):
                    used.add(z["_key"])
        # 3) 종가 손절 (끝난 봉만)
        if pos and stop_mode == "close" and i <= last_closed:
            d = pos["dir"]
            if (b["c"] < pos["sl"]) if d > 0 else (b["c"] > pos["sl"]):
                pend_close = True

    open_info = None
    if pos:
        last = bars[-1]["c"]
        d = pos["dir"]
        open_info = {"dir": "long" if d > 0 else "short", "entry_time": pos["t"], "entry": pos["entry"],
                     "price": last, "sl": pos["sl"], "tp": pos["tp"], "zone": pos["zone"],
                     "ret_pct": (d * (last / pos["entry"] - 1) - fee) * 100}
    waits = []
    if pos is None and n:
        last = bars[-1]
        for z in candidates(last["t"] + 1):
            d = 1 if z["dir"] == "bull" else -1
            if d not in dirs or (first_only and z["_key"] in used):
                continue
            if (last["c"] > z["top"]) if d > 0 else (last["c"] < z["bottom"]):
                px, sl = levels(z, d)
                waits.append({"dir": "long" if d > 0 else "short", "price": px, "sl": sl,
                              "zone": z.get("tfs") or [z["_key"][0]], "dist_pct": (px / last["c"] - 1) * 100})
        waits.sort(key=lambda w: abs(w["dist_pct"]))
    return {"trades": trades, "open": open_info, "waits": waits[:6], "pending": "다음 봉 시가에 손절" if pend_close else None,
            "stats": stats(trades, leverage)}


def run(market: Market, q: dict) -> dict:
    symbol = q.get("symbol", "BTCUSDT").upper()
    basis = q.get("basis", "body")
    count = max(50, min(1500, int(float(q.get("bars", 500) or 500))))
    min_tfs = max(2, min(5, int(float(q.get("min_tfs", 2) or 2))))
    same_dir = q.get("same_dir", "1") != "0"
    view = q.get("interval", "1h")
    if view not in TF_SEC:
        raise ValueError(f"봉은 {', '.join(TFS)} 중 하나")
    with ThreadPoolExecutor(max_workers=5) as ex:
        bars = dict(zip(TFS, ex.map(lambda tf: market.bars(symbol, tf, count), TFS)))
    zones = {tf: order_blocks(b, basis) for tf, b in bars.items()}
    price = bars["5m"][-1]["c"] if bars["5m"] else bars[view][-1]["c"]
    ov = overlaps(zones, min_tfs, same_dir)
    for o in ov:
        o["inside"] = o["bottom"] <= price <= o["top"]
        mid = o["top"] if price > o["top"] else o["bottom"] if price < o["bottom"] else price
        o["dist_pct"] = (mid / price - 1) * 100 if price else 0.0
    ov.sort(key=lambda o: abs(o["dist_pct"]))
    sim = None
    if q.get("sim", "0") == "1":
        num = lambda k, dflt: float(q.get(k, dflt) or 0)  # noqa: E731
        use = [t for t in q.get("tfs", ",".join(TFS)).split(",") if t in TF_SEC] or list(TFS)
        sim = simulate(bars[view], view, zones, use_tfs=use, source=q.get("src", "zones"), min_tfs=min_tfs,
                       same_dir=same_dir, side=q.get("side", "long"), entry=q.get("entry", "edge"),
                       first_only=q.get("first", "1") != "0", stop_mode=q.get("stop_mode", "touch"),
                       stop_buf=num("sl_buf", 0), tp_r=num("tp_r", 2), max_hold=int(num("max_hold", 0)),
                       fee_pct=num("fee", 0.05), leverage=num("lev", 1) or 1)
    return {"symbol": symbol, "interval": view, "price": price, "bars": bars[view], "zones": zones,
            "overlaps": ov, "sim": sim, "time": int(time.time())}


class Handler(BaseHTTPRequestHandler):
    market: Market

    def log_message(self, fmt, *args):  # noqa: A003
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
            elif u.path == "/api/ob":
                data = run(self.market, q)
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="오더블록 화면 (바이낸스 시세, 주문 없음)")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 폰에서도 열 수 있게")
    a = ap.parse_args()
    Handler.market = Market()
    url = f"http://localhost:{a.port}"
    try:
        srv = _Server(("0.0.0.0" if a.lan else "127.0.0.1", a.port), Handler)
    except OSError:
        print(f"이미 켜져 있어요. 화면만 엽니다: {url}")
        if not a.no_open:
            webbrowser.open(url)
        return
    print(f"오더블록 화면: {url}  (끄려면 이 창에서 Ctrl+C)")
    if a.lan and (ip := lan_ip()):
        print(f"폰에서 열기 (같은 와이파이): http://{ip}:{a.port}")
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
