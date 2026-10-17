"""9:05 기준 상승률·거래대금 상위 종목이 상승분의 절반까지 되돌리면 매수, 9:05 까지 고점 익절, 손익비 1:2 손절, 15:10 정리.

  python scripts/half_retrace_905.py            (2026-07~09, reports/noon/noon_days.csv 1분봉 = 그날 고가 +5%↑·거래대금 100억↑ 종목)

상승분 = 9:05 까지 고가 - 기준가(전일 종가, 또는 오늘 시가). 매수가 = 기준가 + 상승분/2 (9:05 뒤 닿으면 지정가).
익절 = 9:05 까지 고가(전고점) 지정가. 손절 = 매수가 - (익절가 - 매수가)/2 (손실 1 : 이익 2). 15:10 시장가 정리.
매수한 봉에서 손절가를 건드리면 손절(보수적). 대상 목록이 '그날 결국 오른 종목'이라 결과가 좋게 나오는 쪽으로 치우침.
"""
from __future__ import annotations

import sys
from itertools import product
from pathlib import Path

sys.argv = sys.argv[:1] + ["09:05"]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import open_pullback_915 as op  # noqa: E402

COMM, TAX, SLIP = op.COMM, op.TAX, op.SLIP


def trade(c, base, rr, buy_until, exit_time, follow_high, min_rise):
    hi = c["hi"]
    pos = None
    for b in c["bars"]:
        if b.t <= "09:05":
            continue
        if pos is None:
            if b.t > buy_until:
                return None
            level = base + (hi - base) / 2
            if (hi / base - 1) * 100 < min_rise:
                return None
            if b.low <= level:
                pos = min(level, b.open)
                tp = hi
                sp = pos - (tp - pos) / rr
                et = b.t
                if b.low <= sp:
                    return et, pos, b.t, sp * (1 - SLIP), "stop"
                continue
            if follow_high and b.high > hi:
                hi = b.high  # 아직 안 샀으면 고점을 따라 올림
            continue
        if b.t >= exit_time:
            return et, pos, b.t, b.open * (1 - SLIP), "time"
        if b.low <= sp:
            return et, pos, b.t, min(sp, b.open) * (1 - SLIP), "stop"
        if b.high >= tp:
            return et, pos, b.t, tp, "tp"
    if pos is None:
        return None
    return et, pos, c["bars"][-1].t, c["bars"][-1].close * (1 - SLIP), "time"


def run(days, pick, basis="prev", rr=2, buy_until="15:00", exit_time="15:10", follow_high=False, min_rise=0, n=3):
    out = []
    for d in sorted(days):
        got = []
        for c in pick(days[d]):
            base = c["prev"] if basis == "prev" else c["open"]
            if c["hi"] <= base or c["px"] <= base + (c["hi"] - base) / 2:
                continue  # 오르지 않았거나 9:05 에 이미 절반 아래
            r = trade(c, base, rr, buy_until, exit_time, follow_high, min_rise)
            if r:
                et, ep, xt, xp, why = r
                ret = (xp * (1 - COMM - TAX) / (ep * (1 + COMM)) - 1) * 100
                got.append({"day": d.isoformat(), "code": c["code"], "name": c["name"], "et": et, "ret": ret, "why": why})
        got.sort(key=lambda t: t["et"])
        out += [dict(t, slot=i < n) for i, t in enumerate(got)]
    return out


def main():
    days = op.load_days()
    nd = len(days)
    tot = sum(len(v) for v in days.values())
    print(f"{min(days)} ~ {max(days)} {nd}거래일, 종목x날 {tot}개, 기준 시각 09:05")
    print("== 대상 (기준 전일 종가, 손익비 1:2, 15:00 까지 매수, 15:10 정리)")
    for k, x, n in product((10, 20, 50), (5, 10), (3, 0)):
        op.show(f"거래대금 {k}위 안·상승률 {x}%↑·{'상승률 순 3' if n else '전부'}", run(days, op.picker(k, x, n)), nd)
    for k in (3, 5, 10):
        op.show(f"상승률 상위 {k}", run(days, op.picker(k, 0, 0, need="chg")), nd)
        op.show(f"거래대금 상위 {k} (상승 중)", run(days, op.picker(k, 0, 0, need="amt")), nd)
    base = op.picker(20, 5, 3)
    print("== 변형 (거래대금 20위 안·상승률 5%↑·상승률 순 3종목)")
    op.show("기준 = 오늘 시가", run(days, base, basis="open"), nd)
    op.show("매수 전 새 고점이면 고점 따라 올림", run(days, base, follow_high=True), nd)
    for mr in (5, 10, 15):
        op.show(f"상승분 {mr}%↑ 만", run(days, base, min_rise=mr), nd)
    for rr in (1, 1.5, 3):
        op.show(f"손익비 1:{rr}", run(days, base, rr=rr), nd)
    for bu in ("09:30", "10:00", "11:00"):
        op.show(f"{bu} 까지만 매수", run(days, base, buy_until=bu), nd)
    op.show("12:00 정리", run(days, base, buy_until="11:30", exit_time="12:00"), nd)


if __name__ == "__main__":
    main()
