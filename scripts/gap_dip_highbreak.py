"""갭상승 시작 → 9:05 까지 시가 아래로 내려감 → 9:10 전까지 그날 고가를 다시 넘으면 매수.

  python scripts/gap_dip_highbreak.py

대상 두 가지 (받아 둔 1분봉으로 가능한 것):
  U1  전일 +20%↑ 종목의 갭상승 날 (2023-09~, 키움 봇 대상과 같은 종목군, 미래 정보 없음)
  U2  2026-07~09 그날 고가 +5%↑·거래대금 100억↑ 종목 (noon 목록). 목록 자체가 '그날 +5% 넘은 종목'이라
      돌파 가격이 이미 전일 대비 +5% 이상인 경우만 씀 (그래야 미래 정보가 안 섞임)
1분봉 시각은 봉이 끝나는 시각. 내려간 봉보다 뒤의 봉에서 그때까지 고가를 넘는 순간 매수(고가 + 슬리피지 0.1%).
매수한 봉에서는 종가 기준으로만 손절 확인, 그 뒤 봉은 손절 먼저.
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tossbot.backtest import SELL_TAX_BY_YEAR, load_cache, load_minute, surge_break_days  # noqa: E402

COMM, SLIP = 0.00015, 0.001
MIN = ROOT / "data/minute"


def signal(bars, prev, dip_until="09:05", buy_until="09:10", min_dip=0.0, dip_ref="open"):
    """(매수 봉 index, 매수가, 내려간 저가) 또는 None."""
    op = bars[0].open
    if op <= prev:
        return None
    ref = op if dip_ref == "open" else prev
    hi, lo, dipped = 0.0, 1e18, False
    for i, b in enumerate(bars):
        if b.t > buy_until:
            return None
        if dipped and i > 0 and b.high > hi:
            return i, max(hi, b.open), lo
        hi, lo = max(hi, b.high), min(lo, b.low)
        if b.t <= dip_until and lo < ref * (1 - min_dip / 100) and (dip_ref == "open" or lo <= ref):
            dipped = True
    return None


def trade(bars, i, price, d, stop=10.0, tp=7.0, exit_time="12:00", stop_price=0.0):
    ep = price * (1 + SLIP)
    sp = stop_price or (ep * (1 - stop / 100) if stop else 0)
    tpp = ep * (1 + tp / 100) if tp else 0
    tax = SELL_TAX_BY_YEAR.get(d.year, 0.0020)
    xp, why = None, "time"
    for k in range(i, len(bars)):
        b = bars[k]
        if k == i:
            if sp and b.close <= sp:
                xp, why = b.close * (1 - SLIP), "stop"
                break
            continue
        if b.t >= exit_time:
            xp = b.open * (1 - SLIP)
            break
        if sp and b.low <= sp:
            xp, why = min(sp, b.open) * (1 - SLIP), "stop"
            break
        if tpp and b.high >= tpp:
            xp, why = tpp, "tp"
            break
    if xp is None:
        xp = bars[-1].close * (1 - SLIP)
    return (xp * (1 - COMM - tax) / (ep * (1 + COMM)) - 1) * 100, why


def collect(universe, **sig):
    """[(day, cand, bars, i, price, low)]"""
    out = []
    for d, c, bars in universe:
        s = signal(bars, c["prev_close"], **sig)
        if s:
            out.append((d, c, bars, *s))
    return out


def show(label, sigs, n_days, stop=10.0, tp=7.0, exit_time="12:00", low_stop=False, flt=None):
    tr = []
    for d, c, bars, i, price, low in sigs:
        if flt and not flt(c, bars, i, price, low):
            continue
        r, why = trade(bars, i, price, d, stop, tp, exit_time, low * 0.999 if low_stop else 0.0)
        tr.append((d, bars[i].t, r, why))
    if not tr:
        print(f"{label:<44} 거래 없음")
        return
    rets = [t[2] for t in tr]
    cut = sorted(rets)[:-5] if len(rets) > 10 else rets
    byday = defaultdict(list)
    for t in tr:
        byday[t[0]].append(t)
    eq = peak = 1.0
    mdd = 0.0
    yr = defaultdict(lambda: 1.0)
    for d in sorted(byday):
        r = sum(t[2] for t in sorted(byday[d], key=lambda t: t[1])[:3]) / 3 / 100
        eq *= 1 + r
        yr[d.year] *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    w = defaultdict(int)
    for t in tr:
        w[t[3]] += 1
    ys = " / ".join(f"{y} {(v - 1) * 100:+.0f}" for y, v in sorted(yr.items()))
    print(f"{label:<44} {len(tr):>4}건 승률 {sum(r > 0 for r in rets) / len(rets) * 100:>3.0f}% 거래당 {sum(rets) / len(rets):+.2f}% "
          f"(5건 빼면 {sum(cut) / len(cut):+.2f}%) 익절{w['tp']} 손절{w['stop']} 정리{w['time']} | "
          f"거래한 날 {len(byday)}/{n_days} 계좌 {(eq - 1) * 100:+.0f}% MDD {mdd * 100:.0f}% | {ys}")


def grid(name, uni, n_days, entry_ok=None):
    print(f"\n===== {name}: 종목x날 {len(uni)}개, {n_days}거래일")
    base = collect(uni)
    f = entry_ok
    show("기본 (손절 10 익절 7 12시)", base, n_days, flt=f)
    for stop, tp in ((3, 3), (3, 5), (5, 5), (5, 7), (7, 7), (10, 5), (10, 10), (0, 7), (10, 0)):
        show(f"  손절 {stop} 익절 {tp}", base, n_days, stop, tp, flt=f)
    for tp in (3, 5, 7, 0):
        show(f"  손절 = 내려간 저가, 익절 {tp}", base, n_days, 0, tp, low_stop=True, flt=f)
    for ex in ("10:00", "11:00", "15:15"):
        show(f"  정리 {ex}", base, n_days, exit_time=ex, flt=f)
    print("  -- 시가 아래로 내려간 깊이")
    for md in (1, 2, 3, 5):
        show(f"  시가 -{md}% 넘게 내려감", collect(uni, min_dip=md), n_days, flt=f)
    show("  전일 종가 이하까지 내려감", collect(uni, dip_ref="prev"), n_days, flt=f)
    print("  -- 시간")
    show("  9:03 까지 하락·9:05 전 돌파", collect(uni, dip_until="09:03", buy_until="09:05"), n_days, flt=f)
    show("  9:05 까지 하락·9:15 전 돌파", collect(uni, buy_until="09:15"), n_days, flt=f)
    show("  9:05 까지 하락·9:30 전 돌파", collect(uni, buy_until="09:30"), n_days, flt=f)
    show("  9:10 까지 하락·9:30 전 돌파", collect(uni, dip_until="09:10", buy_until="09:30"), n_days, flt=f)
    show("  9:30 까지 하락·10:00 전 돌파", collect(uni, dip_until="09:30", buy_until="10:00"), n_days, flt=f)
    print("  -- 시가 갭 / 돌파 가격 위치")
    for lo, hi in ((0, 2), (2, 5), (5, 10), (10, 99)):
        g = lambda c, bars, i, p, low, lo=lo, hi=hi: lo <= (bars[0].open / c["prev_close"] - 1) * 100 < hi and (not f or f(c, bars, i, p, low))  # noqa: E731
        show(f"  시가 갭 {lo}~{hi}%", base, n_days, flt=g)
    for lo, hi in ((0, 5), (5, 10), (10, 15), (15, 99)):
        g = lambda c, bars, i, p, low, lo=lo, hi=hi: lo <= (p / c["prev_close"] - 1) * 100 < hi and (not f or f(c, bars, i, p, low))  # noqa: E731
        show(f"  돌파 가격이 전일 대비 +{lo}~{hi}%", base, n_days, flt=g)


def main():
    data = load_cache(ROOT / "data/ohlcv_long")
    # U1: 전일 +20%↑
    days = surge_break_days(data, date(2023, 9, 1), None, 20, 3e9, 100_000)
    u1, miss = [], 0
    for d, cs in days.items():
        for c in cs:
            bars = load_minute(MIN, c["code"], d)
            if not bars:
                miss += 1
                continue
            u1.append((d, c, bars))
    gap_up = [u for u in u1 if u[2][0].open > u[1]["prev_close"]]
    print(f"U1 전일 +20%↑: 대상 {len(u1) + miss}건 중 분봉 있음 {len(u1)} (없음 {miss}), 갭상승 시작 {len(gap_up)}건")
    grid("U1 전일 +20%↑ 갭상승 (2023-09~2026-10)", gap_up, len(days))
    for y in (2023, 2024, 2025, 2026):
        show(f"  {y}년", collect([u for u in gap_up if u[0].year == y]), len([d for d in days if d.year == y]))

    # U2: noon 목록
    prev = {(code, bars[i].day): (bars[i - 1].close, name) for code, (name, bars) in data.items() for i in range(1, len(bars))}
    u2, dset = [], set()
    with open(ROOT / "reports/noon/noon_days.csv", encoding="utf-8") as fp:
        for r in csv.DictReader(fp):
            d = date.fromisoformat(r["date"])
            p = prev.get((r["code"], d))
            bars = load_minute(MIN, r["code"], d)
            if p and bars and bars[0].open > p[0]:
                u2.append((d, {"code": r["code"], "name": p[1], "prev_close": p[0]}, bars))
                dset.add(d)
    ok = lambda c, bars, i, p, low: p >= c["prev_close"] * 1.05  # noqa: E731
    grid("U2 당일 급등 종목 갭상승 (2026-07~09, 돌파 가격 +5%↑ 만)", u2, len(dset), ok)
    for m in (7, 8, 9):
        show(f"  {m}월", collect([u for u in u2 if u[0].month == m]), len([d for d in dset if d.month == m]), flt=ok)


if __name__ == "__main__":
    main()
