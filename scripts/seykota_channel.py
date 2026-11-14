"""에드 세이코타식 채널 돌파 추세추종 (4주 = 20거래일 돌파 진입 / 2주 = 10거래일 반대 돌파 청산).

규칙 (일봉):
  - 롱 진입: 오늘 고가가 직전 N_in(20)일 최고가를 넘으면 그 가격에 역지정가 매수
    (시가가 이미 위면 시가).
  - 롱 청산: 다음 날부터 저가가 직전 N_out(10)일 최저가 아래로 가면 그 가격에 매도
    (시가가 이미 아래면 시가). 손절·익절 따로 없음 (청산선이 곧 손절이자 트레일링).
  - 숏은 반대 (코인만).
계좌: 자리 N개, 진입 때 평가금액 / N 씩, 같은 날 신호는 전날 거래대금 큰 순.

사용:
  python scripts/seykota_channel.py kr      # 국내 주식 data/ohlcv_long
  python scripts/seykota_channel.py us      # 미국 주식 data/us_surge/daily (172종목, 생존 편향)
  python scripts/seykota_channel.py coin    # 바이낸스 USDT 무기한 코인 (폐지 포함) 4시간봉 → 일봉
  python scripts/seykota_channel.py btceth  # BTC·ETH 만
  공통 옵션: --in 20 --out 10 --slots 10 --side long|short|both --top 20
"""
from __future__ import annotations

import argparse
import glob
import os
import sys

import numpy as np
import pandas as pd


def load_kr():
    out = {}
    for f in glob.glob("data/ohlcv_long/*.csv"):
        d = pd.read_csv(f)
        if "date" not in d.columns:
            continue
        d["date"] = pd.to_datetime(d.date)
        d = d.set_index("date")
        d = d[(d.volume > 0) & (d.open > 0)]
        if len(d) < 60:
            continue
        d["amount"] = d.close * d.volume
        out[os.path.basename(f)[:-4]] = d
    return out


def load_us():
    out = {}
    for f in glob.glob("data/us_surge/daily/*.csv"):
        d = pd.read_csv(f, parse_dates=["date"]).set_index("date")
        d = d[d.open > 0]
        d["amount"] = d.close * d.volume
        out[os.path.basename(f)[:-4]] = d
    return out


def load_coin(only=None):
    out = {}
    for f in glob.glob("data/binance_4h_crypto/*_4h.csv"):
        sym = os.path.basename(f)[:-7]
        if only and sym not in only:
            continue
        d = pd.read_csv(f)
        if len(d) < 6 * 40:
            continue
        d["date"] = pd.to_datetime(d.open_ms, unit="ms").dt.floor("D")
        g = d.groupby("date")
        day = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                            "close": g.close.last(), "amount": g.quote_volume.sum(),
                            "n": g.size()})
        day = day[day.n == 6].drop(columns="n")  # 하루가 다 찬 봉만
        out[sym] = day
    return out


def make_signals(data, n_in, n_out, side):
    """종목마다 거래 목록(진입일·진입가·청산일·청산가·방향)을 만든다. 계좌 제약 없음."""
    trades = []
    for sym, d in data.items():
        o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
        dates = d.index
        hh_in = pd.Series(h).rolling(n_in).max().shift(1).values
        ll_in = pd.Series(l).rolling(n_in).min().shift(1).values
        hh_out = pd.Series(h).rolling(n_out).max().shift(1).values
        ll_out = pd.Series(l).rolling(n_out).min().shift(1).values
        pos = 0
        i = n_in
        while i < len(d):
            if pos == 0:
                if side in ("long", "both") and h[i] > hh_in[i]:
                    pos, ep, ei = 1, max(o[i], hh_in[i]), i
                elif side in ("short", "both") and l[i] < ll_in[i]:
                    pos, ep, ei = -1, min(o[i], ll_in[i]), i
                i += 1
                continue
            # 청산 (진입 다음 날부터)
            if pos == 1 and l[i] < ll_out[i]:
                xp = min(o[i], ll_out[i])
            elif pos == -1 and h[i] > hh_out[i]:
                xp = max(o[i], hh_out[i])
            else:
                i += 1
                continue
            trades.append((sym, dates[ei], ep, dates[i], xp, pos, False))
            pos = 0
            # 청산한 날 반대 방향 진입은 하지 않음(다음 날부터)
            i += 1
        if pos != 0:
            trades.append((sym, dates[ei], ep, dates[-1], c[-1], pos, True))
    return pd.DataFrame(trades, columns=["sym", "entry_date", "entry", "exit_date", "exit", "side", "open_end"])


