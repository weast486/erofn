"""신고가 매수 종목의 투자자별 순매수 주체 분석 (연구용, 실전 봇과 무관).

매수 거래마다 KRX 투자자별 순매수 금액(기관합계·외국인합계·개인)을 받아
세 구간에서 순매수 금액이 가장 큰 쪽을 '주체'로 정하고, 주체별 성과를 비교한다.
  - 포함5일: 매수일 포함 직전 5거래일 (D-4 ~ D)
  - 전일5일: 매수 전날까지 5거래일 (D-5 ~ D-1)  ← 실전에서 15:10에 알 수 있는 정보
  - 이후5일: 매수 다음날부터 5거래일 (D+1 ~ D+5)

필요: 환경 변수 KRX_ID / KRX_PW (data.krx.co.kr 계정, pykrx 가 읽음)
실행:
  python -m tossbot.backtest run ... (CLAUDE.md 추천 설정) → backtest_results/trades.csv
  python scripts/investor_flow.py --years 2023 2024
결과: reports/investor_flow/ (거래별 CSV, 요약 MD). KRX 응답은 data/investor_flow/ 에 캐시.
"""
import argparse
import os
import sys
import time

import pandas as pd

INVESTORS = ["기관합계", "외국인합계", "개인"]
LABEL = {"기관합계": "기관", "외국인합계": "외국인", "개인": "개인"}
WINDOWS = {"포함5일": (-4, 0), "전일5일": (-5, -1), "이후5일": (1, 5)}


def fetch_krx(code, start, end):
    from pykrx import stock
    df = stock.get_market_trading_value_by_date(start, end, code)
    if df is None or df.empty:
        return pd.DataFrame()
    df.index = pd.to_datetime(df.index)
    return df


def load_flow(code, start, end, cache_dir, fetch=fetch_krx):
    path = os.path.join(cache_dir, f"{code}_{start}_{end}.csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    df = fetch(code, start, end)
    if not df.empty:
        os.makedirs(cache_dir, exist_ok=True)
        df.to_csv(path)
    time.sleep(0.3)
    return df


def classify(flow, buy_date):
    """거래 1건: 구간별 투자자 순매수 합계와 주체."""
    out = {}
    if flow.empty or buy_date not in flow.index:
        return None
    i = flow.index.get_loc(buy_date)
    for name, (a, b) in WINDOWS.items():
        if i + a < 0 or i + b >= len(flow):
            out[f"{name}_주체"] = None
            continue
        s = flow.iloc[i + a:i + b + 1][INVESTORS].sum()
        for inv in INVESTORS:
            out[f"{name}_{LABEL[inv]}(억)"] = round(s[inv] / 1e8, 2)
        out[f"{name}_주체"] = LABEL[s.idxmax()]
        out[f"{name}_쌍끌이"] = bool(s["기관합계"] > 0 and s["외국인합계"] > 0)
    return out


def analyze(trades, cache_dir, fetch=fetch_krx):
    rows = []
    for code, g in trades.groupby("종목코드"):
        yrs = pd.to_datetime(g["매수일"]).dt.year
        start = f"{yrs.min() - 1}1201"
        end = f"{yrs.max() + 1}0131"
        flow = load_flow(code, start, end, cache_dir, fetch)
        for _, t in g.iterrows():
            c = classify(flow, pd.Timestamp(t["매수일"]))
            rows.append({**t.to_dict(), **(c or {})})
    return pd.DataFrame(rows)


def summarize(df):
    closed = df[df["사유"] != "보유중"]
    parts = []
    for w in WINDOWS:
        col = f"{w}_주체"
        if col not in closed:
            continue
        for yr, g in list(closed.groupby("연도")) + [("합계", closed)]:
            for who, h in g.dropna(subset=[col]).groupby(col):
                parts.append({
                    "구간": w, "연도": yr, "주체": who, "거래수": len(h),
                    "비중": f"{len(h) / g[col].notna().sum():.0%}",
                    "익절비율": f"{(h['사유'] == 'TAKE_PROFIT').mean():.0%}",
                    "평균수익률": f"{h['수익률'].mean():+.2%}",
                    "합계손익": int(h["손익"].sum()),
                })
    return pd.DataFrame(parts)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--trades", default="backtest_results/trades.csv")
    p.add_argument("--years", type=int, nargs="+", default=[2023, 2024])
    p.add_argument("--cache", default="data/investor_flow")
    p.add_argument("--out", default="reports/investor_flow")
    a = p.parse_args(argv)
    if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
        sys.exit("KRX_ID / KRX_PW 환경 변수가 없습니다 (환경 설정에서 추가 후 새 세션).")
    trades = pd.read_csv(a.trades, dtype={"종목코드": str})
    trades = trades[trades["연도"].isin(a.years)]
    df = analyze(trades, a.cache)
    missing = df["포함5일_주체"].isna().sum() if "포함5일_주체" in df else len(df)
    os.makedirs(a.out, exist_ok=True)
    df.to_csv(os.path.join(a.out, "trades_investor.csv"), index=False, encoding="utf-8-sig")
    summ = summarize(df)
    summ.to_csv(os.path.join(a.out, "summary.csv"), index=False, encoding="utf-8-sig")
    print(f"거래 {len(df)}건, 데이터 없음 {missing}건")
    print(summ.to_string(index=False))


if __name__ == "__main__":
    main()
