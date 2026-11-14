"""BTC·ETH: RSI 가 N 아래로 마감하면 주목 → 처음 종가가 5봉 이평 위로 마감한 봉 다음 시가 롱 → RSI >= 50 마감 다음 시가 매도.

python scripts/bn_rsi_ma5.py [--tf 4h|15m]
비교용으로 '지금 국내 RSI 규칙'(RSI < 30 마감하면 바로 다음 시가 매수)도 같이 계산.
비용 왕복 0.1%, 손절은 봉 저가로 판정(시가가 이미 아래면 시가).
계좌 = 두 종목에 평가금액 절반씩 따로 복리, 낙폭은 청산 손익 기준.
"""
import argparse
import csv
from datetime import datetime, timezone

COST = 0.001


def load(sym, tf):
    d = "data/binance_4h_crypto" if tf == "4h" else "data/binance_15m_crypto"
    rows = []
    with open(f"{d}/{sym}USDT_{tf}.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows.append((int(r["open_ms"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return rows


def rsi_series(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gain = loss = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gain += max(ch, 0)
        loss += max(-ch, 0)
    ag, al = gain / n, loss / n
    out[n] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(ch, 0)) / n
        al = (al * (n - 1) + max(-ch, 0)) / n
        out[i] = 100 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def trades(bars, mode, buy=25, sell=50, ma_n=5, stop=0.0, max_hold=0, watch=0):
    closes = [b[4] for b in bars]
    rsi = rsi_series(closes)
    out = []
    armed_at = None
    pos = None  # (entry_i, entry_price)
    i = 0
    n = len(bars)
    while i < n - 1:
        v = rsi[i]
        if pos is not None:
            ei, ep = pos
            b = bars[i]
            if stop and i > ei - 1:
                sp = ep * (1 - stop / 100)
                if b[1] <= sp or b[3] <= sp:
                    px = min(b[1], sp)
                    out.append((bars[ei][0], px / ep - 1 - COST, "stop"))
                    pos = None
                    armed_at = None
                    i += 1
                    continue
            if v is not None and v >= sell:
                out.append((bars[ei][0], bars[i + 1][1] / ep - 1 - COST, "rsi"))
                pos = None
            elif max_hold and i - ei + 1 >= max_hold:
                out.append((bars[ei][0], bars[i + 1][1] / ep - 1 - COST, "time"))
                pos = None
            i += 1
            continue
        if v is None:
            i += 1
            continue
        sig = False
        if mode == "base":
            sig = v < buy
        else:
            if v < buy:
                armed_at = i
            elif armed_at is not None:
                if watch and i - armed_at > watch:
                    armed_at = None
                elif i + 1 >= ma_n and closes[i] > sum(closes[i + 1 - ma_n:i + 1]) / ma_n:
                    sig = v < sell
                    armed_at = None
        if sig:
            pos = (i + 1, bars[i + 1][1])
            i += 1  # 다음 봉(진입 봉)부터 청산 판정
            continue
        i += 1
    return out


def summarize(label, per_sym):
    allt = sorted([t for ts in per_sym.values() for t in ts])
    if not allt:
        print(f"{label:<44} 거래 없음")
        return
    rets = [t[1] for t in allt]
    wins = sum(r > 0 for r in rets)
    avg = sum(rets) / len(rets)
    top = sorted(rets, reverse=True)[5:]
    ex5 = sum(top) / len(top) if top else 0
    # 계좌: 종목마다 절반씩 따로 복리, 날짜 순으로 합산 평가
    eq = {s: 0.5 for s in per_sym}
    events = sorted((t[0], s, t[1]) for s, ts in per_sym.items() for t in ts)
    peak, mdd = 1.0, 0.0
    by_year = {}
    for ms, s, r in events:
        eq[s] *= 1 + r
        tot = sum(eq.values())
        peak = max(peak, tot)
        mdd = min(mdd, tot / peak - 1)
        y = datetime.fromtimestamp(ms / 1000, timezone.utc).year
        by_year.setdefault(y, []).append(r)
    yr = " / ".join(f"{y} {sum(v)/len(v)*100:+.2f}%({len(v)})" for y, v in sorted(by_year.items()))
    print(f"{label:<44} {len(rets):>5}건 승률 {wins/len(rets)*100:4.0f}% 거래당 {avg*100:+.2f}% (5건 빼면 {ex5*100:+.2f}%) "
          f"최악 {min(rets)*100:+.1f}% 계좌 {(sum(eq.values())-1)*100:+.0f}% MDD {mdd*100:.0f}%")
    print(f"{'':<44} 연도별 {yr}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", default="4h", choices=["4h", "15m"])
    a = ap.parse_args()
    data = {s: load(s, a.tf) for s in ("BTC", "ETH")}
    hold = 20 * (6 if a.tf == "4h" else 96)  # 손절 변형의 최대 보유 = 20일
    cases = [
        ("RSI<25 주목→5선 위 매수→RSI50 (그대로)", dict(mode="ma", buy=25)),
        ("  + 손절 -10% · 최대 20일", dict(mode="ma", buy=25, stop=10, max_hold=hold)),
        ("  + 손절 -5% · 최대 20일", dict(mode="ma", buy=25, stop=5, max_hold=hold)),
        ("RSI<30 주목→5선 위 매수→RSI50", dict(mode="ma", buy=30)),
        ("RSI<20 주목→5선 위 매수→RSI50", dict(mode="ma", buy=20)),
        ("RSI<25 주목→10선 위 매수→RSI50", dict(mode="ma", buy=25, ma_n=10)),
        ("RSI<25 주목→5선 위 매수→RSI60", dict(mode="ma", buy=25, sell=60)),
        ("비교: RSI<25 마감 바로 매수→RSI50", dict(mode="base", buy=25)),
        ("비교: RSI<30 마감 바로 매수→RSI50", dict(mode="base", buy=30)),
        ("비교: RSI<30 바로 · 손절 -10% · 20일", dict(mode="base", buy=30, stop=10, max_hold=hold)),
    ]
    print(f"== {a.tf} BTC·ETH (비용 왕복 0.1%)")
    for label, kw in cases:
        per = {s: trades(b, **kw) for s, b in data.items()}
        summarize(label, per)
        for s, ts in per.items():
            if ts and label.endswith("(그대로)"):
                r = [t[1] for t in ts]
                print(f"{'':<44} {s} {len(r)}건 거래당 {sum(r)/len(r)*100:+.2f}%")


if __name__ == "__main__":
    main()
