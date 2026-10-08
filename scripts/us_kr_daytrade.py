"""국내 단타 규칙(키움 봇)을 미국 주식(바이낸스 상장 종목)에 그대로 적용해 보는 계산.

데이터: data/us_surge (바이낸스 주식 토큰 172종목의 실제 미국 주식, 전날 +5%↑ 다음 날 정규장 1분봉, 2023-01~2026-10)
규칙 (시각은 미국 동부, 한국 9:00 = 미국 9:30):
  A 재돌파: 전날 +N%↑ 종목이 시가가 전날 종가 아래에서 시작 → 장 시작 뒤 buy_minutes 분 안에 전날 종가를 넘으면 매수
  B 갭상승: 시가가 전날 종가 위에서 시작 → 첫 dip_minutes 분 안에 전날 종가 이하로 내려간 적이 있고,
            그 뒤(다음 봉부터) buy_minutes 분 안에 오늘 시가를 넘으면 매수
  손절 -stop% 시장가 / 익절 +tp% 지정가 / 장 시작 exit_minutes 분 뒤 정리, 롱만
비용: 바이낸스 선물 기준 수수료 한쪽 0.05% + 슬리피지 한쪽 0.05% (펀딩비는 당일 정리라 없음)

    python scripts/us_kr_daytrade.py
"""
from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / "data" / "us_surge"
FEE, SLIP = 0.0005, 0.0005
_bars: dict[tuple[str, str], list] = {}


