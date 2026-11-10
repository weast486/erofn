"""1시간봉: 200선이 10·20·50선보다 위에 있을 때 종가가 X선(10/20/50) 아래로 내려가면 다음 봉 시가 숏."""
import glob, os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

ET = ZoneInfo("America/New_York")
KORU_SPLIT = int(datetime(2026, 7, 15, 9, 30, tzinfo=timezone.utc).timestamp() * 1000)
COST = 0.002


def load(f):
    sym = os.path.basename(f).split("_")[0]
    d = pd.read_csv(f)
    if sym == "KORUUSDT":
        m = d.open_ms < KORU_SPLIT
        for c in ("open", "high", "low", "close"):
            d.loc[m, c] /= 20
    return sym, d


def deadline(ms):
    t = datetime.fromtimestamp(ms / 1000, ET)
    d = t.replace(hour=16, minute=0, second=0, microsecond=0)
    if t >= d:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return int(d.timestamp() * 1000)


def sim(d, line, cond, stop, tp, time_exit, max_bars=120):
    c = d.close.values; o = d.open.values; h = d.high.values; lo = d.low.values; t = d.open_ms.values
    ma = {n: d.close.rolling(n).mean().values for n in (10, 20, 50, 200)}
    x = ma[line]
    out = []
    i = 201
    n = len(d)
    while i < n - 1:
        ok = c[i] < x[i] and c[i - 1] >= x[i - 1]
        if ok and cond == "200above":
            ok = max(ma[10][i], ma[20][i], ma[50][i]) < ma[200][i]
        if not ok:
            i += 1
            continue
        e = o[i + 1]; et = t[i + 1]
        dl = deadline(et) if time_exit else 1 << 62
        sp = e * (1 + stop / 100) if stop else None
        tg = e * (1 - tp / 100) if tp else None
        j = i + 1; px = None; why = "max"
        while j < n:
            if t[j] >= dl:
                px, why = o[j], "time"; break
            if sp and h[j] >= sp:
                px, why = max(sp, o[j]), "stop"; break
            if tg and lo[j] <= tg:
                px, why = min(tg, o[j]) if o[j] < tg else tg, "tp"; break
            if j - (i + 1) >= max_bars:
                px, why = c[j], "max"; break
            j += 1
        if px is None:
            break
        out.append((et, e / px - 1 - COST, why, j - i - 1))
        i = j
    return out


def show(rows, label):
    if not rows:
        print(f"{label:46s} 0건"); return
    a = np.array([r[1] for r in rows])
    ex5 = np.sort(a)[:-5].mean() if len(a) > 10 else float("nan")
    why = {k: sum(1 for r in rows if r[2] == k) for k in ("tp", "stop", "time", "max")}
    eq = 1.0; pk = 1.0; mdd = 0.0
    for r in sorted(rows, key=lambda r: r[0]):
        eq *= 1 + 2 * r[1]; pk = max(pk, eq); mdd = min(mdd, eq / pk - 1)
    mo = {}
    for r in rows:
        k = datetime.fromtimestamp(r[0] / 1000, ET).strftime(PERIOD)
        mo.setdefault(k, []).append(r[1])
    ms = " ".join(f"{k}:{np.mean(v)*100:+.1f}" for k, v in sorted(mo.items()))
    print(f"{label:46s} {len(a):4d}건 승률 {np.mean(a>0)*100:3.0f}% 거래당 {a.mean()*100:+.2f}% (5건 빼면 {ex5*100:+.2f}%) "
          f"익절{why['tp']} 손절{why['stop']} 시간{why['time']+why['max']} 2배복리 {(eq-1)*100:+.0f}% MDD {mdd*100:.0f}% | {ms}")


import sys
CRYPTO = "--crypto" in sys.argv  # BTC·ETH 2022~ (비용 왕복 0.1%, 연도별)
PERIOD = "%Y" if CRYPTO else "%m"
if CRYPTO:
    COST = 0.001
files = sorted(glob.glob(r"data\binance_1h_crypto\*_1h.csv" if CRYPTO else r"data\binance_1h\*_1h.csv"))
data = dict(load(f) for f in files)
exits = [("손절5·익절10·정규장마감", 5, 10, True), ("손절5·익절10·마감없음", 5, 10, False),
         ("정규장마감만", 0, 0, True)]
for cond in ("200above", "any"):
    print(f"\n=== 조건: {'200선이 10·20·50선 위' if cond=='200above' else '조건 없음(비교)'} ===")
    for line in (10, 20, 50):
        for name, sp, tp, te in exits:
            for one in (["BTCUSDT", "ETHUSDT"] if CRYPTO else ["SOXLUSDT"]):
                show(sim(data[one], line, cond, sp, tp, te), f"{one[:-4]} {line}선 이탈 {name}")
            allr = []
            pos = 0
            for sym, d in data.items():
                r = sim(d, line, cond, sp, tp, te)
                allr += r
                if r and np.mean([x[1] for x in r]) > 0:
                    pos += 1
            show(allr, f"  {'BTC+ETH' if CRYPTO else f'주식토큰{len(data)}'} {line}선 {name} (이익종목 {pos})")
