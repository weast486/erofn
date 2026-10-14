"""9:15 기준 거래대금 상위·상승률 상위 종목의 '오늘 시가 눌림' 매수 테스트.

  python scripts/open_pullback_915.py            (2026-07~09, reports/noon/noon_days.csv 의 1분봉 = 그날 고가 +5%↑·거래대금 100억↑ 종목 전부)

대상: 9:15 까지 누적 거래대금 순위 K 위 안 + 그때 상승률 X%↑ (+ 9:15 가격이 오늘 시가보다 위), 상승률 높은 순 N 종목.
매수: 9:15 뒤 가격이 오늘 시가까지 내려오면 시가 지정가. 손절 매수가 -S% (시장가) / 익절 +T% (지정가) / 정리 시각 시장가.
1분봉 시각은 봉이 끝나는 시각 ('09:15' 봉 = 9:14~9:15). 매수한 봉에서 손절가를 건드리면 손절로 봄(보수적), 그 봉의 익절은 안 봄.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tossbot.backtest import load_cache, load_minute  # noqa: E402

COMM, TAX, SLIP = 0.00015, 0.0020, 0.001
CUT = sys.argv[1] if len(sys.argv) > 1 else "09:15"


def load_days():
    data = load_cache(ROOT / "data/ohlcv_long")
    prev = {}
    for code, (name, bars) in data.items():
        for i in range(1, len(bars)):
            prev[(code, bars[i].day)] = (bars[i - 1].close, name)
    days = defaultdict(list)
    with open(ROOT / "reports/noon/noon_days.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d = date.fromisoformat(r["date"])
            p = prev.get((r["code"], d))
            m = load_minute(ROOT / "data/minute", r["code"], d)
            if not p or not m:
                continue
            early = [b for b in m if b.t <= CUT]
            if not early:
                continue
            days[d].append({"code": r["code"], "name": p[1], "prev": p[0], "open": m[0].open, "bars": m,
                            "amt": sum(b.close * b.volume for b in early), "px": early[-1].close,
                            "chg": (early[-1].close / p[0] - 1) * 100, "hi": max(b.high for b in early),
                            "lo": min(b.low for b in early)})
    for d, cs in days.items():
        for i, c in enumerate(sorted(cs, key=lambda c: -c["amt"]), 1):
            c["arank"] = i
        for i, c in enumerate(sorted(cs, key=lambda c: -c["chg"]), 1):
            c["crank"] = i
    return days


def trade(c, level, stop, tp, buy_until, exit_time, mode="pull"):
    """level = 매수 지정가. mode now = 9:15 바로 다음 봉 시가 매수(비교용)."""
    pos = None
    for b in c["bars"]:
        if b.t <= CUT:
            continue
        if pos is None:
            if b.t > buy_until:
                return None
            if mode == "now":
                pos = b.open * (1 + SLIP)
            elif b.low <= level:
                pos = min(level, b.open)
            else:
                continue
            et, sp, tpp = b.t, pos * (1 - stop / 100), pos * (1 + tp / 100) if tp else 0
            if stop and b.low <= sp:
                return et, pos, b.t, sp * (1 - SLIP), "stop"
            continue
        if b.t >= exit_time:
            return et, pos, b.t, b.open * (1 - SLIP), "time"
        if stop and b.low <= sp:
            return et, pos, b.t, min(sp, b.open) * (1 - SLIP), "stop"
        if tp and b.high >= tpp:
            return et, pos, b.t, tpp, "tp"
    if pos is None:
        return None
    return et, pos, c["bars"][-1].t, c["bars"][-1].close * (1 - SLIP), "time"


def run(days, pick, level_fn, stop, tp, buy_until="12:00", exit_time="12:00", n=3, mode="pull"):
    out = []
    for d in sorted(days):
        cs = pick(days[d])
        got = []
        for c in cs:
            lv = level_fn(c)
            if mode == "pull" and c["px"] <= lv:
                continue  # 이미 매수가 아래
            r = trade(c, lv, stop, tp, buy_until, exit_time, mode)
            if r:
                et, ep, xt, xp, why = r
                ret = (xp * (1 - COMM - TAX) / (ep * (1 + COMM)) - 1) * 100
                got.append({"day": d.isoformat(), "code": c["code"], "name": c["name"], "et": et, "ret": ret, "why": why})
        got.sort(key=lambda t: t["et"])
        out += [dict(t, slot=i < n) for i, t in enumerate(got)]
    return out


def show(label, tr, ndays, n=3):
    if not tr:
        print(f"{label:<46} 거래 없음")
        return
    rets = [t["ret"] for t in tr]
    cut = sorted(rets)[:-5] if len(rets) > 10 else rets
    eq = peak = 1.0
    mdd = 0.0
    mon = defaultdict(lambda: 1.0)
    byday = defaultdict(list)
    for t in tr:
        if t["slot"]:
            byday[t["day"]].append(t["ret"])
    for d in sorted(byday):
        r = sum(byday[d]) / n / 100
        eq *= 1 + r
        mon[d[:7]] *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    why = defaultdict(int)
    for t in tr:
        why[t["why"]] += 1
    ms = " / ".join(f"{(v - 1) * 100:+.0f}" for _, v in sorted(mon.items()))
    print(f"{label:<46} {len(tr):>4}건 승률 {sum(r > 0 for r in rets) / len(rets) * 100:>3.0f}% 거래당 {sum(rets) / len(rets):+.2f}% "
          f"(5건 빼면 {sum(cut) / len(cut):+.2f}%) 익절{why['tp']} 손절{why['stop']} 정리{why['time']} | "
          f"계좌 {(eq - 1) * 100:+.0f}% MDD {mdd * 100:.0f}% 월 {ms}")


def picker(k, x, n, by="chg", need="both"):
    def f(cs):
        if need == "both":
            sel = [c for c in cs if c["arank"] <= k and c["chg"] >= x]
        elif need == "amt":
            sel = [c for c in cs if c["arank"] <= k and c["chg"] > 0]
        else:
            sel = [c for c in cs if c["crank"] <= k]
        sel.sort(key=lambda c: -c[by])
        return sel[:n] if n else sel
    return f


def main():
    days = load_days()
    nd = len(days)
    tot = sum(len(v) for v in days.values())
    print(f"{min(days)} ~ {max(days)} {nd}거래일, 종목x날 {tot}개 (하루 평균 {tot / nd:.0f}), 기준 시각 {CUT}")
    op = lambda c: c["open"]  # noqa: E731
    base = picker(20, 5, 3)
    print("== 기본: 거래대금 20위 안 + 상승률 5%↑ 중 상승률 높은 3종목, 시가 눌림 매수")
    for stop, tp, ex in product((2, 3, 5, 0), (3, 5, 7, 0), ("12:00", "15:15")):
        show(f"손절 {stop} 익절 {tp} 정리 {ex}", run(days, base, op, stop, tp, "12:00" if ex == "12:00" else "14:30", ex), nd)
    print("== 대상 바꾸기 (손절 3 익절 5 정리 12:00, 뽑힌 종목 전부의 거래당 / 계좌는 먼저 체결된 3종목)")
    for k, x, n in product((5, 10, 20, 50), (3, 5, 10, 15), (3, 0)):
        show(f"거래대금 {k}위 안·상승률 {x}%↑·{'상승률 순 3종목' if n else '전부'}", run(days, picker(k, x, n), op, 3, 5), nd)
    for k in (3, 5, 10):
        show(f"거래대금 상위 {k} (상승 중이면)", run(days, picker(k, 0, 0, need="amt"), op, 3, 5), nd)
        show(f"상승률 상위 {k} (거래대금 무관)", run(days, picker(k, 0, 0, need="chg"), op, 3, 5), nd)
    show("거래대금 20위·5%↑ 중 거래대금 순 3종목", run(days, picker(20, 5, 3, by="amt"), op, 3, 5), nd)
    print("== 매수 마감 (기본 대상, 손절 3 익절 5 정리 12:00)")
    for bu in ("09:30", "10:00", "11:00"):
        show(f"{bu} 까지만 매수", run(days, base, op, 3, 5, bu), nd)
    print("== 눌림 위치 바꾸기 (기본 대상, 손절 3 익절 5 정리 12:00)")
    show("전일 종가까지 눌림", run(days, base, lambda c: c["prev"], 3, 5), nd)
    show("시가와 9:15 가격의 가운데", run(days, base, lambda c: (c["open"] + c["px"]) / 2, 3, 5), nd)
    show("9:15 가격 -2%", run(days, base, lambda c: c["px"] * 0.98, 3, 5), nd)
    show("9:15 가격 -3%", run(days, base, lambda c: c["px"] * 0.97, 3, 5), nd)
    show("9:15 까지 저가", run(days, base, lambda c: c["lo"], 3, 5), nd)
    show("눌림 안 기다리고 9:15 바로 매수", run(days, base, op, 3, 5, mode="now"), nd)
    print("== 시가 대비 9:15 위치별 (기본 대상 전부, 손절 3 익절 5)")
    for lo, hi in ((0, 2), (2, 5), (5, 10), (10, 99)):
        f = lambda cs, lo=lo, hi=hi: [c for c in picker(20, 5, 0)(cs) if lo <= (c["px"] / c["open"] - 1) * 100 < hi]  # noqa: E731
        show(f"9:15 가격이 시가보다 {lo}~{hi}% 위", run(days, f, op, 3, 5), nd)
    print("== 시가 갭별 (기본 대상 전부, 손절 3 익절 5)")
    for lo, hi in ((-99, 0), (0, 3), (3, 7), (7, 99)):
        f = lambda cs, lo=lo, hi=hi: [c for c in picker(20, 5, 0)(cs) if lo <= (c["open"] / c["prev"] - 1) * 100 < hi]  # noqa: E731
        show(f"시가 갭 {lo}~{hi}%", run(days, f, op, 3, 5), nd)


if __name__ == "__main__":
    main()
