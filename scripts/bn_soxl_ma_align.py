"""바이낸스 SOXL 토큰 1분봉: 7/21/50 이평 정배열이 막 시작될 때 매수 → 정배열이 깨지면 매도 (모두 종가 기준).

데이터: data/binance/SOXLUSDT_1m.csv.gz (UTC 12~22시 = 미국 동부 8~18시, 2026-05-15~)
규칙:
  1분봉 종가로 계산한 7선 > 21선 > 50선 이 '아니었다가 → 맞게' 된 봉(정배열 초입) 다음 봉 시가에 매수
  그 뒤 어떤 봉의 종가 기준으로 정배열이 깨지면(7 > 21 > 50 이 아님) 다음 봉 시가에 매도
  정규장(9:30~16:00)에만 매수, 받아 둔 봉이 끊기는 곳(장 끝)에서는 마지막 봉 종가에 정리
비용: 한쪽 수수료 0.05% + 슬리피지 0.05% (시장가 기준)

    python scripts/bn_soxl_ma_align.py [SYMBOL]
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bn_soxl_ma_touch import bars15, is_rth  # noqa: E402

FEE, SLIP = 0.0005, 0.0005


def sma(c, n):
    out = [None] * len(c)
    s = 0.0
    for i, x in enumerate(c):
        s += x
        if i >= n:
            s -= c[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def run(bars, mas=(7, 21, 50), tf=1, exit_rule="full", price_above=False, entry_rth=True, side=1, cost=FEE + SLIP,
        min_gap=0.0, stop=0.0):
    """[(진입 시각, 수익률 %, 비용 전 %, 보유 봉 수)]. side 1 = 정배열 롱 / -1 = 역배열 숏.
    exit_rule: full = 7>21>50 이 깨지면 / fast = 7선이 21선 아래로 / price = 종가가 21선 아래로."""
    c = [b[4] for b in bars]
    a, m, s = (sma(c, n) for n in mas)
    step = timedelta(minutes=tf)

    def aligned(i):
        if s[i] is None:
            return False
        ok = a[i] > m[i] > s[i] if side == 1 else a[i] < m[i] < s[i]
        if ok and price_above:
            ok = c[i] > a[i] if side == 1 else c[i] < a[i]
        if ok and min_gap:
            ok = abs(a[i] / s[i] - 1) * 100 >= min_gap
        return ok

    def broken(i):
        if exit_rule == "fast":
            return a[i] <= m[i] if side == 1 else a[i] >= m[i]
        if exit_rule == "price":
            return c[i] < m[i] if side == 1 else c[i] > m[i]
        return not (a[i] > m[i] > s[i] if side == 1 else a[i] < m[i] < s[i])

    out, i, n = [], mas[2], len(bars)
    while i < n - 1:
        cont = bars[i][0] - bars[i - 1][0] == step and bars[i + 1][0] - bars[i][0] == step
        if cont and aligned(i) and not aligned(i - 1) and (not entry_rth or is_rth(bars[i + 1][0])):
            j = i + 1
            entry = bars[j][1]
            k, px = j, None
            while True:
                if stop and side * (bars[k][3 if side == 1 else 2] / entry - 1) * 100 <= -stop:
                    px = entry * (1 - side * stop / 100)
                    break
                last = k + 1 >= n or bars[k + 1][0] - bars[k][0] != step
                if last:
                    px = bars[k][4]  # 받아 둔 봉이 끊김(장 끝) → 종가 정리
                    break
                if broken(k):
                    k += 1
                    px = bars[k][1]
                    break
                k += 1
            gross = side * (px / entry - 1) * 100
            out.append((bars[j][0], gross - cost * 2 * 100, gross, k - j))
            i = k
        i += 1
    return out


def stat(rs):
    if not rs:
        return "0건"
    r = sorted(x[1] for x in rs)
    g = [x[2] for x in rs]
    hold = sorted(x[3] for x in rs)[len(rs) // 2]
    return (f"{len(r)}건 거래당 {sum(r) / len(r):+.3f}% (비용 전 {sum(g) / len(g):+.3f}%) 승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}% "
            f"보유 중간 {hold}봉")


def account(rs, lev=1.0):
    eq = peak = 1.0
    mdd = 0.0
    for _, r, _, _ in rs:
        eq *= max(0.0, 1 + r / 100 * lev)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return f"{eq * 100 - 100:+.0f}% (MDD {mdd * 100:.0f}%)"


def months(rs):
    ms = sorted({x[0].strftime("%m") for x in rs})
    return " / ".join(f"{m}월 {len(v)}건 {sum(x[1] for x in v) / len(v):+.3f}%" for m in ms for v in [[x for x in rs if x[0].strftime('%m') == m]])


def main():
    sym = sys.argv[1] if len(sys.argv) > 1 else "SOXLUSDT"
    b = bars15(sym, 1)
    print(f"{sym} 1분봉 {len(b)}개, {b[0][0]:%Y-%m-%d} ~ {b[-1][0]:%Y-%m-%d} ({len({x[0].date() for x in b})}일), "
          f"가격 {b[0][1]:g} → {b[-1][4]:g}, 비용 한쪽 {(FEE + SLIP) * 100:g}% (왕복 {(FEE + SLIP) * 200:g}%)\n")
    base = run(b)
    print(f"== 기본 (7/21/50, 정배열 초입 다음 봉 시가 매수 → 정배열 깨진 다음 봉 시가 매도, 정규장 매수)\n  {stat(base)}")
    print(f"  매번 전액 1배 {account(base)} / 2배 {account(base, 2)} / 5배 {account(base, 5)}")
    print(f"  월별 {months(base)}")
    r = sorted(x[2] for x in base)
    print(f"  비용 전 수익 분포: 하위 10% {r[len(r) // 10]:+.2f} / 중간 {r[len(r) // 2]:+.2f} / 상위 10% {r[-len(r) // 10]:+.2f} / 최고 {r[-1]:+.2f}%")
    print(f"  수수료가 메이커(한쪽 0.02%, 슬리피지 없음)라면: {stat(run(b, cost=0.0002)).split(' 승률')[0]}")
    print("\n== 조건을 바꾸면")
    for label, kw in (("매도: 7선이 21선 아래로 갈 때만", {"exit_rule": "fast"}), ("매도: 종가가 21선 아래로", {"exit_rule": "price"}),
                      ("매수 때 종가 > 7선 도 요구", {"price_above": True}), ("7선과 50선 간격 0.1%↑ 일 때만", {"min_gap": 0.1}),
                      ("7선과 50선 간격 0.3%↑ 일 때만", {"min_gap": 0.3}), ("매수 시간 제한 없음", {"entry_rth": False}),
                      ("손절 -0.5% 추가", {"stop": 0.5}), ("손절 -1% 추가", {"stop": 1.0}),
                      ("이평 5/20/60", {"mas": (5, 20, 60)}), ("이평 10/30/60", {"mas": (10, 30, 60)}),
                      ("이평 20/60/120", {"mas": (20, 60, 120)}), ("이평 7/21/50 → 숏(역배열 초입 매도)", {"side": -1})):
        rs = run(b, **kw)
        print(f"  {label}: {stat(rs)} | 1배 {account(rs)}")
    print("\n== 봉 길이를 바꾸면 (같은 7/21/50)")
    for tf in (3, 5, 15, 30, 60):
        bb = bars15(sym, tf)
        rs = run(bb, tf=tf)
        print(f"  {tf}분봉: {stat(rs)} | 1배 {account(rs)}")


if __name__ == "__main__":
    main()
