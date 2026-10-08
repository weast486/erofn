"""바이낸스 SOXL 토큰 15분봉: 정배열(가격 > 7선 > 20선 > 60선)에서 7선에 닿으면 매수 → 다음 봉에 매도.

데이터: data/binance/SOXLUSDT_1m.csv.gz (받아 둔 것은 UTC 12~22시 = 미국 동부 8~18시, 2026-05-15~)
규칙 (직전 봉까지 완성된 값으로 판단):
  직전 봉 종가 > 7선 > 20선 > 60선 (단순 이동평균, 15분봉 종가)
  이번 봉 저가가 7선(직전 봉 기준 값) 이하로 내려오면 7선 가격에 지정가 매수 (시가가 이미 7선 아래면 시가)
  매도: same = 그 봉 종가 / next_open = 다음 봉 시가 / next_close = 다음 봉 종가
비용: 매수 지정가(메이커) 0.02%, 매도 시장가(테이커) 0.05% + 슬리피지 0.05%

    python scripts/bn_soxl_ma_touch.py [SYMBOL]
"""
from __future__ import annotations

import csv
import gzip
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
MAKER, TAKER, SLIP = 0.0002, 0.0005, 0.0005


def bars15(sym: str, tf: int = 15):
    g = {}
    with gzip.open(ROOT / "data" / "binance" / f"{sym}_1m.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            t = datetime.fromtimestamp(int(r["open_ms"]) / 1000, ET)
            if t.weekday() >= 5:
                continue
            k = t.replace(minute=t.minute - t.minute % tf, second=0, microsecond=0)
            o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
            x = g.get(k)
            if x is None:
                g[k] = [o, h, l, c, 1]
            else:
                x[1] = max(x[1], h); x[2] = min(x[2], l); x[3] = c; x[4] += 1
    return [(k, *v[:4]) for k, v in sorted(g.items()) if v[4] >= tf - 2]


def is_rth(t: datetime) -> bool:
    m = t.hour * 60 + t.minute
    return 570 <= m < 960


def run(bars, exit_mode="next_open", source="all", entry_rth=True, mas=(7, 20, 60), slope=False, taker_entry=False,
        tf=15, gap=0.0, need_above=True):
    """거래 목록 [(진입 시각, 수익률 %)]. source = all(받아 둔 봉 전부로 이평) / rth(정규장 봉만 이어 붙여 이평)."""
    src = [b for b in bars if source == "all" or is_rth(b[0])]
    c = [b[4] for b in src]
    a, m, s = mas
    out = []
    for i in range(s, len(src) - 2):
        t = src[i][0]
        if entry_rth and not is_rth(t):
            continue
        if src[i][0] - src[i - 1][0] != timedelta(minutes=tf):
            continue  # 밤·주말을 건너뛴 첫 봉은 빼고 (이평 값이 전날 것)
        m7 = sum(c[i - a:i]) / a
        m20 = sum(c[i - m:i]) / m
        m60 = sum(c[i - s:i]) / s
        if not (m7 > m20 > m60) or (need_above and not c[i - 1] > m7):
            continue
        if slope and not m7 > sum(c[i - a - 1:i - 1]) / a:
            continue
        level = m7 * (1 + gap / 100)
        _, o, h, l, cl = src[i]
        if l > level:
            continue
        entry = min(o, level)
        nxt = src[i + 1]
        if exit_mode != "same" and nxt[0] - t != timedelta(minutes=tf):
            px = cl  # 다음 봉이 이어지지 않으면(장 끝) 그 봉 종가에 정리
        else:
            px = cl if exit_mode == "same" else nxt[1] if exit_mode == "next_open" else nxt[4]
        fee_in = (TAKER + SLIP) if (taker_entry or o <= level) else MAKER  # 시가가 이미 아래면 시장가로 삼
        ret = (px * (1 - TAKER - SLIP) / (entry * (1 + fee_in)) - 1) * 100
        gross = (px / entry - 1) * 100
        out.append((t, ret, gross))
    return out


def stat(rs):
    if not rs:
        return "0건"
    r = [x[1] for x in rs]
    g = [x[2] for x in rs]
    return (f"{len(r)}건 거래당 {sum(r) / len(r):+.3f}% (비용 전 {sum(g) / len(g):+.3f}%) 승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}%")


def account(rs, lev=1.0):
    eq = peak = 1.0
    mdd = 0.0
    for _, r, _ in rs:
        eq *= max(0.0, 1 + r / 100 * lev)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return f"{eq * 100 - 100:+.0f}% (MDD {mdd * 100:.0f}%)"


def months(rs):
    ms = sorted({x[0].strftime("%m") for x in rs})
    return " / ".join(f"{m}월 {len(v)}건 {sum(x[1] for x in v) / len(v):+.2f}%" for m in ms for v in [[x for x in rs if x[0].strftime('%m') == m]])


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOXLUSDT"
    b = bars15(sym)
    days = len({x[0].date() for x in b})
    print(f"{sym} 15분봉 {len(b)}개, {b[0][0]:%Y-%m-%d} ~ {b[-1][0]:%Y-%m-%d} ({days}일), 가격 {b[0][1]:g} → {b[-1][4]:g} ({(b[-1][4] / b[0][1] - 1) * 100:+.0f}%)")
    print(f"비용: 매수 지정가 {MAKER * 100:g}%, 매도 시장가 {TAKER * 100:g}% + 슬리피지 {SLIP * 100:g}%\n")
    print("== 기본: 받아 둔 봉 전부로 이평, 정규장(9:30~16:00)에만 매수")
    for mode, name in (("same", "그 봉 종가에 매도"), ("next_open", "다음 봉 시가에 매도"), ("next_close", "다음 봉 종가에 매도")):
        rs = run(b, mode)
        print(f"  {name}: {stat(rs)}\n      매번 전액 1배 {account(rs)} / 2배 {account(rs, 2)} / 5배 {account(rs, 5)}\n      {months(rs)}")
    print("\n== 조건을 바꾸면 (다음 봉 시가 매도 / 다음 봉 종가 매도)")
    for label, kw in (("정규장 봉만으로 이평", {"source": "rth"}), ("매수 시간 제한 없음(8~18시)", {"entry_rth": False}),
                      ("7선이 오르는 중일 때만", {"slope": True}), ("직전 종가 > 7선 조건 없이", {"need_above": False}),
                      ("7선보다 0.2% 위에서 매수", {"gap": 0.2}), ("7선보다 0.3% 아래에서 매수", {"gap": -0.3}),
                      ("7선보다 0.5% 아래에서 매수", {"gap": -0.5}), ("이평 5/20/60", {"mas": (5, 20, 60)}),
                      ("이평 10/20/60", {"mas": (10, 20, 60)}), ("이평 7/20/120", {"mas": (7, 20, 120)}),
                      ("매수도 시장가 비용", {"taker_entry": True})):
        a, c = run(b, "next_open", **kw), run(b, "next_close", **kw)
        print(f"  {label}: {stat(a)} | {stat(c).split('건 ')[1]}")
    print("\n== 다른 봉 길이 (다음 봉 시가 / 종가 매도)")
    for tf in (5, 30, 60):
        bb = bars15(sym, tf)
        a, c = run(bb, "next_open", tf=tf), run(bb, "next_close", tf=tf)
        print(f"  {tf}분봉: {stat(a)} | {stat(c).split('건 ')[1]}")
    print("\n== 반대로: 역배열(가격 < 7 < 20 < 60)에서 7선에 닿으면 매도(숏) → 다음 봉 정리 (참고)")
    for mode, name in (("next_open", "다음 봉 시가"), ("next_close", "다음 봉 종가")):
        rs = run_short(b, mode)
        print(f"  {name}: {stat(rs)}")


def run_short(bars, exit_mode, tf=15, mas=(7, 20, 60)):
    c = [b[4] for b in bars]
    a, m, s = mas
    out = []
    for i in range(s, len(bars) - 2):
        t = bars[i][0]
        if not is_rth(t) or bars[i][0] - bars[i - 1][0] != timedelta(minutes=tf):
            continue
        m7, m20, m60 = sum(c[i - a:i]) / a, sum(c[i - m:i]) / m, sum(c[i - s:i]) / s
        if not (c[i - 1] < m7 < m20 < m60):
            continue
        _, o, h, l, cl = bars[i]
        if h < m7:
            continue
        entry = max(o, m7)
        nxt = bars[i + 1]
        px = cl if nxt[0] - t != timedelta(minutes=tf) else (nxt[1] if exit_mode == "next_open" else nxt[4])
        fee_in = (TAKER + SLIP) if o >= m7 else MAKER
        ret = (entry * (1 - fee_in) / (px * (1 + TAKER + SLIP)) - 1) * 100
        out.append((t, ret, (entry / px - 1) * 100))
    return out


if __name__ == "__main__":
    main()
