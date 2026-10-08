"""바이낸스 토큰: 볼린저 중심선이 오르는 중 + 밴드 수축 상태에서 하단을 아래로 벗어나면 롱 매수.

데이터: data/binance/{SYMBOL}_1m.csv.gz (UTC 12~22시 = 미국 동부 8~18시)
규칙 (완성된 봉의 종가 기준):
  볼린저(20, 2). 중심선 기울기: 중심선이 slope 봉 전보다 높음. 수축: 밴드 폭(상단-하단)/중심선 이 최근 100봉 폭의 하위 sq% 이하
  break = close: 종가가 하단 아래로 끝난 봉 다음 봉 시가에 매수 / touch: 저가가 하단에 닿으면 하단 가격 지정가(시가가 아래면 시가)
  매도: mid = 종가가 중심선 이상이면 다음 봉 시가 / upper = 종가가 상단 이상 / 숫자 N = 매수 뒤 N봉째 시가
        (mid·upper 는 손절 stop% 를 같이 쓸 수 있음), 받아 둔 봉이 끊기는 곳(장 끝)에서는 마지막 종가에 정리
  정규장(9:30~16:00)에만 매수, 한 번에 한 포지션
비용: 한쪽 수수료 0.05% + 슬리피지 0.05%

    python scripts/bn_bb_squeeze_dip.py [SYMBOL ...]
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bn_soxl_ma_touch import bars15, is_rth  # noqa: E402

COST = 0.001  # 한쪽


def roll(a, n, fn):
    w = np.lib.stride_tricks.sliding_window_view(a, n)
    r = np.full(len(a), np.nan)
    r[n - 1:] = fn(w, axis=1)
    return r


def run(bars, tf, sq=25, slope=5, brk="close", exit_rule="mid", stop=0.0, need_slope=True, need_squeeze=True, down_slope=False):
    """[(진입 시각, 수익률 %, 비용 전 %, 보유 봉 수)]."""
    t = [b[0] for b in bars]
    o = np.array([b[1] for b in bars]); h = np.array([b[2] for b in bars]); l = np.array([b[3] for b in bars]); c = np.array([b[4] for b in bars])
    n = len(c)
    if n < 150:
        return []
    mid = roll(c, 20, np.mean); sd = roll(c, 20, np.std)
    up, lo = mid + 2 * sd, mid - 2 * sd
    width = 4 * sd / mid
    q = np.full(n, np.nan)
    q[99:] = np.nanpercentile(np.lib.stride_tricks.sliding_window_view(width, 100), sq, axis=1)
    step = timedelta(minutes=tf)
    out, i = [], 125
    while i < n - 2:
        ok = t[i] - t[i - 1] == step and t[i + 1] - t[i] == step
        if ok and need_slope:
            ok = (mid[i - 1] < mid[i - 1 - slope]) if down_slope else (mid[i - 1] > mid[i - 1 - slope])
        if ok and need_squeeze:
            ok = width[i - 1] <= q[i - 1]
        entry = None
        if ok and brk == "close":
            # 직전 봉(i-1)까지 조건 충족, 이번 봉(i) 종가가 하단 아래 → 다음 봉(i+1) 시가 매수
            if c[i] < lo[i] and is_rth(t[i + 1]):
                j, entry = i + 1, o[i + 1]
        elif ok and brk == "touch":
            if l[i] <= lo[i - 1] and is_rth(t[i]):
                j, entry = i, min(o[i], lo[i - 1])
        if entry is None:
            i += 1
            continue
        k, px = j, None
        while True:
            if stop and (l[k] / entry - 1) * 100 <= -stop and not (brk == "touch" and k == j and False):
                px = min(entry * (1 - stop / 100), o[k] if k > j else entry * (1 - stop / 100))
                break
            last = k + 1 >= n or t[k + 1] - t[k] != step
            if last:
                px = c[k]
                break
            if isinstance(exit_rule, int):
                if k - j + 1 >= exit_rule:
                    k += 1
                    px = o[k]
                    break
            elif (c[k] >= mid[k] if exit_rule == "mid" else c[k] >= up[k]) and not (brk == "close" and k < j):
                k += 1
                px = o[k]
                break
            k += 1
        gross = (px / entry - 1) * 100
        out.append((t[j], gross - COST * 200, gross, k - j))
        i = k + 1
    return out


def stat(rs):
    if not rs:
        return "0건"
    r = sorted(x[1] for x in rs)
    g = [x[2] for x in rs]
    ex = sum(r[:-3]) / (len(r) - 3) if len(r) > 6 else float("nan")
    return (f"{len(r)}건 거래당 {sum(r) / len(r):+.3f}% (비용 전 {sum(g) / len(g):+.3f}%, 좋은 3건 빼면 {ex:+.3f}%) "
            f"승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}% 보유 중간 {sorted(x[3] for x in rs)[len(rs) // 2]}봉")


def account(rs, lev=1.0):
    eq = peak = 1.0
    mdd = 0.0
    for _, r, _, _ in sorted(rs):
        eq *= max(0.0, 1 + r / 100 * lev)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    return f"{eq * 100 - 100:+.0f}% (MDD {mdd * 100:.0f}%)"


def main():
    syms = sys.argv[1:] or ["SOXLUSDT"]
    for sym in syms:
        print(f"######## {sym}, 비용 한쪽 {COST * 100:g}% (왕복 {COST * 200:g}%)")
        allb = {tf: bars15(sym, tf) for tf in (1, 5, 15, 30, 60)}
        b0 = allb[15]
        print(f"기간 {b0[0][0]:%Y-%m-%d} ~ {b0[-1][0]:%Y-%m-%d} ({len({x[0].date() for x in b0})}일), 가격 {b0[0][1]:g} → {b0[-1][4]:g}\n")
        print("== 봉 길이 × 매도 방식 (종가가 하단 아래로 끝난 다음 봉 시가 매수, 수축 = 폭 하위 25%, 중심선 5봉 전보다 높음)")
        for tf, b in allb.items():
            line = [f"  {tf:2d}분봉"]
            for ex, nm in (("mid", "중심선 복귀"), ("upper", "상단 도달"), (3, "3봉 뒤"), (5, "5봉 뒤"), (10, "10봉 뒤")):
                rs = run(b, tf, exit_rule=ex)
                r = [x[1] for x in rs]
                line.append(f"{nm} {len(r)}건 {sum(r) / len(r):+.2f}%" if r else f"{nm} 0건")
            print(" | ".join(line))
        print("\n== 15분봉 상세")
        b = allb[15]
        for label, kw in (("기본 (중심선 복귀 매도)", {}), ("상단 도달 매도", {"exit_rule": "upper"}), ("5봉 뒤 매도", {"exit_rule": 5}),
                          ("중심선 복귀 + 손절 1%", {"stop": 1.0}), ("중심선 복귀 + 손절 2%", {"stop": 2.0}),
                          ("하단에 닿으면 지정가 매수", {"brk": "touch"}), ("수축 기준 하위 50%", {"sq": 50}),
                          ("수축 기준 하위 10%", {"sq": 10}), ("기울기 10봉 기준", {"slope": 10}),
                          ("수축 조건 없이 (기울기만)", {"need_squeeze": False}), ("기울기 조건 없이 (수축만)", {"need_slope": False}),
                          ("조건 둘 다 없이 (하단 이탈만)", {"need_squeeze": False, "need_slope": False}),
                          ("반대: 중심선이 내리는 중 + 수축", {"down_slope": True})):
            rs = run(b, 15, **kw)
            print(f"  {label}: {stat(rs)} | 1배 {account(rs)}")
        print("\n== 5분봉 상세")
        b = allb[5]
        for label, kw in (("기본 (중심선 복귀 매도)", {}), ("상단 도달 매도", {"exit_rule": "upper"}), ("5봉 뒤 매도", {"exit_rule": 5}),
                          ("하단에 닿으면 지정가 매수", {"brk": "touch"}), ("수축 조건 없이", {"need_squeeze": False}),
                          ("기울기 조건 없이", {"need_slope": False}), ("조건 둘 다 없이", {"need_squeeze": False, "need_slope": False}),
                          ("반대: 중심선이 내리는 중 + 수축", {"down_slope": True})):
            rs = run(b, 5, **kw)
            print(f"  {label}: {stat(rs)} | 1배 {account(rs)}")
        print()


if __name__ == "__main__":
    main()