def bars(sym: str, day: str):
    """[(장 시작 뒤 몇 분째, o, h, l, c)] 정규장만."""
    k = (sym, day)
    if k not in _bars:
        p = D / "minute" / f"{sym}_{day}.csv"
        out = []
        if p.exists():
            with open(p, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    hh, mm = int(r["time_et"][11:13]), int(r["time_et"][14:16])
                    m = hh * 60 + mm - 570
                    if 0 <= m < 390:
                        out.append((m, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
        _bars[k] = out
    return _bars[k]


def trade(b, pc, buy_minutes=5, dip_minutes=5, stop=10.0, tp=7.0, exit_minutes=180, rules="AB"):
    """(규칙, 진입 분, 수익률 %, 사유) 또는 None."""
    if not b or b[0][0] > 0:
        return None  # 9:30 첫 1분에 거래가 없던 날 (시가를 알 수 없음)
    op = b[0][1]
    if not 0.4 < op / pc < 2.5:
        return None  # 전날 종가와 시가가 너무 다름 (분할·병합 등으로 가격 기준이 안 맞는 날)
    entry = None
    if op < pc and "A" in rules:
        for j, (m, o, h, l, c) in enumerate(b):
            if m >= buy_minutes:
                break
            if h > pc:
                entry = ("A", j, max(o, pc))
                break
    elif op > pc and "B" in rules:
        dipped = False
        for j, (m, o, h, l, c) in enumerate(b):
            if m >= buy_minutes or (not dipped and m >= dip_minutes):
                break
            if dipped and h > op:
                entry = ("B", j, max(o, op))
                break
            if l <= pc:
                dipped = True
    if not entry:
        return None
    kind, j, px = entry
    px *= 1 + SLIP
    s, t = px * (1 - stop / 100), px * (1 + tp / 100)
    out = None
    if b[j][4] <= s:  # 매수한 봉이 손절가 아래에서 끝남
        out = (min(s, b[j][4]) * (1 - SLIP), "stop")
    else:
        for m, o, h, l, c in b[j + 1:]:
            if m >= exit_minutes:
                out = (o * (1 - SLIP), "time")
                break
            if l <= s:
                out = (min(o, s) * (1 - SLIP), "stop")
                break
            if h >= t:
                out = (max(o, t), "tp")
                break
        if out is None:
            out = (b[-1][4] * (1 - SLIP), "time")
    ret = (out[0] * (1 - FEE) / (px * (1 + FEE)) - 1) * 100
    return kind, b[j][0], ret, out[1]


def load_events():
    with open(D / "days.csv", encoding="utf-8") as f:
        return [dict(r, chg=float(r["prev_change_pct"]), pc=float(r["prev_close"]), dv=float(r["prev_dollar_volume"]))
                for r in csv.DictReader(f)]


def onboard_dates() -> dict[str, str]:
    """바이낸스 상장일 {주식 티커: YYYY-MM-DD} (data/binance/symbols.json 이 있으면)."""
    p = ROOT / "data" / "binance" / "symbols.json"
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("symbols", [])
    out = {}
    for s in rows:
        if isinstance(s, dict) and s.get("onboardDate"):
            d = datetime.fromtimestamp(int(s["onboardDate"]) / 1000, timezone.utc).strftime("%Y-%m-%d")
            out[str(s.get("symbol", "")).replace("USDT", "")] = d
    return out


def run(events, thr=20.0, min_dv=1e6, **kw):
    out = []
    for e in events:
        if e["chg"] < thr or e["dv"] < min_dv:
            continue
        r = trade(bars(e["symbol"], e["event_date"]), e["pc"], **kw)
        if r:
            out.append({"day": e["event_date"], "sym": e["symbol"], "kind": r[0], "min": r[1], "ret": r[2], "why": r[3],
                        "dv": e["dv"], "pc": e["pc"]})
    return out


def stat(rs):
    if not rs:
        return "0건"
    n = len(rs)
    return (f"{n}건 {sum(r['ret'] for r in rs) / n:+.2f}% 승률 {sum(r['ret'] > 0 for r in rs) / n * 100:.0f}% "
            f"(익절 {sum(r['why'] == 'tp' for r in rs)} 손절 {sum(r['why'] == 'stop' for r in rs)})")


def years(rs):
    return " / ".join(f"{y} {stat([r for r in rs if r['day'][:4] == y]).split(' 승률')[0]}" for y in ("2023", "2024", "2025", "2026"))


def account(rs, slots=3, lev=1.0):
    by = defaultdict(list)
    for r in rs:
        by[r["day"]].append(r)
    eq = peak = 1.0
    mdd = worst = 0.0
    ys = {}
    for d in sorted(by):
        day = sorted(by[d], key=lambda r: (r["min"], r["kind"]))[:slots]
        g = max(0.0, 1 + sum(r["ret"] for r in day) / 100 / slots * lev)
        worst = min(worst, g - 1)
        eq *= g
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        e = ys.setdefault(d[:4], [1.0])
        e[0] *= g
    return (f"연속 {eq * 100 - 100:+.0f}% MDD {mdd * 100:.0f}% 최악일 {worst * 100:.1f}% | "
            + " / ".join(f"{y} {v[0] * 100 - 100:+.0f}%" for y, v in ys.items()))


def main():
    global D
    args = list(sys.argv[1:])
    if "--data" in args:  # 예: --data data/us_market (미국 시장 전체, download_us_market_surge.py)
        i = args.index("--data")
        D = ROOT / args[i + 1]
        del args[i:i + 2]
    sys.argv[1:] = args
    ev = load_events()
    print(f"이벤트 {len(ev)}건 (전날 +5%↑), 비용 한쪽 수수료 {FEE * 100:g}% + 슬리피지 {SLIP * 100:g}%\n")
    print("== 국내와 같은 설정: 전날 +20%↑, 5분 안 매수, 손절 10 / 익절 7, 3시간 뒤 정리")
    base = run(ev)
    for k, name in (("A", "A 재돌파(갭하락 시작)"), ("B", "B 갭상승→전날 종가 이하→시가 돌파")):
        rs = [r for r in base if r["kind"] == k]
        print(f"  {name}: {stat(rs)}\n      {years(rs)}")
    print("  합친 계좌 3종목 x 33% (1배):", account(base))
    print("\n== 전날 상승률 기준별 (나머지 같음)")
    for thr in (10, 15, 20, 25, 30):
        rs = run(ev, thr=thr)
        print(f"  +{thr}%↑  A {stat([r for r in rs if r['kind'] == 'A'])} | B {stat([r for r in rs if r['kind'] == 'B'])}")
        print(f"         A {years([r for r in rs if r['kind'] == 'A'])}")
        print(f"         B {years([r for r in rs if r['kind'] == 'B'])}")
    print("\n== 매수 마감 (전날 +20%↑)")
    for bm in (2, 5, 10, 15, 30):
        rs = run(ev, buy_minutes=bm)
        print(f"  {bm}분  A {stat([r for r in rs if r['kind'] == 'A'])} | B {stat([r for r in rs if r['kind'] == 'B'])}")
    print("\n== 전날 거래대금·주가별 (전날 +20%↑, 국내 설정)")
    for lo, hi in ((1e6, 3e7), (3e7, 1e8), (1e8, 3e8), (3e8, 1e13)):
        rs = [r for r in base if lo <= r["dv"] < hi]
        print(f"  거래대금 {lo / 1e6:g}~{hi / 1e6:g}백만$  A {stat([r for r in rs if r['kind'] == 'A']).split(' (')[0]} | "
              f"B {stat([r for r in rs if r['kind'] == 'B']).split(' (')[0]}")
    for lo, hi in ((0, 2), (2, 5), (5, 20), (20, 1e9)):
        rs = [r for r in base if lo <= r["pc"] < hi]
        print(f"  전날 종가 {lo:g}~{hi:g}$  A {stat([r for r in rs if r['kind'] == 'A']).split(' (')[0]} | "
              f"B {stat([r for r in rs if r['kind'] == 'B']).split(' (')[0]}")
    print("\n== 손절 / 익절 / 정리 (전날 +20%↑, 5분)")
    for st, tp, ex in ((10, 7, 180), (7, 7, 180), (5, 7, 180), (5, 5, 180), (7, 5, 180), (10, 5, 180), (10, 10, 180), (15, 7, 180),
                       (10, 7, 150), (10, 7, 385), (7, 5, 150)):
        rs = run(ev, stop=st, tp=tp, exit_minutes=ex)
        print(f"  손절 {st} 익절 {tp} 정리 {ex}분  A {stat([r for r in rs if r['kind'] == 'A']).split(' (')[0]} | "
              f"B {stat([r for r in rs if r['kind'] == 'B']).split(' (')[0]} | 계좌 {account(rs).split(' |')[0]}")
    ob = onboard_dates()
    if ob:
        print("\n== 바이낸스 상장 뒤의 거래만 (전날 +20%↑, 국내 설정)")
        rs = [r for r in base if ob.get(r["sym"], "9999") <= r["day"]]
        print(f"  A {stat([r for r in rs if r['kind'] == 'A'])} | B {stat([r for r in rs if r['kind'] == 'B'])}")
        rs10 = [r for r in run(ev, thr=10) if ob.get(r["sym"], "9999") <= r["day"]]
        print(f"  (+10%↑) A {stat([r for r in rs10 if r['kind'] == 'A'])} | B {stat([r for r in rs10 if r['kind'] == 'B'])}")
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(base[0]))
            w.writeheader()
            w.writerows(base)


if __name__ == "__main__":
    main()
