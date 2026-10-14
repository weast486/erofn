"""전일 20일 종가 신고가(상승률 무관) 종목이 다음 날 갭하락으로 시작해 전일 종가를 다시 넘으면 매수 (키움 단타 A 규칙의 대상만 바꿈).

  python scripts/newhigh_gapdown_break.py list  [--start 2025-10-01] [--out reports/surgebreak/newhigh_days.csv]
      → 분봉이 없는 (종목, 날짜) 목록 저장. 받기: python -m tossbot.backtest download-minute --days 그 파일
  python scripts/newhigh_gapdown_break.py run   [--start 2025-10-01]

대상: 전일 종가 > 그 전 20거래일 최고 종가, 20일 평균 거래대금 30억↑, 1주 10만원 이하.
매매: 시가 < 전일 종가 → 09:05 까지 전일 종가를 넘으면 매수, 손절 -10% / 익절 +7% / 12:00 정리 (surgebreak prevclose 와 같은 코드).
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tossbot.backtest import PullBreakSettings, load_cache, run_surge_break  # noqa: E402


def events(data: dict, start: date, lookback: int, min_avg: float, max_price: float, first_days: int = 0):
    """{매매일: [대상]} 과 {(code, 매매일): 일봉}. first_days > 0 이면 직전 그 기간에 신고가가 없던 첫 신고가만."""
    out, daily = defaultdict(list), {}
    for code, (name, bars) in data.items():
        closes = [b.close for b in bars]
        amounts = [b.close * b.volume for b in bars]
        is_high = [i >= lookback and closes[i] > max(closes[i - lookback:i]) for i in range(len(bars))]
        for i in range(max(21, lookback), len(bars) - 1):
            d, t = bars[i], bars[i + 1]
            if t.day < start or not is_high[i] or d.volume <= 0:
                continue
            if first_days and any(is_high[max(0, i - first_days):i]):
                continue
            if sum(amounts[i - 20:i]) / 20 < min_avg or d.close > max_price:
                continue
            p = bars[i - 1]
            out[t.day].append({"code": code, "name": name, "prev_close": d.close, "prev_high": d.high,
                               "prev_open": d.open, "prev_low": d.low,
                               "prev_change": (d.close / p.close - 1) * 100, "prev_amount": amounts[i]})
            daily[(code, t.day)] = t
    return out, daily


def summarize(label: str, trades: list[dict], n_days: int) -> None:
    if not trades:
        print(f"{label:<30} 거래 없음")
        return
    rets = [t["ret_pct"] for t in trades]
    cut = sorted(rets)[:-5] or rets
    byday = defaultdict(list)
    for t in trades:
        byday[t["trade_day"]].append(t)
    eq = peak = 1.0
    mdd = 0.0
    yr = defaultdict(lambda: 1.0)
    picked = []
    for d in sorted(byday):
        p = sorted(byday[d], key=lambda x: x["entry_time"])[:3]
        picked += [x["ret_pct"] for x in p]
        r = sum(x["ret_pct"] for x in p) / 3 / 100
        eq *= 1 + r
        yr[d[:4]] *= 1 + r
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    ys = " / ".join(f"{y} {(v - 1) * 100:+.0f}" for y, v in sorted(yr.items()))
    print(f"{label:<30} {len(trades):>5}건 승률 {sum(r > 0 for r in rets) / len(rets) * 100:.0f}% 거래당 {sum(rets) / len(rets):+.2f}% "
          f"(5건 빼면 {sum(cut) / len(cut):+.2f}%) | 거래한 날 {len(byday) / n_days * 100:.0f}% 하루 3종목 {len(picked)}건 "
          f"{sum(picked) / len(picked):+.2f}% 계좌 {(eq - 1) * 100:+.0f}% MDD {mdd * 100:.0f}% | {ys}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["list", "run"])
    ap.add_argument("--start", default="2025-10-01")
    ap.add_argument("--cache", type=Path, default=ROOT / "data/ohlcv_long")
    ap.add_argument("--minute-dir", type=Path, default=ROOT / "data/minute")
    ap.add_argument("--out", type=Path, default=ROOT / "reports/surgebreak/newhigh_days.csv")
    ap.add_argument("--lookback", type=int, default=20)
    ap.add_argument("--min-prev-amount", type=float, default=0.0, help="list: 전일 거래대금 하한 (받을 양 줄이기)")
    args = ap.parse_args()
    start = date.fromisoformat(args.start)
    data = load_cache(args.cache)
    days, daily = events(data, start, args.lookback, 3e9, 100_000)
    n_days = len(days)
    total = sum(len(v) for v in days.values())
    # 일봉으로 그날 재돌파가 가능했던 날만 (시가 < 전일 종가 < 고가)
    poss = {d: [c for c in cs if daily[(c["code"], d)].open < c["prev_close"] < daily[(c["code"], d)].high] for d, cs in days.items()}
    n_poss = sum(len(v) for v in poss.values())
    have = sum((args.minute_dir / f"{c['code']}_{d.isoformat()}.csv").exists() for d, cs in poss.items() for c in cs)
    print(f"{args.start}~ 거래일 {n_days}일, 20일 신고가 대상 {total}건(하루 평균 {total / n_days:.1f}) → 갭하락 뒤 재돌파 가능 {n_poss}건, 분봉 있음 {have}건")
    if args.cmd == "list":
        rows = sorted((c["code"], d.isoformat(), 4) for d, cs in poss.items() for c in cs
                      if c["prev_amount"] >= args.min_prev_amount
                      and not (args.minute_dir / f"{c['code']}_{d.isoformat()}.csv").exists())
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["code", "date", "pages"])
            w.writerows(rows)
        print(f"받을 목록 {len(rows)}개: {args.out}")
        return

    s = PullBreakSettings(take_profit=7, stop_pct=10, buy_until="09:05", exit_time="12:00", entry_bar_stop="close")
    info = {(c["code"], d.isoformat()): c for d, cs in poss.items() for c in cs}
    first = {k for d, cs in events(data, start, args.lookback, 3e9, 100_000, 20)[0].items() for c in cs
             for k in [(c["code"], d.isoformat())]}
    trades, missing = run_surge_break(poss, args.minute_dir, "prevclose", s)
    for t in trades:
        c = info[(t["trade_day"], t["code"])[::-1]]
        t["pc"], t["amt"], t["first"] = c["prev_change"], c["prev_amount"], (t["code"], t["trade_day"]) in first
    print(f"분봉 없음 {missing}건 (결과에서 빠짐)")
    summarize("20일 신고가 전부", trades, n_days)
    summarize("  직전 20일 첫 신고가만", [t for t in trades if t["first"]], n_days)
    for lo, hi in ((-99, 3), (3, 7), (7, 10), (10, 15), (15, 20), (20, 99)):
        summarize(f"  전일 상승률 {lo:+d}~{hi:+d}%", [t for t in trades if lo <= t["pc"] < hi], n_days)
    for a in (2e10, 5e10, 1e11):
        summarize(f"  전일 거래대금 {a / 1e8:.0f}억↑", [t for t in trades if t["amt"] >= a], n_days)
    summarize("  거래대금 200억↑·첫 신고가", [t for t in trades if t["amt"] >= 2e10 and t["first"]], n_days)
    summarize("  전일 +20%↑ (지금 봇 대상과 겹침)", [t for t in trades if t["pc"] >= 20], n_days)
    summarize("  전일 +20% 미만", [t for t in trades if t["pc"] < 20], n_days)
    for q in ("2025-10", "2026-01", "2026-04", "2026-07"):
        y, m = int(q[:4]), int(q[5:])
        ks = {f"{y}-{mm:02d}" for mm in range(m, m + 3)}
        summarize(f"  분기 {q}~", [t for t in trades if t["trade_day"][:7] in ks], n_days)


if __name__ == "__main__":
    main()
