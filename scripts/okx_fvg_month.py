"""OKX FVG 단타를 한 기간 실제 OKX 1분봉으로 다시 돌려 보기 (2026-11-25 사용자 질문: 11월·100달러).

규칙은 실전 봇(binancebot/bot.py, .env.okx)과 같게: 전날 정규장 거래대금 상위 10 (OKX 주식 토큰, BN_EXCLUDE 제외),
첫 5분봉 + 1분봉 FVG(갭 전체가 첫 봉 밖, 갭 다 메운 가격, 그날 첫 FVG), 손절 첫 봉 반대편 닿음, 2R 익절, 15:20 정리,
손절 거리 5% 넘는 신호 제외, 하루 최대 2종목(먼저 체결된 순, 같은 분이면 ADR 높은 순), 종목당 평가금액 x 5배.
비용: 진입·손절·시간 정리 테이커 0.05% + 손절·정리 미끄러짐 0.05%, 익절 지정가 0.02%. 펀딩비 없음.
봇의 '숏 과열 제외(short_exhausted)' 도 넣음 (BN_SHORT_MAX_USED 0.6 / BN_SHORT_MAX_NEED 2).

python scripts/okx_fvg_month.py --start 2026-11-02 --end 2026-11-24 --capital 100
데이터는 data/okx_fvg 에 저장해 두고 다시 쓸 때는 받지 않음.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time as _t
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from binancebot.backtest import ET, US_EARLY_CLOSE, MinBar, Params, fvg_trades  # noqa: E402
from binancebot.bot import prev_trading_day, trading_day  # noqa: E402
from binancebot.okx_client import OkxClient  # noqa: E402
from binancebot.sizing import SizeRule  # noqa: E402

DATA = Path("data/okx_fvg")
EXCLUDE = {"SOXSUSDT", "SQQQUSDT", "SKDDUSDT", "KORUUSDT"}
TAKER, MAKER, SLIP = 0.05, 0.02, 0.05


def at(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm), ET)


def ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def cached(path: Path, fetch):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-11-02")
    ap.add_argument("--end", default="2026-11-24")
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--lev", type=float, default=5.0)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--max-trades", type=int, default=2)
    ap.add_argument("--old-adr", action="store_true", help="2026-11-26 수정 전 봇의 ADR(최근 약 8일 빠짐)")
    a = ap.parse_args()
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    days = [d0 + timedelta(i) for i in range((d1 - d0).days + 1) if trading_day(d0 + timedelta(i))]

    c = OkxClient()
    info = c.exchange_info()["symbols"]
    syms = [s["symbol"] for s in info if s["underlyingType"] == "EQUITY" and s["status"] == "TRADING"
            and s["symbol"] not in EXCLUDE]
    print(f"OKX 주식 토큰 {len(syms)}개, 거래일 {len(days)}일 ({days[0]} ~ {days[-1]})", flush=True)

    # 30분봉: 순위(전날 정규장 거래대금)와 ADR(20일)
    s30 = at(d0 - timedelta(days=45), 9, 30)
    n30 = int((at(d1, 16, 0) - s30).total_seconds() // 1800) + 2
    bars30 = {}
    for i, s in enumerate(syms):
        try:
            bars30[s] = cached(DATA / "30m" / f"{s}_{d0}_{d1}.json", lambda: c.klines(s, ms(s30), limit=n30, interval="30m"))
        except Exception as exc:  # noqa: BLE001
            print(f"  {s} 30분봉 실패: {exc}")
        if i % 40 == 0:
            print(f"  30분봉 {i}/{len(syms)}", flush=True)
    daily_qv: dict[str, dict[date, float]] = defaultdict(dict)
    daily_hl: dict[str, dict[date, list[float]]] = defaultdict(dict)
    for s, rows in bars30.items():
        for k in rows:
            t = datetime.fromtimestamp(int(k[0]) / 1000, ET)
            d = t.date()
            close = time(13, 0) if d in US_EARLY_CLOSE else time(16, 0)
            if not trading_day(d) or not (time(9, 30) <= t.time() < close):
                continue
            daily_qv[s][d] = daily_qv[s].get(d, 0.0) + float(k[7])
            h, l = daily_hl[s].get(d, [0.0, float("inf")])
            daily_hl[s][d] = [max(h, float(k[2])), min(l, float(k[3]))]

    def adr(s: str, d: date) -> float:
        # 전날까지 20거래일. --old-adr 이면 2026-11-26 전 봇처럼 39일 전 9:30 부터 30분봉 1500개(=31일)만 씀 → 최근 며칠 빠짐
        lo = at(d - timedelta(days=int(20 * 1.6) + 7), 9, 30)
        hi_t = lo + timedelta(minutes=30 * 1500) if a.old_adr else at(d, 0, 0)
        ds = sorted(x for x in daily_hl[s] if x < d and lo.date() <= x and at(x, 9, 30) < hi_t)[-20:]
        return sum((daily_hl[s][x][0] / daily_hl[s][x][1] - 1) * 100 for x in ds) / len(ds) if len(ds) >= 5 else 0.0

    p = Params(fvg_loc="zone", fvg_entry="full", fvg_pick="first", fvg_target_r=2.0, fvg_side="both",
               fvg_minutes=350, fvg_stop_mode="touch", rule=SizeRule(fee_pct=TAKER, slippage_pct=SLIP))
    eq = fix = a.capital
    peak, mdd = eq, 0.0
    rows_out, per_day, skipped = [], [], []
    for d in days:
        prev = prev_trading_day(d)
        top = [s for s, v in sorted(((s, daily_qv[s].get(prev, 0.0)) for s in syms), key=lambda kv: -kv[1]) if v > 0][: a.top]
        cands = []
        for s in top:
            rows = cached(DATA / "1m" / f"{s}_{d}.json", lambda: c.klines(s, ms(at(d, 9, 30)), limit=390, interval="1m"))
            mins = [MinBar(datetime.fromtimestamp(int(k[0]) / 1000, ET), float(k[1]), float(k[2]), float(k[3]),
                           float(k[4]), float(k[5])) for k in rows]
            for tr in fvg_trades(s, mins, p)[0]:
                if abs(tr.entry - tr.stop) / tr.entry * 100 > 5.0:
                    continue  # BN_MAX_STOP_PCT
                ad = adr(s, d)
                if tr.side == -1 and ad > 0:  # 봇의 short_exhausted (BN_SHORT_MAX_USED 0.6 / BN_SHORT_MAX_NEED 2)
                    hi = max(b.h for b in mins if b.t < tr.entry_t)
                    tp = tr.entry - 2.0 * (tr.stop - tr.entry)
                    if (hi - tr.entry) / tr.entry * 100 / ad >= 0.6 or (hi - tp) / tr.entry * 100 / ad >= 2.0:
                        skipped.append(f"{d} {s}")
                        continue
                cands.append((tr, ad))
            _t.sleep(0.02)
        cands.sort(key=lambda x: (x[0].entry_t, -x[1]))
        picked = cands[: a.max_trades]
        day_pnl = day_fix = 0.0
        for tr, ad in picked:
            fe = TAKER
            fx = MAKER if tr.reason == "target" else TAKER
            r = tr.side * (tr.exit - tr.entry) / tr.entry * 100 - fe - fx
            pnl, pfix = eq * a.lev * r / 100, a.capital * a.lev * r / 100
            day_pnl += pnl
            day_fix += pfix
            rows_out.append({"날짜": d.isoformat(), "종목": tr.symbol, "방향": "롱" if tr.side == 1 else "숏",
                             "진입": tr.entry_t.strftime("%H:%M"), "진입가": round(tr.entry, 4),
                             "청산": tr.exit_t.strftime("%H:%M"), "청산가": round(tr.exit, 4),
                             "결과": {"target": "익절", "stop": "손절", "time": "시간 정리"}[tr.reason],
                             "수익률%": round(r, 2), "손익_복리": round(pnl, 2), "손익_고정": round(pfix, 2)})
        eq += day_pnl
        fix += day_fix
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
        per_day.append((d, top, len(cands), [x[0].symbol for x in picked], day_pnl, eq, fix))
        print(f"{d} 상위 {len(top)} 신호 {len(cands)} 거래 {', '.join(x[0].symbol for x in picked) or '-'}"
              f" 손익 {day_pnl:+.2f} → {eq:.2f} (고정 {fix:.2f})", flush=True)

    out = Path("reports/okx_fvg_month")
    out.mkdir(parents=True, exist_ok=True)
    with open(out / f"trades_{d0}_{d1}.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows_out[0]) if rows_out else ["날짜"])
        w.writeheader()
        w.writerows(rows_out)
    wins = [x for x in rows_out if x["수익률%"] > 0]
    n = len(rows_out)
    print(f"\n거래 {n}번, 이익 {len(wins)} / 손실 {n - len(wins)}, 승률 {len(wins) / n * 100 if n else 0:.0f}%")
    if n:
        print(f"거래당 평균 {sum(x['수익률%'] for x in rows_out) / n:+.2f}% (배율 전, 비용 뺀 뒤)")
    print(f"복리: {a.capital:.0f} → {eq:.2f} USDT ({(eq / a.capital - 1) * 100:+.1f}%), 최대 낙폭 {mdd * 100:.1f}%")
    print(f"고정(매번 {a.capital:.0f}달러 x {a.lev:g}배): {a.capital:.0f} → {fix:.2f} USDT ({(fix / a.capital - 1) * 100:+.1f}%)")
    for k in ("익절", "손절", "시간 정리"):
        xs = [x for x in rows_out if x["결과"] == k]
        if xs:
            print(f"  {k} {len(xs)}번 평균 {sum(x['수익률%'] for x in xs) / len(xs):+.2f}%")


if __name__ == "__main__":
    main()
