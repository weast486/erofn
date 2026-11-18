"""오더블록 + 이평선 지지 백테스트 (2026-11-16 사용자 요청).

오더블록 화면(binancebot/obweb.py)의 모의 매매 규칙 그대로, '자리 저점(숏은 고점)이나 손절선이
이평선과 가까울 때만 진입' 조건을 붙였을 때와 안 붙였을 때를 비교한다.
이평선은 15분봉 기준 20선·50선·거래량가중 100선·200선, 넷 중 하나라도 가까우면 진입 (차트가 1시간이어도
15분봉 이평선을 씀 — 진입 봉 시작 시각까지 끝난 15분봉의 값).

데이터: data/binance_5m (주식 토큰 41개, 24시간 5분봉) + data/binance_5m_crypto (BTC·ETH 2022~).
5분봉을 15분·1시간·4시간·일봉(UTC)으로 합쳐 5개 봉의 오더블록을 만든다. KORU 는 2026-07-15 20:1 분할 보정.

    python scripts/ob_ma_backtest.py                 # 15분·1시간 차트, 모든 이평선·범위
    python scripts/ob_ma_backtest.py --tf 15m --group crypto
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from binancebot.maweb import moving_average  # noqa: E402
from binancebot.obweb import TF_SEC, TFS, order_blocks, simulate  # noqa: E402

KORU_SPLIT_MS = 1784108100000
# 지지 이평선은 15분봉 기준 4개만 (사용자 지정): 20선·50선·거래량가중 100선·200선, 어느 것이든 하나만 가까우면 됨
MAS = [("sma", 20), ("sma", 50), ("vwma", 100), ("sma", 200)]
PCTS = [0.2, 0.5, 1.0]
FEE = 0.05
ZONE_SETS = {"5개 봉": TFS, "15분 이상": ("1d", "4h", "1h", "15m"), "큰 봉(일·4h·1h)": ("1d", "4h", "1h")}


def load_5m(path: Path) -> list[dict]:
    out = []
    koru = path.name.startswith("KORU")
    with open(path) as f:
        r = csv.reader(f)
        next(r)
        for row in r:
            ms = int(row[0])
            o, h, l, c, v = (float(x) for x in row[1:6])
            if koru and ms < KORU_SPLIT_MS:
                o, h, l, c, v = o / 20, h / 20, l / 20, c / 20, v * 20
            out.append({"t": ms // 1000, "o": o, "h": h, "l": l, "c": c, "v": v})
    return out


def resample(bars: list[dict], sec: int) -> list[dict]:
    out: list[dict] = []
    for b in bars:
        k = b["t"] - b["t"] % sec
        if out and out[-1]["t"] == k:
            x = out[-1]
            x["h"] = max(x["h"], b["h"])
            x["l"] = min(x["l"], b["l"])
            x["c"] = b["c"]
            x["v"] += b["v"]
        else:
            out.append({"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out


def fast_blocks(bars: list[dict], open_tol: float = 0.05) -> list[dict]:
    """obweb.order_blocks 와 같은 결과(touches 제외)를 numpy 로 빠르게."""
    n = len(bars)
    if n < 3:
        return []
    o = np.array([b["o"] for b in bars]); h = np.array([b["h"] for b in bars])
    l = np.array([b["l"] for b in bars]); c = np.array([b["c"] for b in bars])
    v = np.array([b["v"] for b in bars]); t = np.array([b["t"] for b in bars])
    last = n - 2
    po, pc, bo, bc = o[:-1], c[:-1], o[1:], c[1:]
    tol = pc * open_tol / 100
    vol = v[1:] > v[:-1]
    bull = vol & (pc < po) & (bc > bo) & (bc > po) & (bo <= pc + tol)
    bear = vol & (pc > po) & (bc < bo) & (bc < po) & (bo >= pc - tol)
    out = []
    for i in np.nonzero(bull | bear)[0] + 1:
        if i > last:
            continue
        d = 1 if bull[i - 1] else -1
        top, bot = max(o[i - 1], c[i - 1]), min(o[i - 1], c[i - 1])
        k0 = max(0, i - 1 - 7)
        if d > 0:  # 자리 저점이 앞 7봉 최저가 이하이거나 1% 이내
            sw = i - 1 - k0 >= 7 and bot <= l[k0:i - 1].min() * 1.01
        else:
            sw = i - 1 - k0 >= 7 and top >= h[k0:i - 1].max() * 0.99
        z = {"dir": "bull" if d > 0 else "bear", "t": int(t[i - 1]), "t_engulf": int(t[i]), "top": float(top),
             "bottom": float(bot), "broken_t": None, "touched_t": None, "swing": bool(sw)}
        j0, step = i + 1, 256
        while j0 <= last and (z["broken_t"] is None):
            j1 = min(last + 1, j0 + step)
            br = (c[j0:j1] < bot) if d > 0 else (c[j0:j1] > top)
            tc = (l[j0:j1] <= top) & (h[j0:j1] >= bot)
            if z["touched_t"] is None:
                k = np.argmax(tc | br)
                if (tc | br)[k]:
                    z["touched_t"] = int(t[j0 + k])
            k = np.argmax(br)
            if br[k]:
                z["broken_t"] = int(t[j0 + k])
            j0, step = j1, step * 2
        out.append(z)
    return out


def align(ma: list, b15: list[dict], bars: list[dict], sec: int) -> list:
    """차트 봉 i 에 '봉 i 가 끝날 때까지 끝난 마지막 15분봉'의 이평선 값을 맞춤 (simulate 는 i-1 을 씀)."""
    idx = {b["t"]: j for j, b in enumerate(b15)}
    out = []
    for b in bars:
        j = idx.get(b["t"] + sec - 900)
        out.append(ma[j] if j is not None else None)
    return out


def one(args) -> dict:
    path, tfs, znames, swing = args
    b5 = load_5m(Path(path))
    allbars = {"5m": b5}
    for tf in ("15m", "1h", "4h", "1d"):
        allbars[tf] = resample(b5, TF_SEC[tf])
    zones = {tf: fast_blocks(allbars[tf]) for tf in TFS}
    if swing:
        zones = {tf: [z for z in zs if z["swing"]] for tf, zs in zones.items()}
    b15 = allbars["15m"]
    c15, v15 = [b["c"] for b in b15], [b["v"] for b in b15]
    ma15 = {(k, n): moving_average(k, n, c15, v15) for k, n in MAS}
    res = {}
    for tf in tfs:
        bars = allbars[tf]
        lines = [align(ma15[(k, n)], b15, bars, TF_SEC[tf]) for k, n in MAS]
        runs = {}
        for zname in znames:
            use = ZONE_SETS[zname]
            kw = dict(use_tfs=use, side="both", tp_mode="near", tp_r=5, fresh=True, fee_pct=FEE)
            runs[f"{zname}|없음"] = simulate(bars, tf, zones, **kw)
            for p in PCTS:
                runs[f"{zname}|4선중하나 {p}%"] = simulate(bars, tf, zones, ma_line=lines, ma_pct=p, **kw)
        # (방향, 손익%, 진입 시각, 수수료 전 R)
        res[tf] = {k: [(x["dir"], x["ret_pct"], x["entry_time"],
                        (x["ret_pct"] + 2 * FEE) / (abs(x["entry"] - x["sl"]) / x["entry"] * 100)
                        if x["entry"] != x["sl"] else 0.0) for x in r["trades"]] for k, r in runs.items()}
    return {"symbol": Path(path).stem.replace("_5m", ""), "res": res}


def summary(trades: list[tuple]) -> dict:
    rets = sorted(x[1] for x in trades)
    n = len(rets)
    if not n:
        return {"n": 0}
    lo = [x[1] for x in trades if x[0] == "long"]
    sh = [x[1] for x in trades if x[0] == "short"]
    rs = [max(-3.0, min(10.0, x[3])) for x in trades]  # 갭으로 튄 값은 -3R~10R 로 자름
    return {"n": n, "win": sum(r > 0 for r in rets) / n * 100, "avg": sum(rets) / n, "R": sum(rs) / n,
            "avg_ex5": sum(rets[:-5]) / (n - 5) if n > 5 else None, "sum": sum(rets),
            "long": (len(lo), sum(lo) / len(lo) if lo else 0.0), "short": (len(sh), sum(sh) / len(sh) if sh else 0.0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="+", default=["15m", "1h"])
    ap.add_argument("--group", choices=["all", "stock", "crypto"], default="all")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--swing", action="store_true", help="자리 저점(숏은 고점)이 앞 7봉 최저가(최고가) 1%% 이내인 자리만")
    ap.add_argument("--zones", nargs="+", default=list(ZONE_SETS), help="쓸 자리 묶음 이름")
    ap.add_argument("--out", default=str(ROOT / "reports" / "ob_ma" / "result.json"))
    a = ap.parse_args()
    files = []
    if a.group in ("all", "stock"):
        files += [("stock", p) for p in sorted((ROOT / "data" / "binance_5m").glob("*_5m.csv"))]
    if a.group in ("all", "crypto"):
        files += [("crypto", p) for p in sorted((ROOT / "data" / "binance_5m_crypto").glob("*_5m.csv"))]
    group = {p.stem.replace("_5m", ""): g for g, p in files}
    t0 = time.time()
    results = []
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for r in ex.map(one, [(str(p), a.tf, a.zones, a.swing) for _, p in files]):
            results.append(r)
            print(f"{r['symbol']} 끝 ({time.time() - t0:.0f}초)", flush=True)
    configs = list(results[0]["res"][a.tf[0]].keys())
    table = {}
    for tf in a.tf:
        for g in ("stock", "crypto"):
            rs = [r for r in results if group[r["symbol"]] == g]
            if not rs:
                continue
            for k in configs:
                trades = [x for r in rs for x in r["res"][tf][k]]
                s = summary(trades)
                s["sym_pos"] = sum(1 for r in rs if sum(x[1] for x in r["res"][tf][k]) > 0)
                s["sym_n"] = len(rs)
                if g == "crypto":  # 해마다
                    import datetime as dt
                    yr = {}
                    for x in trades:
                        y = dt.datetime.fromtimestamp(x[2], dt.timezone.utc).year
                        yr.setdefault(y, []).append(x[1])
                    s["years"] = {y: (len(v), sum(v) / len(v)) for y, v in sorted(yr.items())}
                table[f"{tf}|{g}|{k}"] = s
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    for tf in a.tf:
        for g in ("stock", "crypto"):
            print(f"\n== {tf} 차트 · {'주식 토큰 41' if g == 'stock' else 'BTC·ETH'} ==")
            print(f"{'자리 | 이평선 조건':<26}{'거래':>7}{'승률':>6}{'거래당%':>9}{'5건뺌':>8}{'R(수수료전)':>11}{'롱%':>13}{'숏%':>13}{'이익종목':>8}")
            for k in configs:
                s = table.get(f"{tf}|{g}|{k}")
                if not s or not s["n"]:
                    continue
                ex5 = f"{s['avg_ex5']:+.2f}" if s["avg_ex5"] is not None else "-"
                print(f"{k:<26}{s['n']:>7}{s['win']:>5.0f}%{s['avg']:>+9.3f}{ex5:>8}{s['R']:>+11.3f}"
                      f"{s['long'][0]:>6} {s['long'][1]:>+6.2f}%{s['short'][0]:>6} {s['short'][1]:>+6.2f}%"
                      f"{s['sym_pos']:>4}/{s['sym_n']}")
                if s.get("years"):
                    print("      해마다 거래당 " + "  ".join(f"{y} {v[1]:+.2f}%({v[0]})" for y, v in s["years"].items()))


if __name__ == "__main__":
    main()
