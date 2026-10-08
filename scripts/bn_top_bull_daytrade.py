"""바이낸스 주식 토큰: 전날 정규장 거래대금 상위 N 종목 중 전날이 양봉인 종목에 국내 단타 규칙을 적용.

데이터: data/binance/{SYMBOL}_1m.csv.gz (바이낸스 선물 1분봉), 정규장 9:30~16:00 (미국 동부)만 사용
규칙 (scripts/us_kr_daytrade.py 의 trade 와 같음, 전날 종가 = 전날 정규장 마지막 봉 종가, 시가 = 9:30 봉 시가):
  A 시가가 전날 종가 아래 → 5분 안에 전날 종가를 넘으면 매수
  B 시가가 전날 종가 위 → 첫 5분 안에 전날 종가 이하로 내려간 적 있음 → 그 뒤 5분 안에 오늘 시가를 넘으면 매수
  손절 -10% / 익절 +7% / 장 시작 3시간 뒤 정리, 롱만, 수수료 0.05% + 슬리피지 0.05% (한쪽)

    python scripts/bn_top_bull_daytrade.py
"""
from __future__ import annotations

import csv
import gzip
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import us_kr_daytrade as K  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "binance"
ET = ZoneInfo("America/New_York")
EXCLUDE = {"SOXSUSDT", "SQQQUSDT", "SKDDUSDT"}  # 롱 짝이 있는 인버스 ETF (봇과 같게 뺌)


def load():
    """{종목: {날짜: [(분, o, h, l, c)]}}, {종목: {날짜: (시가, 종가, 거래대금)}}"""
    meta = json.loads((DATA / "symbols.json").read_text(encoding="utf-8"))["symbols"]
    eq = {s["symbol"] for s in meta if s.get("underlyingType") == "EQUITY"} - EXCLUDE
    mins, daily = {}, {}
    for sym in sorted(eq):
        p = DATA / f"{sym}_1m.csv.gz"
        if not p.exists():
            continue
        days = defaultdict(list)
        qv = defaultdict(float)
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                t = datetime.fromtimestamp(int(r["open_ms"]) / 1000, ET)
                m = t.hour * 60 + t.minute - 570
                if t.weekday() < 5 and 0 <= m < 390:
                    d = t.strftime("%Y-%m-%d")
                    days[d].append((m, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
                    qv[d] += float(r["quote_volume"])
        mins[sym] = dict(days)
        daily[sym] = {d: (b[0][1], b[-1][4], qv[d]) for d, b in days.items() if b[0][0] == 0 and len(b) >= 300}
    return mins, daily


def run(mins, daily, top=3, bull=True, pick="top_then_bull", min_qv=0.0, **kw):
    cal = sorted({d for v in daily.values() for d in v})
    out = []
    for i in range(1, len(cal)):
        p, d = cal[i - 1], cal[i]
        rank = sorted(((v[p][2], s) for s, v in daily.items() if p in v and v[p][2] >= min_qv), reverse=True)
        if pick == "bull_then_top" and bull:
            rank = [x for x in rank if daily[x[1]][p][1] > daily[x[1]][p][0]]
        for order, (qv, s) in enumerate(rank[:top], 1):
            o, c, _ = daily[s][p]
            if bull and c <= o:
                continue
            r = K.trade(mins[s].get(d, []), c, **kw)
            if r:
                out.append({"day": d, "sym": s, "kind": r[0], "min": r[1], "ret": r[2], "why": r[3], "rank": order,
                            "dv": qv, "pc": c})
    return out, len(cal) - 1


def months(rs):
    ms = sorted({r["day"][:7] for r in rs})
    return " / ".join(f"{m[5:]}월 {len(x)}건 {sum(r['ret'] for r in x) / len(x):+.2f}%" for m in ms
                      for x in [[r for r in rs if r['day'][:7] == m]])


def show(title, rs):
    a, b = [r for r in rs if r["kind"] == "A"], [r for r in rs if r["kind"] == "B"]
    print(f"{title}\n    전체 {K.stat(rs)}\n    A {K.stat(a)}\n    B {K.stat(b)}")


def acct(rs, slots=3, lev=1.0):
    s = K.account(rs, slots, lev)
    return s.split(" |")[0]


def main():
    mins, daily = load()
    print(f"종목 {len(daily)}개, 비용 한쪽 수수료 {K.FEE * 100:g}% + 슬리피지 {K.SLIP * 100:g}%\n")
    base, ndays = run(mins, daily)
    show(f"== 전날 거래대금 상위 3 중 전날 양봉, 국내 설정 (5분 안 매수, 손절 10 / 익절 7, 3시간 뒤 정리) — 거래일 {ndays}일", base)
    print("    월별:", months(base))
    print("    종목:", ", ".join(f"{s} {n}" for s, n in sorted(defaultdict(int, {s: sum(r['sym'] == s for r in base) for s in {r['sym'] for r in base}}).items(), key=lambda x: -x[1])[:10]))
    for lev in (1, 2, 5):
        print(f"    계좌 3종목 x 33% x {lev}배: {acct(base, 3, lev)}")
    print()
    show("== 양봉 조건 없이 (상위 3 전부)", run(mins, daily, bull=False)[0])
    nb = [r for r in run(mins, daily, bull=False)[0] if (r["day"], r["sym"]) not in {(x["day"], x["sym"]) for x in base}]
    show("== 전날 음봉이던 종목만 (비교)", nb)
    show("== 양봉인 종목 중에서 거래대금 상위 3", run(mins, daily, pick="bull_then_top")[0])
    print("\n== 상위 N (전날 양봉)")
    for n in (1, 2, 3, 5, 10, 20):
        rs = run(mins, daily, top=n)[0]
        print(f"  상위 {n:2d}: {K.stat(rs)} | A {K.stat([r for r in rs if r['kind'] == 'A']).split(' (')[0]} | "
              f"B {K.stat([r for r in rs if r['kind'] == 'B']).split(' (')[0]} | 계좌 1배 {acct(rs)}")
    print("\n== 매수 마감 (상위 3·양봉)")
    for bm in (2, 5, 10, 15, 30, 60):
        rs = run(mins, daily, buy_minutes=bm)[0]
        print(f"  {bm:2d}분: {K.stat(rs)} | A {K.stat([r for r in rs if r['kind'] == 'A']).split(' (')[0]} | "
              f"B {K.stat([r for r in rs if r['kind'] == 'B']).split(' (')[0]}")
    print("\n== 손절 / 익절 / 정리 (상위 3·양봉, 5분)")
    for st, tp, ex in ((10, 7, 180), (5, 7, 180), (3, 7, 180), (2, 4, 180), (3, 5, 180), (5, 5, 180), (3, 3, 180), (2, 2, 180),
                       (10, 7, 60), (10, 7, 385), (3, 5, 385), (100, 100, 180), (100, 100, 385)):
        rs = run(mins, daily, stop=st, tp=tp, exit_minutes=ex)[0]
        print(f"  손절 {st} 익절 {tp} 정리 {ex}분: {K.stat(rs)} | 계좌 1배 {acct(rs)}")


if __name__ == "__main__":
    main()
