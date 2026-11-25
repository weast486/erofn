"""SOXL 과 편입 비중 큰 종목 5개 차트 비교 화면 (조회만, 주문 없음).

- 편입 종목·비중: Direxion 공개 보유 종목 CSV (6시간 캐시, 못 받으면 기본 목록)
- 시세: 야후 파이낸스 공개 차트 API (키 필요 없음, 정규장만)
실행: python -m usbot.soxlweb  → http://localhost:8792
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import threading
import time
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

HTML = Path(__file__).with_name("soxlweb.html")
HOLDINGS_URL = "https://www.direxion.com/holdings/{etf}.csv"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={iv}&includePrePost=false"
UA = {"User-Agent": "Mozilla/5.0"}
# 기간 → 야후 봉 간격
RANGES = {"1d": "5m", "5d": "15m", "1mo": "1h", "3mo": "1d", "6mo": "1d", "1y": "1d", "2y": "1wk"}
# Direxion 을 못 받을 때 쓰는 목록 (2026-11-24 기준 비중 %)
FALLBACK = [("AMD", "ADVANCED MICRO DEVICES", 6.23), ("AVGO", "BROADCOM INC", 5.47), ("NVDA", "NVIDIA CORP", 4.83),
            ("MU", "MICRON TECHNOLOGY INC", 4.77), ("INTC", "INTEL CORP", 4.31), ("TXN", "TEXAS INSTRUMENTS INC", 3.61),
            ("ADI", "ANALOG DEVICES INC", 3.38)]


def _get(url: str, timeout: float = 20) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return r.read()


def parse_holdings(text: str) -> tuple[str, list[dict]]:
    """Direxion CSV → (기준일, 종목 목록 비중 큰 순). 현금·스왑(티커 없음) 줄은 뺌."""
    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith('"TradeDate"') or ln.startswith("TradeDate"))
    rows = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    out, date = [], ""
    for r in rows:
        tk = (r.get("StockTicker") or "").strip()
        if not tk:
            continue
        date = (r.get("TradeDate") or "").split(" ")[0]
        try:
            pct = float(r.get("HoldingsPercent") or 0)
        except ValueError:
            continue
        out.append({"symbol": tk.replace(" ", "-"), "name": (r.get("SecurityDescription") or "").strip(), "weight": pct})
    out.sort(key=lambda x: -x["weight"])
    return date, out


class Data:
    def __init__(self, fetch=_get):
        self.fetch = fetch
        self._lock = threading.Lock()
        self._hold: dict[str, tuple[float, dict]] = {}

    def holdings(self, etf: str = "SOXL") -> dict:
        with self._lock:
            hit = self._hold.get(etf)
            if hit and time.time() - hit[0] < 6 * 3600:
                return hit[1]
        try:
            date, rows = parse_holdings(self.fetch(HOLDINGS_URL.format(etf=etf)).decode("utf-8", "replace"))
            if not rows:
                raise ValueError("종목 없음")
            res = {"date": date, "rows": rows, "source": "Direxion"}
        except Exception as exc:  # noqa: BLE001
            res = {"date": "", "source": f"기본 목록 (Direxion 조회 실패: {exc})",
                   "rows": [{"symbol": s, "name": n, "weight": w} for s, n, w in FALLBACK]}
        with self._lock:
            self._hold[etf] = (time.time(), res)
        return res

    def chart(self, sym: str, rng: str) -> list[list[float]]:
        """[[시각(초), 종가], ...] 정규장 봉만."""
        raw = json.loads(self.fetch(CHART_URL.format(sym=quote(sym), rng=rng, iv=RANGES[rng])))
        res = (raw.get("chart") or {}).get("result") or []
        if not res:
            raise ValueError(f"{sym} 시세 없음")
        r = res[0]
        ts = r.get("timestamp") or []
        closes = ((r.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        return [[t, c] for t, c in zip(ts, closes) if c is not None]


def build(data: Data, q: dict) -> dict:
    etf = (q.get("etf") or "SOXL").upper()
    rng = q.get("range", "5d")
    if rng not in RANGES:
        raise ValueError(f"기간은 {', '.join(RANGES)} 중 하나")
    n = max(1, min(10, int(q.get("n", 5) or 5)))
    hold = data.holdings(etf)
    top = hold["rows"][:n]
    syms = [etf] + [h["symbol"] for h in top]
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda s: _safe(data.chart, s, rng), syms))
    series, errors = {}, {}
    for s, (ok, v) in zip(syms, results):
        if ok and v:
            series[s] = v
        else:
            errors[s] = v if not ok else "데이터 없음"
    lines = align(series)
    # 편입 종목 가중평균 (비중대로, 기간 시작 = 0%) 과 그 3배
    ws = {h["symbol"]: h["weight"] for h in top if h["symbol"] in lines}
    tot = sum(ws.values())
    if lines and tot > 0:
        k = len(lines[etf if etf in lines else next(iter(lines))])
        basket = [sum(lines[s][i] * w for s, w in ws.items()) / tot for i in range(k)]
        lines[f"{n}종목 가중평균"] = basket
    times = sorted({t for v in series.values() for t, _ in v})
    stats = {}
    for s, v in series.items():
        stats[s] = {"last": v[-1][1], "first": v[0][1], "chg": (v[-1][1] / v[0][1] - 1) * 100}
    return {"etf": etf, "range": rng, "interval": RANGES[rng], "holdings": hold | {"rows": hold["rows"][:15]},
            "top": top, "times": times, "lines": lines, "stats": stats, "errors": errors, "now": int(time.time())}


def _safe(fn, *a):
    try:
        return True, fn(*a)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def align(series: dict[str, list[list[float]]]) -> dict[str, list[float | None]]:
    """시각을 합쳐 맞추고 기간 첫 값 대비 % 변화로 바꿈 (빈 칸은 직전 값, 첫 값 전은 None)."""
    times = sorted({t for v in series.values() for t, _ in v})
    out = {}
    for s, v in series.items():
        m = dict((t, c) for t, c in v)
        base = v[0][1]
        last, row = None, []
        for t in times:
            if t in m:
                last = m[t]
            row.append(None if last is None else (last / base - 1) * 100)
        out[s] = row
    return out


class Handler(BaseHTTPRequestHandler):
    data: Data

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
            if u.path != "/api/compare":
                self._send(404, b"not found", "text/plain")
                return
            body = json.dumps(build(self.data, q), ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="SOXL 과 편입 상위 종목 차트 비교 (조회만)")
    ap.add_argument("--port", type=int, default=8792)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    Handler.data = Data()
    url = f"http://localhost:{a.port}"
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        print(f"이미 켜져 있어요. 화면만 엽니다: {url}")
        if not a.no_open:
            webbrowser.open(url)
        return
    print(f"SOXL 편입 종목 비교 화면: {url}  (끄려면 이 창에서 Ctrl+C)")
    if not a.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
