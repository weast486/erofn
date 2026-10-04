"""미국 주식 '전날 급등 → 다음 날' 검증용 데이터 받기 (토스증권 Open API, 조회만·주문 없음).

    python scripts/download_us_surge.py [--start 2023-01-01] [--min-change 5] [--symbols AAPL TSLA] [--no-zip]

대상: data/binance/symbols.json 의 underlyingType == "EQUITY" 종목(baseAsset). 토스에 없는 티커는 건너뜀.
저장 (data/us_surge):
  symbols.csv                      바이낸스 이름 → 토스 티커 대응표
  daily/{SYMBOL}.csv               date,open,high,low,close,volume  (정규장, 분할 반영)
  days.csv                         symbol,event_date,prev_date,prev_close,prev_change_pct,prev_volume,prev_dollar_volume
  minute/{SYMBOL}_{YYYY-MM-DD}.csv time_et,open,high,low,close,volume  (이벤트 날 09:30~16:00 미국 동부, 봉 시작 시각)
  missing.csv                      못 받은 이벤트와 이유
끊기면 다시 실행하면 이어받음(이미 받은 분봉 파일·'분봉 없음'으로 확인된 날은 건너뜀).
끝나면 data/us_surge_1.zip ... (4MB 이하)로 묶음.
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

ET = ZoneInfo("America/New_York")
OUT = ROOT / "data" / "us_surge"
# 바이낸스 이름이 실제 미국 티커와 다른 것
TICKER_FIX = {"BRKB": "BRK.B"}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def find_events(daily: list[list], start: date, min_change: float) -> list[list]:
    """daily: [date,open,high,low,close,volume] 날짜순. D 가 전날 종가 대비 +min_change%↑ 이면 다음 거래일이 이벤트."""
    out = []
    for i in range(1, len(daily) - 1):
        prev_close, close, vol = float(daily[i - 1][4]), float(daily[i][4]), float(daily[i][5])
        if prev_close <= 0:
            continue
        chg = (close / prev_close - 1) * 100
        event = daily[i + 1][0]
        if chg >= min_change and event >= start.isoformat():
            out.append([event, daily[i][0], daily[i][4], round(chg, 3), daily[i][5], round(close * vol, 2)])
    return out


def minute_rows(raw: list[dict], d: date) -> dict[str, list]:
    """토스 1분봉(시각 = 봉이 끝나는 시각) → d 일 정규장 봉 {봉 시작 시각: 행}."""
    out = {}
    for c in raw:
        t = _ts(c["timestamp"]).astimezone(ET) - timedelta(minutes=1)
        if t.date() == d and "09:30" <= t.strftime("%H:%M") < "16:00":
            key = t.strftime("%Y-%m-%d %H:%M")
            out[key] = [key, c["openPrice"], c["highPrice"], c["lowPrice"], c["closePrice"], c["volume"]]
    return out


def fix_split_scale(events: list[list]) -> int:
    """토스 1분봉은 오래된 날의 분할(병합)이 반영 안 된 경우가 있다 → 일봉(분할 반영)과 고가가 20% 넘게 다르면
    종목별 배율(중간값)로 가격을 곱하고 거래량을 나눈다. 고친 파일은 rescaled.csv 에 기록. 다시 실행해도 한 번만 적용."""
    import math
    import statistics

    daily: dict[str, dict[str, float]] = {}
    ratios: dict[str, list[tuple[Path, str, float]]] = {}
    for e in events:
        sym, d = e[0], e[1]
        path = OUT / "minute" / f"{sym}_{d}.csv"
        if not path.exists():
            continue
        if sym not in daily:
            with open(OUT / "daily" / f"{sym}.csv", encoding="utf-8") as f:
                daily[sym] = {r["date"]: float(r["high"]) for r in csv.DictReader(f)}
        with open(path, encoding="utf-8") as f:
            hi = max((float(r["high"]) for r in csv.DictReader(f)), default=0.0)
        if hi > 0 and d in daily[sym] and abs(math.log(daily[sym][d] / hi)) > 0.2:
            ratios.setdefault(sym, []).append((path, d, daily[sym][d] / hi))
    log_path = OUT / "rescaled.csv"
    new = not log_path.exists()
    n = 0
    with open(log_path, "a", newline="", encoding="utf-8") as lf:
        lw = csv.writer(lf)
        if new:
            lw.writerow(["symbol", "event_date", "price_factor"])
        for sym, items in ratios.items():
            factor = round(statistics.median(r for _, _, r in items), 3)
            for path, d, _ in items:
                with open(path, encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                out = [rows[0]] + [[r[0]] + [f"{float(x) * factor:.4f}" for x in r[1:5]] + [f"{float(r[5]) / factor:.0f}"]
                                   for r in rows[1:]]
                with open(path, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerows(out)
                lw.writerow([sym, d, factor])
                n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1), help="이벤트 날 시작")
    ap.add_argument("--min-change", type=float, default=5.0, help="전날 상승률 하한 %%")
    ap.add_argument("--symbols", nargs="*", help="바이낸스 목록 대신 직접 지정")
    ap.add_argument("--env", default=str(ROOT / ".env"))
    ap.add_argument("--interval", type=float, default=0.12, help="요청 사이 쉬는 시간(초)")
    ap.add_argument("--no-zip", action="store_true")
    a = ap.parse_args()

    from tossbot.backtest import split_zip
    from tossbot.client import TossClient
    from tossbot.config import Config, load_dotenv

    load_dotenv(a.env)
    cfg = Config.from_env()
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    (OUT / "daily").mkdir(parents=True, exist_ok=True)
    (OUT / "minute").mkdir(parents=True, exist_ok=True)

    def candles(symbol: str, interval: str, before: str | None = None) -> dict:
        time.sleep(a.interval)
        params = {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "true"}
        if before:
            params["before"] = before
        return client._request("GET", "/api/v1/candles", params=params) or {}

    # 1) 대상 종목
    if a.symbols:
        assets = [s.upper() for s in a.symbols]
    else:
        meta = json.loads((ROOT / "data/binance/symbols.json").read_text(encoding="utf-8"))["symbols"]
        assets = sorted({s["baseAsset"] for s in meta if s.get("underlyingType") == "EQUITY"})
    tickers = {s: TICKER_FIX.get(s, s) for s in assets}
    info = {s["symbol"]: s for s in client.get_stocks(sorted(set(tickers.values())))}
    with open(OUT / "symbols.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["binance_asset", "ticker", "found", "name", "type", "market"])
        for s in assets:
            i = info.get(tickers[s], {})
            w.writerow([s, tickers[s], "Y" if i else "N", i.get("englishName", ""), i.get("securityType", ""), i.get("market", "")])
    symbols = [tickers[s] for s in assets if tickers[s] in info]
    print(f"대상 {len(assets)}개 중 토스에 있는 종목 {len(symbols)}개 (없음: "
          f"{[s for s in assets if tickers[s] not in info]})", flush=True)

    # 2) 일봉 (오늘 이미 받은 파일은 다시 안 받음)
    today = datetime.now(ET).date()
    first = a.start - timedelta(days=45)
    events: list[list] = []
    for n, sym in enumerate(symbols, 1):
        path = OUT / "daily" / f"{sym}.csv"
        if path.exists() and date.fromtimestamp(path.stat().st_mtime) == date.today():
            with open(path, encoding="utf-8") as f:
                daily = [r for r in csv.reader(f)][1:]
        else:
            rows: dict[str, list] = {}
            before = None
            while True:
                res = candles(sym, "1d", before)
                got = res.get("candles", [])
                for c in got:
                    d = _ts(c["timestamp"]).astimezone(ET).date()
                    if d < today:
                        rows[d.isoformat()] = [d.isoformat(), c["openPrice"], c["highPrice"], c["lowPrice"],
                                               c["closePrice"], c["volume"]]
                before = res.get("nextBefore")
                if not got or not before or (rows and min(rows) < first.isoformat()):
                    break
            daily = [rows[k] for k in sorted(rows) if k >= first.isoformat()]
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["date", "open", "high", "low", "close", "volume"])
                w.writerows(daily)
        events += [[sym] + e for e in find_events(daily, a.start, a.min_change)]
        if n % 25 == 0 or n == len(symbols):
            print(f"  일봉 {n} / {len(symbols)}", flush=True)

    # 3) 이벤트 목록
    events.sort(key=lambda e: (e[1], e[0]))
    with open(OUT / "days.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "event_date", "prev_date", "prev_close", "prev_change_pct", "prev_volume", "prev_dollar_volume"])
        w.writerows(events)
    print(f"이벤트 {len(events)}건 (전날 +{a.min_change:g}%↑)", flush=True)

    # 4) 이벤트 날 1분봉
    empty_path = OUT / "minute" / "_empty.txt"  # 요청했지만 분봉이 없던 날 (다시 요청 안 함)
    empty = set(empty_path.read_text(encoding="utf-8").split()) if empty_path.exists() else set()
    todo = [e for e in events if not (OUT / "minute" / f"{e[0]}_{e[1]}.csv").exists() and f"{e[0]}_{e[1]}" not in empty]
    print(f"1분봉 받을 이벤트 {len(todo)}건 (이미 받음 {len(events) - len(todo)}건)", flush=True)
    errors: dict[str, str] = {}
    for n, e in enumerate(todo, 1):
        sym, d = e[0], date.fromisoformat(e[1])
        open_t = datetime(d.year, d.month, d.day, 9, 31, tzinfo=ET)
        before = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).isoformat(timespec="milliseconds")
        rows: dict[str, list] = {}
        try:
            for _ in range(6):  # 200분씩 (정규장 390분, 조기 마감일은 시간외 봉이 섞여 더 필요)
                raw = candles(sym, "1m", before).get("candles", [])
                rows.update(minute_rows(raw, d))
                if not raw:
                    break
                oldest = min(raw, key=lambda c: _ts(c["timestamp"]))["timestamp"]
                if _ts(oldest) <= open_t:
                    break
                before = oldest
        except Exception as exc:  # 다음 실행 때 다시 시도
            errors[f"{sym}_{e[1]}"] = f"요청 실패: {str(exc)[:80]}"
            continue
        if not rows:
            empty.add(f"{sym}_{e[1]}")
            with open(empty_path, "a", encoding="utf-8") as f:
                f.write(f"{sym}_{e[1]}\n")
            continue
        with open(OUT / "minute" / f"{sym}_{e[1]}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["time_et", "open", "high", "low", "close", "volume"])
            w.writerows(rows[k] for k in sorted(rows))
        if n % 100 == 0 or n == len(todo):
            print(f"  1분봉 {n} / {len(todo)}", flush=True)

    fixed = fix_split_scale(events)
    if fixed:
        print(f"분할이 반영 안 된 1분봉 {fixed}개를 일봉 기준으로 맞춤 → {OUT / 'rescaled.csv'}", flush=True)

    # 5) 못 받은 이벤트
    miss = []
    for e in events:
        key = f"{e[0]}_{e[1]}"
        if key in errors:
            miss.append([e[0], e[1], errors[key]])
        elif key in empty:
            miss.append([e[0], e[1], "토스에 그날 정규장 1분봉 없음"])
        elif not (OUT / "minute" / f"{key}.csv").exists():
            miss.append([e[0], e[1], "아직 안 받음"])
    with open(OUT / "missing.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "event_date", "reason"])
        w.writerows(miss)
    print(f"못 받은 이벤트 {len(miss)}건 → {OUT / 'missing.csv'}", flush=True)

    if not a.no_zip:
        keys = {f"{e[0]}_{e[1]}" for e in events}
        files = [OUT / "days.csv", OUT / "missing.csv", OUT / "symbols.csv", OUT / "rescaled.csv"]
        files += sorted((OUT / "daily").glob("*.csv"))
        files += sorted(p for p in (OUT / "minute").glob("*.csv") if p.stem in keys)
        parts = split_zip(files, OUT.parent / OUT.name)
        print(f"묶음 파일 {len(parts)}개: {parts[0]} ~ {parts[-1].name}")


if __name__ == "__main__":
    main()
