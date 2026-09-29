"""신고가 매수 종목의 투자자별 순매수 주체 분석 (연구용, 실전 봇과 무관).

매수 거래마다 투자자별 순매수 금액(기관합계·외국인합계·개인)을 받아
세 구간에서 순매수 금액이 가장 큰 쪽을 '주체'로 정하고, 주체별 성과를 비교한다.
  - 포함5일: 매수일 포함 직전 5거래일 (D-4 ~ D)
  - 전일5일: 매수 전날까지 5거래일 (D-5 ~ D-1)  ← 실전에서 15:10에 알 수 있는 정보
  - 이후5일: 매수 다음날부터 5거래일 (D+1 ~ D+5)

데이터 출처 (--source)
  toss (기본): 토스증권 Open API GET /api/v1/stocks/{symbol}/investor-trading. .env 의 TOSS_CLIENT_ID /
    TOSS_CLIENT_SECRET 사용 (읽기 전용 시세 조회, 주문 없음). 순매수 '주식 수'만 주므로 그날 종가를 곱해
    금액으로 근사한다 (종가는 수정주가라 액면분할 종목은 금액 크기만 달라지고, 주체 판정은 거의 같다).
    외국인은 등록외국인 기준.
  krx: pykrx (data.krx.co.kr 로그인 필요 → 환경 변수 KRX_ID / KRX_PW)

실행 (클라우드 연구 세션: 백테스트 후 입력 파일 준비 → 사용자 PC 에서 토스로 받기)
  python -m tossbot.backtest run ... (CLAUDE.md 추천 설정) → backtest_results/trades.csv
  python scripts/investor_flow.py --prepare     → reports/investor_flow/input_trades.csv, input_closes.csv
  python scripts/investor_flow.py               (사용자 PC, E:\\erofn 에서. 입력 파일이 있으면 그걸 사용)
결과: reports/investor_flow/ (trades_investor.csv 거래별, summary.csv 요약). 응답은 data/investor_flow/ 에 캐시.
"""
import argparse
import os
import sys
import time

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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


def toss_client(env_path=None):
    from tossbot.client import TossClient
    from tossbot.config import load_dotenv
    load_dotenv(env_path or os.path.join(ROOT, ".env"))
    return TossClient(os.environ.get("TOSS_CLIENT_ID", ""), os.environ.get("TOSS_CLIENT_SECRET", ""))


def toss_records_to_df(records):
    """토스 investor-trading records → 날짜별 순매수 주식 수 (기관합계·외국인합계·개인)."""
    rows = {}
    for r in records:
        def net(key):
            v = r.get(key) or {}
            return float(v.get("netBuyVolume") or 0)
        rows[pd.Timestamp(r["date"])] = {
            "기관합계": net("institution"), "외국인합계": net("foreigner"), "개인": net("individual")}
    df = pd.DataFrame.from_dict(rows, orient="index")
    return df.sort_index() if not df.empty else df


def fetch_toss(client, code, until, count=20):
    result = client._request("GET", f"/api/v1/stocks/{code}/investor-trading",
                             params={"count": count, "until": until})
    return toss_records_to_df((result or {}).get("records", []))


def load_flow_toss(client, code, buy_date, closes, cache_dir, fetch=fetch_toss):
    """매수일 D 주변(D-5 ~ D+5 이상) 순매수 주식 수를 받아 종가를 곱해 금액(원)으로 바꾼다."""
    until = (buy_date + pd.Timedelta(days=12)).strftime("%Y-%m-%d")
    path = os.path.join(cache_dir, f"toss_{code}_{until}.csv")
    if os.path.exists(path):
        vol = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        vol = fetch(client, code, until)
        time.sleep(0.2)
        if not vol.empty:
            os.makedirs(cache_dir, exist_ok=True)
            vol.to_csv(path)
    if vol.empty:
        return vol
    px = closes.get(code)
    if px is None:
        return pd.DataFrame()
    px = px.reindex(vol.index).ffill().bfill()
    return vol.mul(px, axis=0)


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


