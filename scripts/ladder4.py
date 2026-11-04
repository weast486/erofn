"""기준봉 종가 매수 + 3단계 물타기(계좌 4개로 나눔), 손절 없음·계좌별 익절 백테스트 (일봉).

규칙 (2026-10-23 사용자 요청):
- 돈을 똑같이 4개 계좌로 나눔. 계좌마다 최대 N종목(기본 20), 종목당 = 계좌 평가금액 / N.
- 1번: 당일 거래대금 X억↑·상승률 Y%↑ 기준봉 종가 매수 (거래대금 큰 순, 상한가 마감 제외).
- k번(2~4): (k-1)번 계좌의 그 종목이 아직 보유 중이고 (k-1)번 매수가 대비 -D% 에 닿으면 지정가 매수
  (시가가 이미 아래면 시가).
- 손절 없음, 계좌마다 자기 매수가 +T% 지정가 익절(시가가 이미 위면 시가).
- 같은 날: 그날 시작 때 윗단계가 보유 중이면 아래 단계 매수 가능, 그날 산 것은 그날 익절 안 함.
- 비용: 수수료 0.015%, 매도세(연도별), 종가 매수 슬리피지 0.1%. 소수 주(정수 주 반올림 없음).

python scripts/ladder4.py --min-amount 5e10 --min-change 10
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tossbot.backtest import load_cache  # noqa: E402

FEE = 0.00015
TAX = {2022: 0.0023, 2023: 0.0020, 2024: 0.0018, 2025: 0.0015, 2026: 0.0020}
SLIP = 0.001


def run(data, start, min_amount, min_change, drop, tp, slots, levels, reentry, max_change=29.5,
        capital=100_000_000):
    # 날짜별 인덱스
    bars = {c: b for c, (_, b) in data.items() if b}
    idx = {c: {bar.day: i for i, bar in enumerate(b)} for c, b in bars.items()}
    days = sorted({bar.day for b in bars.values() for bar in b if bar.day >= start})
    cash = [capital / levels] * levels
    # pos[level][code] = dict(entry, qty, cost, d)
    pos = [dict() for _ in range(levels)]
    # 체인: code -> dict(entries=[...], done=set(levels bought))
    chain = {}
    trades = []
    equity = []
    for d in days:
        year = d.year
        tax = TAX.get(year, 0.002)
        today = {}
        for c in set(chain):
            i = idx[c].get(d)
            if i is not None:
                today[c] = bars[c][i]
        held_start = [set(p) for p in pos]
        # 1) 물타기 매수 (윗단계가 그날 시작 때 보유 중)
        for c, bar in today.items():
            ch = chain[c]
            for k in range(1, levels):
                if c in pos[k] or c not in held_start[k - 1]:
                    continue
                if k in ch["done"] and not reentry:
                    continue
                level = pos[k - 1][c]["entry"] * (1 - drop / 100)
                if bar.low > level:
                    continue
                price = min(bar.open, level)
                eq_k = cash[k] + sum(p["qty"] * _px(bars, idx, cc, d, p) for cc, p in pos[k].items())
                amt = min(eq_k / slots, cash[k])
                if amt <= 0:
                    continue
                qty = amt / (price * (1 + FEE))
                cash[k] -= amt
                pos[k][c] = {"entry": price, "qty": qty, "cost": amt, "d": d}
                ch["done"].add(k)
        # 2) 익절 (그날 산 것 제외)
        for k in range(levels):
            for c in list(pos[k]):
                p = pos[k][c]
                if p["d"] == d or c not in today:
                    continue
                bar = today[c]
                target = p["entry"] * (1 + tp / 100)
                if bar.high >= target:
                    price = max(bar.open, target)
                    proceeds = p["qty"] * price * (1 - FEE - tax)
                    cash[k] += proceeds
                    trades.append((k + 1, c, p["d"], d, proceeds / p["cost"] - 1))
                    del pos[k][c]
        # 체인 정리: 아무 단계도 안 들고 있으면 끝
        for c in list(chain):
            if not any(c in pos[k] for k in range(levels)):
                del chain[c]
        # 3) 기준봉 종가 매수 (1번 계좌)
        cands = []
        for c, b in bars.items():
            i = idx[c].get(d)
            if i is None or i == 0 or c in chain:
                continue
            bar, prev = b[i], b[i - 1]
            if prev.close <= 0:
                continue
            chg = (bar.close / prev.close - 1) * 100
            amount = bar.close * bar.volume
            if chg >= min_change and chg < max_change and amount >= min_amount:
                cands.append((amount, c, bar))
        cands.sort(reverse=True)
        for amount, c, bar in cands:
            if len(pos[0]) >= slots:
                break
            eq0 = cash[0] + sum(p["qty"] * _px(bars, idx, cc, d, p) for cc, p in pos[0].items())
            amt = min(eq0 / slots, cash[0])
            if amt <= eq0 / slots * 0.5:
                break
            price = bar.close * (1 + SLIP)
            cash[0] -= amt
            pos[0][c] = {"entry": price, "qty": amt / (price * (1 + FEE)), "cost": amt, "d": d}
            chain[c] = {"done": {0}}
        # 평가
        accs = []
        for k in range(levels):
            accs.append(cash[k] + sum(p["qty"] * _px(bars, idx, c, d, p) for c, p in pos[k].items()))
        equity.append((d, accs))
    open_pos = [(k + 1, c, p["d"], _px(bars, idx, c, days[-1], p) / p["entry"] - 1)
                for k in range(levels) for c, p in pos[k].items()]
    return equity, trades, open_pos


def _px(bars, idx, c, d, p):
    i = idx[c].get(d)
    if i is None:
        # 거래정지 등: 가장 최근 종가
        b = bars[c]
        j = max((k for k, bar in enumerate(b) if bar.day <= d), default=None)
        return b[j].close if j is not None else p["entry"]
    return bars[c][i].close


def summarize(equity, trades, open_pos, levels, label):
    tot = [(d, sum(a)) for d, a in equity]
    peak, mdd = 0, 0
    for _, v in tot:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    by_year = defaultdict(list)
    for d, v in tot:
        by_year[d.year].append(v)
    prev = tot[0][1]
    yr = []
    for y in sorted(by_year):
        end = by_year[y][-1]
        yr.append(f"{y} {(end / prev - 1) * 100:+.1f}")
        prev = end
    print(f"\n== {label}")
    print(f"  전체 {(tot[-1][1] / tot[0][1] - 1) * 100:+.1f}%  MDD {mdd * 100:.1f}%  | " + " / ".join(yr))
    final = equity[-1][1]
    start_acc = equity[0][1]
    for k in range(levels):
        tk = [t for t in trades if t[0] == k + 1]
        ok = [t for t in open_pos if t[0] == k + 1]
        hold = sorted((t[3] - t[2]).days for t in tk)
        med = hold[len(hold) // 2] if hold else 0
        unreal = sum(t[3] for t in ok) / len(ok) * 100 if ok else 0
        print(f"  {k + 1}번 계좌: 익절 {len(tk)}건 보유 중간 {med}일 | 끝에 남은 {len(ok)}종목 평균 {unreal:+.1f}% "
              f"| 계좌 {(final[k] / start_acc[k] - 1) * 100:+.1f}%")
    # 계좌 1 이 묶인 비율
    stuck = [t for t in open_pos if t[0] == 1]
    old = [t for t in stuck if (equity[-1][0] - t[2]).days > 180]
    print(f"  끝날 때 1번 계좌 보유 {len(stuck)}종목 중 180일 넘게 물린 것 {len(old)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data/ohlcv_long")
    ap.add_argument("--start", default="2023-01-02")
    ap.add_argument("--min-amount", type=float, default=5e10)
    ap.add_argument("--min-change", type=float, default=10)
    ap.add_argument("--drop", type=float, default=10)
    ap.add_argument("--tp", type=float, default=10)
    ap.add_argument("--slots", type=int, default=20)
    ap.add_argument("--levels", type=int, default=4)
    ap.add_argument("--reentry", action="store_true")
    a = ap.parse_args()
    data = load_cache(Path(a.cache))
    eq, tr, op = run(data, date.fromisoformat(a.start), a.min_amount, a.min_change, a.drop, a.tp,
                     a.slots, a.levels, a.reentry)
    summarize(eq, tr, op, a.levels,
              f"{a.min_amount / 1e8:.0f}억·+{a.min_change}% 낙폭 {a.drop}% 익절 {a.tp}% 계좌 {a.levels}개"
              f"{' 재진입' if a.reentry else ''}")


if __name__ == "__main__":
    main()
