"""상승 장악형 캔들 다음 봉 시가 매수 백테스트 (2026-11-19 사용자 요청).

규칙: 앞 봉 음봉 · 이번 봉 양봉이 앞 몸통을 감쌈(종가 > 앞 시가, 시가 <= 앞 종가 + 0.05%)
      + 거래량 > 앞 봉 (오더블록 화면과 같은 장악형 정의, --no-vol 이면 거래량 안 봄)
      → 다음 봉 시가 롱, 손절 = 장악 봉 저가, 익절 = 진입가 + 손절폭 x 5,
      진입 봉 포함 최대 14봉 (14번째 봉 종가 정리). 같은 봉에서 둘 다 닿으면 손절.
      종목마다 한 번에 하나. 수수료 한쪽 0.05%.
--side short 이면 거울(하락 장악형 → 숏, 손절 장악 봉 고가).
데이터: data/binance_5m (주식 토큰 41) + data/binance_5m_crypto (BTC·ETH).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
KORU_SPLIT_MS = 1784108100000  # KORU 2026-07-15 20:1 분할 (바이낸스 봉에 반영 안 됨)


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
            x["h"] = max(x["h"], b["h"]); x["l"] = min(x["l"], b["l"]); x["c"] = b["c"]; x["v"] += b["v"]
        else:
            out.append({"t": k, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]})
    return out

SEC = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def rsi_list(bars, n=14):
    out = [None] * len(bars)
    ag = al = 0.0
    for i in range(1, len(bars)):
        d = bars[i]["c"] - bars[i - 1]["c"]
        g, l = max(d, 0.0), max(-d, 0.0)
        if i <= n:
            ag += g / n; al += l / n
            if i < n:
                continue
        else:
            ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def run(bars, side, tp_r, hold, fee, vol, tol=0.05, rsi_max=None):
    out = []
    n = len(bars)
    rs = rsi_list(bars) if rsi_max is not None else None
    i = 1
    sgn = 1 if side == "long" else -1
    while i < n - 1:
        p, b = bars[i - 1], bars[i]
        if sgn > 0:
            ok = p["c"] < p["o"] and b["c"] > b["o"] and b["c"] > p["o"] and b["o"] <= p["c"] * (1 + tol / 100)
        else:
            ok = p["c"] > p["o"] and b["c"] < b["o"] and b["c"] < p["o"] and b["o"] >= p["c"] * (1 - tol / 100)
        if vol and not b["v"] > p["v"]:
            ok = False
        if ok and rs is not None:  # 장악 봉 종가 RSI(14) 조건 (숏은 100-rsi_max 이상)
            r = rs[i]
            ok = r is not None and (r <= rsi_max if sgn > 0 else r >= 100 - rsi_max)
        if not ok:
            i += 1
            continue
        e = bars[i + 1]["o"]
        stop = b["l"] if sgn > 0 else b["h"]
        risk = (e - stop) * sgn
        if risk <= 0:
            i += 1
            continue
        tp = e + sgn * risk * tp_r
        x = None
        j = i + 1
        last = min(n - 1, i + hold)
        while j <= last:
            c = bars[j]
            if (sgn > 0 and c["l"] <= stop) or (sgn < 0 and c["h"] >= stop):
                x = stop if j > i + 1 or True else stop
                # 갭으로 손절가를 건너뛰면 시가
                if (sgn > 0 and c["o"] < stop) or (sgn < 0 and c["o"] > stop):
                    x = c["o"]
                why = "stop"
                break
            if (sgn > 0 and c["h"] >= tp) or (sgn < 0 and c["l"] <= tp):
                x = max(tp, c["o"]) if sgn > 0 else min(tp, c["o"])
                why = "tp"
                break
            j += 1
        if x is None:
            j = last
            x = bars[j]["c"]
            why = "time"
        ret = (x / e - 1) * 100 * sgn - 2 * fee
        out.append((bars[i + 1]["t"], ret, (x - e) * sgn / risk, why, risk / e * 100))
        i = j + 1
    return out


def summ(tr):
    n = len(tr)
    if not n:
        return "  거래 없음"
    r = sorted(x[1] for x in tr)
    ex5 = sum(r[:-5]) / (n - 5) if n > 5 else float("nan")
    w = sum(x > 0 for x in r) / n * 100
    rr = sum(max(-3, min(10, x[2])) for x in tr) / n
    tp = sum(x[3] == "tp" for x in tr); st = sum(x[3] == "stop" for x in tr); tm = n - tp - st
    risk = sorted(x[4] for x in tr)[n // 2]
    return (f"{n:>7}건 승률 {w:4.0f}% 거래당 {sum(r)/n:+.3f}% (5건뺌 {ex5:+.3f}) R {rr:+.2f} "
            f"익절/손절/시간 {tp}/{st}/{tm} 손절폭중간 {risk:.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", nargs="+", default=["5m", "15m", "1h", "4h", "1d"])
    ap.add_argument("--side", default="long", choices=["long", "short"])
    ap.add_argument("--tp-r", type=float, default=5)
    ap.add_argument("--hold", type=int, default=14)
    ap.add_argument("--fee", type=float, default=0.05)
    ap.add_argument("--no-vol", action="store_true")
    ap.add_argument("--rsi-max", type=float, default=None, help="장악 봉 종가 RSI(14) 가 이 값 이하일 때만 (숏은 100-값 이상)")
    a = ap.parse_args()
    files = [("stock", p) for p in sorted((ROOT / "data" / "binance_5m").glob("*_5m.csv"))]
    files += [("crypto", p) for p in sorted((ROOT / "data" / "binance_5m_crypto").glob("*_5m.csv"))]
    data = [(g, p.stem.replace("_5m", ""), load_5m(p)) for g, p in files]
    print(f"규칙: {a.side} 다음 봉 시가 · 손절 장악 봉 {'저가' if a.side == 'long' else '고가'} · 익절 {a.tp_r}R · 최대 {a.hold}봉 · 거래량 조건 {'없음' if a.no_vol else '있음'} · RSI 상한 {a.rsi_max}")
    for tf in a.tf:
        res = {"stock": [], "crypto": []}
        pos = {"stock": 0, "crypto": 0}
        cnt = {"stock": 0, "crypto": 0}
        for g, sym, b5 in data:
            bars = b5 if tf == "5m" else resample(b5, SEC[tf])
            tr = run(bars, a.side, a.tp_r, a.hold, a.fee, not a.no_vol, rsi_max=a.rsi_max)
            res[g] += tr
            cnt[g] += 1
            pos[g] += sum(x[1] for x in tr) > 0
        print(f"\n== {tf} ==")
        for g, name in (("stock", "주식 토큰"), ("crypto", "BTC·ETH")):
            print(f" {name:<6}{summ(res[g])} 이익종목 {pos[g]}/{cnt[g]}")
            if g == "crypto" and res[g]:
                yr = {}
                for x in res[g]:
                    yr.setdefault(dt.datetime.fromtimestamp(x[0], dt.timezone.utc).year, []).append(x[1])
                print("   해마다 " + " / ".join(f"{y} {len(v)}건 {sum(v)/len(v):+.2f}%" for y, v in sorted(yr.items())))


if __name__ == "__main__":
    main()
