"""미국 시장 전체의 '전날 급등 → 다음 날' 데이터 받기 (조회만·주문 없음).

    python scripts/download_us_market_surge.py [--start 2023-10-02] [--min-change 20] [--min-amount 10000] [--min-price 1]

1) 키움 REST(usa24120 미국주식 특정일자 상승/하락)로 날짜별 급등 종목 → data/us_market/rank/{YYYYMMDD}.json
   (주식만, 거래대금 1천만 달러↑, 상승률 큰 순)
2) 다음 거래일 9:30~12:50(동부) 1분봉 200개 (before 는 그 시각에 끝나는 봉까지 포함)을 토스 Open API 로 → data/us_market/minute/{SYMBOL}_{YYYY-MM-DD}.csv
   (time_et = 봉 시작 시각, 분할 미반영 원래 가격 — 전날 종가도 키움의 그날 종가라 서로 맞음)
3) data/us_market/days.csv (scripts/us_kr_daytrade.py --data data/us_market 로 계산)
끊기면 다시 실행하면 이어받음. 한국 15:03~15:38 에는 쉰다 (토스·키움 봇의 주문 시간).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ET, KST = ZoneInfo("America/New_York"), ZoneInfo("Asia/Seoul")
OUT = ROOT / "data" / "us_market"


def pause_for_bots() -> None:
    while "15:03" <= datetime.now(KST).strftime("%H:%M") < "15:38" and datetime.now(KST).weekday() < 5:
        time.sleep(20)


def num(v) -> float:
    try:
        return abs(float(str(v).replace(",", "")))
    except ValueError:
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 10, 2))
    ap.add_argument("--end", type=date.fromisoformat, default=None)
    ap.add_argument("--min-change", type=float, default=20.0)
    ap.add_argument("--min-amount", type=float, default=10_000, help="전날 거래대금 하한 (천 달러)")
    ap.add_argument("--min-price", type=float, default=1.0, help="전날 종가 하한 (달러)")
    ap.add_argument("--rank-only", action="store_true")
    a = ap.parse_args()

    from tossbot.config import Config, load_dotenv
    from tossbot.client import TossClient
    from kiwoombot.client import KiwoomClient
    from kiwoombot.config import KiwoomConfig

    (OUT / "rank").mkdir(parents=True, exist_ok=True)
    (OUT / "minute").mkdir(parents=True, exist_ok=True)
    end = a.end or (datetime.now(ET).date() - timedelta(days=1))

    # 1) 날짜별 급등 목록 (키움)
    load_dotenv(str(ROOT / ".env.kiwoom"))
    kcfg = KiwoomConfig.from_env()
    kw = KiwoomClient(kcfg.app_key, kcfg.secret_key, mock=False)
    keep = a.min_change * 0.9
    d, n_new = a.start, 0
    while d <= end:
        ymd = d.strftime("%Y%m%d")
        path = OUT / "rank" / f"{ymd}.json"
        have = json.loads(path.read_text(encoding="utf-8")).get("keep", 18.0) if path.exists() else None
        if d.weekday() < 5 and (have is None or have > keep + 1e-9):  # 없거나, 더 높은 기준으로 받아 둔 날은 다시
            pause_for_bots()
            rows, key = [], ""
            for _ in range(12):
                body = {"stex_tp": "0", "inds_cd": "000", "stk_tp": "1", "stk_cnd": "0", "pric_cnd": "0",
                        "trde_qty_tp": "0", "trde_prica_cnd": "1000", "base_dt": ymd, "sort_tp": "0"}
                data, key = kw.request("usa24120", "/api/us/rkinfo", body, key)
                page = data.get("result_list") or []
                rows += page
                if not page or not key or num(page[-1].get("flu_rt")) < keep:
                    break
            out = [{"code": r["stk_cd"], "ex": r.get("stex_tp", ""), "name": r.get("stk_enm", ""), "close": num(r["cur_prc"]),
                    "open": num(r["open_pric"]), "high": num(r["high_pric"]), "low": num(r["low_pric"]),
                    "chg": float(r["flu_rt"]), "volume": num(r["acc_trde_qty"]), "amount_k": num(r["trde_prica"])}
                   for r in rows if r.get("base_dt") == ymd and float(r["flu_rt"]) >= keep]
            trading = any(r.get("base_dt") == ymd for r in rows)
            path.write_text(json.dumps({"trading": trading, "keep": keep, "rows": out}, ensure_ascii=False), encoding="utf-8")
            n_new += 1
            if n_new % 50 == 0:
                print(f"순위 {ymd} ({n_new}일 받음)", flush=True)
        d += timedelta(days=1)
    ranks = {}
    for p in sorted((OUT / "rank").glob("*.json")):
        j = json.loads(p.read_text(encoding="utf-8"))
        if j["trading"]:
            ranks[p.stem] = j["rows"]
    days = sorted(ranks)
    print(f"거래일 {len(days)}일의 급등 목록 준비됨", flush=True)

    # 2) 이벤트 = 급등 다음 거래일
    events = []
    for i, ymd in enumerate(days[:-1]):
        nxt = days[i + 1]
        for r in ranks[ymd]:
            if r["chg"] < a.min_change or r["close"] < a.min_price or r["amount_k"] < a.min_amount:
                continue
            events.append({"symbol": r["code"], "event_date": f"{nxt[:4]}-{nxt[4:6]}-{nxt[6:]}",
                           "prev_date": f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}", "prev_close": r["close"],
                           "prev_change_pct": r["chg"], "prev_volume": r["volume"], "prev_dollar_volume": r["amount_k"] * 1000,
                           "name": r["name"], "exchange": r["ex"]})
    with open(OUT / "days.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(events[0]))
        w.writeheader()
        w.writerows(events)
    print(f"이벤트 {len(events)}건 → days.csv", flush=True)
    if a.rank_only:
        return

    # 3) 이벤트 날 1분봉 (토스)
    load_dotenv(str(ROOT / ".env"))
    cfg = Config.from_env()
    toss = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    miss_path = OUT / "missing.json"
    missing = json.loads(miss_path.read_text(encoding="utf-8")) if miss_path.exists() else {}
    got = bad = 0
    for n, e in enumerate(events, 1):
        key = f"{e['symbol']}_{e['event_date']}"
        path = OUT / "minute" / f"{key}.csv"
        if path.exists() or key in missing:
            continue
        pause_for_bots()
        day = date.fromisoformat(e["event_date"])
        before = datetime(day.year, day.month, day.day, 12, 50, tzinfo=ET).isoformat(timespec="milliseconds")
        time.sleep(0.15)
        try:
            raw = (toss._request("GET", "/api/v1/candles", params={"symbol": e["symbol"], "interval": "1m", "count": 200,
                                                                   "adjusted": "false", "before": before}) or {}).get("candles", [])
        except Exception as exc:  # noqa: BLE001
            missing[key] = str(exc)[:80]
            bad += 1
            raw = None
        if raw is not None:
            rows = {}
            for c in raw:
                t = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).astimezone(ET) - timedelta(minutes=1)
                if t.date() == day and "09:30" <= t.strftime("%H:%M") < "16:00":
                    k = t.strftime("%Y-%m-%d %H:%M")
                    rows[k] = [k, c["openPrice"], c["highPrice"], c["lowPrice"], c["closePrice"], c["volume"]]
            if rows:
                with open(path, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["time_et", "open", "high", "low", "close", "volume"])
                    w.writerows(rows[k] for k in sorted(rows))
                got += 1
            else:
                missing[key] = "분봉 없음"
                bad += 1
        if n % 200 == 0:
            miss_path.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
            print(f"분봉 {n}/{len(events)} (새로 {got}, 없음 {bad})", flush=True)
    miss_path.write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")
    print(f"끝: 새로 받은 분봉 {got}, 없음 {bad}", flush=True)


if __name__ == "__main__":
    main()