def trade_ret(t, cost_side):
    r = (t.exit / t.entry - 1) * t.side
    return r - 2 * cost_side


def portfolio(data, trades, slots, cost_side, rank_amount=True, top=None, filt=None):
    """자리 slots 개·진입 때 평가금액/slots. 매일 종가로 평가."""
    # 전날 거래대금 순위·필터
    amt = pd.DataFrame({s: d.amount for s, d in data.items()})
    prev_amt = amt.shift(1)
    if top:
        rank = prev_amt.rank(axis=1, ascending=False)
    closes = pd.DataFrame({s: d.close for s, d in data.items()})
    days = closes.index
    t = trades.copy()
    t["pamt"] = [prev_amt.at[r.entry_date, r.sym] for r in t.itertuples()]
    keep = t.pamt.notna()
    if top:
        t["rank"] = [rank.at[r.entry_date, r.sym] for r in t.itertuples()]
        keep &= t["rank"] <= top
    if filt is not None:
        keep &= np.array([filt(data[r.sym], r.entry_date) for r in t.itertuples()], dtype=bool)
    t = t[keep].sort_values(["entry_date", "pamt"], ascending=[True, False])
    by_day = {k: g for k, g in t.groupby("entry_date")}
    exits_by_day = {}
    cash = 1.0
    open_pos = {}  # id -> (sym, side, qty_value_at_entry, entry, exit_date, exit)
    eq = []
    taken = []
    for day in days:
        # 청산
        for pid in [p for p, v in open_pos.items() if v["exit_date"] == day]:
            v = open_pos.pop(pid)
            r = (v["exit"] / v["entry"] - 1) * v["side"]
            cash += v["val"] * (1 + r) * (1 - cost_side)
        # 진입
        if day in by_day:
            for r in by_day[day].itertuples():
                if len(open_pos) >= slots:
                    break
                if any(v["sym"] == r.sym for v in open_pos.values()):
                    continue
                # 지금 평가금액
                total = cash + sum(_mark(v, closes, day) for v in open_pos.values())
                val = min(total / slots, cash)
                if val <= 0:
                    break
                cash -= val
                open_pos[r.Index] = dict(sym=r.sym, side=r.side, val=val * (1 - cost_side), entry=r.entry,
                                         exit_date=r.exit_date, exit=r.exit)
                taken.append(r.Index)
                # 같은 날 청산(드묾)은 make_signals 에서 안 생김
        total = cash + sum(_mark(v, closes, day) for v in open_pos.values())
        eq.append(total)
    eq = pd.Series(eq, index=days)
    return eq, t.loc[taken]


def _mark(v, closes, day):
    c = closes.at[day, v["sym"]]
    if np.isnan(c):
        return v["val"]  # 그날 봉 없음 → 진입 금액으로(근사)
    r = (c / v["entry"] - 1) * v["side"]
    return v["val"] * (1 + r)


