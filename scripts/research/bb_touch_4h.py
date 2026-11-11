import sys, glob, zipfile, io, os
import pandas as pd, numpy as np
D = sys.argv[1]
COST = 0.001

def load(sym):
    fr = []
    for f in sorted(glob.glob(f"{D}/{sym}/*.zip")):
        z = zipfile.ZipFile(f); raw = z.read(z.namelist()[0]).decode()
        hdr = None if raw[:1].isdigit() else 0
        df = pd.read_csv(io.StringIO(raw), header=hdr).iloc[:, :6]
        df.columns = ["t", "o", "h", "l", "c", "v"]
        fr.append(df)
    df = pd.concat(fr).drop_duplicates("t").sort_values("t")
    df["t"] = pd.to_datetime(df.t, unit="ms")
    df = df.set_index("t").astype(float)
    m = df.c.rolling(20).mean(); sd = df.c.rolling(20).std(ddof=int(os.environ.get("DDOF","0")))
    df["mid"], df["up"], df["lo"] = m, m + 2 * sd, m - 2 * sd
    df["out"] = ((df.c > df.up) | (df.c < df.lo)).astype(int)
    return df

def sim(df, mode, nmin, stop_kind="siglow", R=2.0, start="2022-01-01"):
    """mode close: 신호 봉 종가 상단 밖 → 다음 봉 시가 / touch: 직전 완성 봉 기준 상단 가격에 장중 닿으면 그 가격(갭이면 시가)."""
    o, h, l, c = df.o.values, df.h.values, df.l.values, df.c.values
    up, mid, out = df.up.values, df.mid.values, df.out.values
    idx = df.index; n = len(df); i0 = idx.searchsorted(pd.Timestamp(start))
    trades = []; i = max(i0, 41)
    while i < n - 1:
        cnt = out[i - 20:i].sum()  # 직전 20개 완성 봉
        if mode == "close":
            if VAR == "a": ok = c[i] > up[i] and out[i - 20:i].sum() >= nmin                       # 직전 20봉 N개↑
            elif VAR == "b": ok = c[i] > up[i] and out[i - 19:i + 1].sum() >= nmin                # 신호 봉 포함 20봉
            else: ok = c[i] > up[i] and c[i - 1] <= up[i - 1] and out[i - 20:i].sum() >= nmin  # 새로 밖 + 직전 20봉
            if not ok: i += 1; continue
            e_i, entry = i + 1, o[i + 1]
            stop = l[i] if stop_kind == "siglow" else mid[i]
            first = e_i
        else:
            lvl = up[i - 1]  # 직전 완성 봉의 상단
            if not (h[i] >= lvl and cnt >= nmin and c[i - 1] <= up[i - 1]):
                i += 1; continue
            e_i, entry = i, max(o[i], lvl)
            stop = l[i - 1] if stop_kind == "siglow" else mid[i - 1]
            first = i
        if not np.isfinite(stop) or stop >= entry:
            i += 1; continue
        tp = entry + R * (entry - stop)
        res = None; j = first
        while j < n:
            if j == first and mode == "touch":
                lo_, hi_ = l[j], h[j]   # 진입 뒤 같은 봉: 손절가 닿으면 손절로 (보수적)
            else:
                lo_, hi_ = l[j], h[j]
                if o[j] <= stop: res = (j, o[j]); break
                if o[j] >= tp: res = (j, o[j]); break
            if lo_ <= stop: res = (j, stop); break
            if hi_ >= tp: res = (j, tp); break
            j += 1
        if res is None: break
        j, px = res
        trades.append(dict(entry_t=idx[e_i], exit_t=idx[j], ret=px / entry - 1 - COST, risk=(entry - stop) / entry))
        i = j + 1 if mode == "close" else j + 1
    return pd.DataFrame(trades)

def report(lab, T):
    if T.empty: print(lab, "no trades"); return
    s = T.ret.sort_values()
    yr = T.groupby(T.entry_t.dt.year).ret.mean()
    eq = 1.0; peak = 1.0; mdd = 0
    for r in T.sort_values("entry_t").ret:  # 거래마다 1/2 (두 종목) 근사
        eq *= 1 + r / 2; peak = max(peak, eq); mdd = min(mdd, eq / peak - 1)
    print(f"{lab:38s} {len(T):4d}건 승률 {(T.ret>0).mean():.0%} 거래당 {T.ret.mean():+.2%} (5건 빼면 {s.iloc[:-5].mean():+.2%}) 손절폭 {T.risk.median():.1%} 계좌 {eq-1:+.0%} MDD {mdd:.0%} | " + " ".join(f"{y%100}:{v:+.2%}" for y, v in yr.items()))

import os
VAR = os.environ.get("VAR", "a")
data = {s: load(s) for s in ["BTCUSDT", "ETHUSDT"]}
print({s: (d.index[0], d.index[-1], len(d)) for s, d in data.items()})
for mode in os.environ.get("MODES", "close touch").split():
    for stop_kind in os.environ.get("STOPS", "siglow mid").split():
        for nmin in [int(x) for x in os.environ.get("NS", "0 3 5 7").split()]:
            T = pd.concat([sim(d, mode, nmin, stop_kind).assign(sym=s) for s, d in data.items()])
            report(f"{mode:5s} 손절={stop_kind:6s} 이탈{nmin}개↑", T)
            if mode=="touch" and stop_kind=="siglow" and nmin==5:
                for s in data: report(f"   {s}", T[T.sym==s])