def analyze_toss(trades, closes, cache_dir, client=None, fetch=fetch_toss, env_path=None):
    client = client or toss_client(env_path)
    rows = []
    n = len(trades)
    for k, (_, t) in enumerate(trades.iterrows(), 1):
        d = pd.Timestamp(t["매수일"])
        try:
            flow = load_flow_toss(client, t["종목코드"], d, closes, cache_dir, fetch)
        except Exception as exc:  # 종목 1개 실패가 전체를 멈추지 않게
            print(f"  {t['종목코드']} {t['매수일']} 실패: {exc}")
            flow = pd.DataFrame()
        c = classify(flow, d)
        rows.append({**t.to_dict(), **(c or {})})
        if k % 50 == 0 or k == n:
            print(f"  {k}/{n} 완료")
    return pd.DataFrame(rows)


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


def prepare(trades, ohlcv_dir, out):
    """사용자 PC 용 입력 파일: 매매내역 + 매수일 전후 종가 (data/ 없이 실행 가능하게)."""
    rows = []
    for code, g in trades.groupby("종목코드"):
        px = pd.read_csv(os.path.join(ohlcv_dir, f"{code}.csv"), index_col="date", parse_dates=True)["close"]
        for d in pd.to_datetime(g["매수일"]):
            w = px[(px.index >= d - pd.Timedelta(days=25)) & (px.index <= d + pd.Timedelta(days=25))]
            rows += [{"종목코드": code, "date": i.strftime("%Y-%m-%d"), "close": v} for i, v in w.items()]
    closes = pd.DataFrame(rows).drop_duplicates(["종목코드", "date"]).sort_values(["종목코드", "date"])
    os.makedirs(out, exist_ok=True)
    trades.to_csv(os.path.join(out, "input_trades.csv"), index=False, encoding="utf-8-sig")
    closes.to_csv(os.path.join(out, "input_closes.csv"), index=False, encoding="utf-8-sig")
    print(f"입력 파일 준비: 거래 {len(trades)}건, 종가 {len(closes)}행 → {out}")


def read_closes(path):
    df = pd.read_csv(path, dtype={"종목코드": str}, parse_dates=["date"])
    return {c: g.set_index("date")["close"] for c, g in df.groupby("종목코드")}


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=["toss", "krx"], default="toss")
    p.add_argument("--trades", default=None, help="기본: reports/investor_flow/input_trades.csv, 없으면 backtest_results/trades.csv")
    p.add_argument("--years", type=int, nargs="+", default=[2023, 2024])
    p.add_argument("--cache", default="data/investor_flow")
    p.add_argument("--ohlcv", default="data/ohlcv_long")
    p.add_argument("--out", default="reports/investor_flow")
    p.add_argument("--prepare", action="store_true", help="입력 파일만 만든다 (클라우드 세션용)")
    p.add_argument("--env", default=None, help="토스 키가 든 .env 경로 (기본: 이 폴더의 .env)")
    p.add_argument("--limit", type=int, default=0, help="앞에서 N건만 (시험용)")
    a = p.parse_args(argv)
    if a.env:
        a.env = os.path.abspath(a.env)
    os.chdir(ROOT)
    input_trades = os.path.join(a.out, "input_trades.csv")
    path = a.trades or (input_trades if os.path.exists(input_trades) and not a.prepare
                        else "backtest_results/trades.csv")
    trades = pd.read_csv(path, dtype={"종목코드": str})
    trades = trades[trades["연도"].isin(a.years)]
    if a.limit:
        trades = trades.head(a.limit)
    if a.prepare:
        prepare(trades, a.ohlcv, a.out)
        return
    if a.source == "toss":
        closes = read_closes(os.path.join(a.out, "input_closes.csv"))
        df = analyze_toss(trades, closes, a.cache, env_path=a.env)
    else:
        if not (os.environ.get("KRX_ID") and os.environ.get("KRX_PW")):
            sys.exit("KRX_ID / KRX_PW 환경 변수가 없습니다 (환경 설정에서 추가 후 새 세션).")
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