def summary(tr, cost_side, label):
    if len(tr) == 0:
        print(f"{label}: 거래 없음")
        return
    r = trade_ret(tr, cost_side).sort_values()
    hold = (tr.exit_date - tr.entry_date).dt.days
    wins = r[r > 0]
    loss = r[r <= 0]
    cut5 = r.iloc[:-5].mean() if len(r) > 10 else float("nan")
    print(f"{label}: {len(r)}건 승률 {len(wins)/len(r)*100:.0f}% 거래당 {r.mean()*100:+.2f}% "
          f"(5건 빼면 {cut5*100:+.2f}%) 이익평균 {wins.mean()*100:+.1f}% 손실평균 {loss.mean()*100:+.1f}% "
          f"최고 {r.max()*100:+.0f}% 최악 {r.min()*100:+.0f}% 보유중간 {hold.median():.0f}일")
    yr = tr.assign(r=trade_ret(tr, cost_side)).groupby(tr.entry_date.dt.year).r.agg(["count", "mean"])
    print("   연도별 거래당: " + " / ".join(f"{y} {int(n)}건 {m*100:+.2f}%" for y, (n, m) in yr.iterrows()))


def eq_summary(eq, label):
    mdd = (eq / eq.cummax() - 1).min()
    yrs = []
    for y, g in eq.groupby(eq.index.year):
        start = eq[eq.index < g.index[0]]
        base = start.iloc[-1] if len(start) else 1.0
        yrs.append(f"{y} {(g.iloc[-1]/base-1)*100:+.0f}%")
    print(f"   계좌 {label}: {(eq.iloc[-1]-1)*100:+.0f}% (MDD {mdd*100:.0f}%) | " + " / ".join(yrs))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("market", choices=["kr", "us", "coin", "btceth"])
    ap.add_argument("--in", dest="n_in", type=int, default=20)
    ap.add_argument("--out", dest="n_out", type=int, default=10)
    ap.add_argument("--slots", type=int, default=10)
    ap.add_argument("--side", default=None)
    ap.add_argument("--top", type=int, default=None)
    ap.add_argument("--cost", type=float, default=None, help="한쪽 비용 %%")
    ap.add_argument("--start", default=None)
    a = ap.parse_args()

    if a.market == "kr":
        data = load_kr()
        side = a.side or "long"
        cost = (a.cost if a.cost is not None else 0.215) / 100  # 수수료+매도세 절반+슬리피지 근사
        def filt(d, day):
            w = d.loc[:day].iloc[-21:-1]
            return len(w) == 20 and w.amount.mean() >= 3e9 and w.close.iloc[-1] >= 1000
    elif a.market == "us":
        data = load_us()
        side = a.side or "long"
        cost = (a.cost if a.cost is not None else 0.1) / 100
        filt = None
    else:
        data = load_coin({"BTCUSDT", "ETHUSDT"} if a.market == "btceth" else None)
        side = a.side or "both"
        cost = (a.cost if a.cost is not None else 0.1) / 100
        def filt(d, day):  # 상장 180일↑
            return (day - d.index[0]).days >= 180 if a.market == "coin" else True
    if a.start:
        data = {s: d[d.index >= a.start] for s, d in data.items()}
        data = {s: d for s, d in data.items() if len(d) > a.n_in + 5}
    top = a.top if a.top is not None else (20 if a.market == "coin" else None)
    slots = a.slots if a.market != "btceth" else min(a.slots, 2)

    print(f"[{a.market}] 진입 {a.n_in}일 / 청산 {a.n_out}일, 방향 {side}, 자리 {slots}, 상위 {top}, 한쪽 비용 {cost*100:.3f}%, 종목 {len(data)}")
    tr = make_signals(data, a.n_in, a.n_out, side)
    eq, taken = portfolio(data, tr, slots, cost, top=top, filt=filt)
    allowed = tr  # 계좌 제약 없는 전체 신호(필터 전)
    summary(allowed, cost, "신호 전부(필터 전)")
    summary(taken, cost, "계좌가 실제로 산 거래")
    for s in ([1, -1] if side == "both" else []):
        summary(taken[taken.side == s], cost, "  롱" if s == 1 else "  숏")
    eq_summary(eq, f"{slots}자리")


if __name__ == "__main__":
    sys.exit(main())
