"""토스증권 Open API 로 미국 주식 일봉·정규장 1분봉 받기 (조회만, 주문 없음).

저장: data/tossus/{SYMBOL}_1m.csv.gz  (정규장 09:30~16:00 미국 동부 봉만, binancebot 과 같은 형식)
      data/tossus/{SYMBOL}_1d.csv     (일봉: date,open,high,low,close,volume)
      data/tossus/symbols.json        (종목 정보)
끊기면 다시 실행하면 이어받음.
"""
from __future__ import annotations

import csv
import gzip
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# 기본 대상: 대형주 + 인기 ETF (지금 인기 순위로 고르면 '오른 종목만' 뽑히므로 고정 목록)
DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "AMD", "NFLX",
                   "MU", "INTC", "PLTR", "COIN", "JPM", "SPY", "QQQ", "IWM", "TQQQ", "SOXL"]


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def last_saved_day(path: Path) -> date | None:
    if not path.exists():
        return None
    last = None
    try:
        with gzip.open(path, "rt", newline="") as f:
            for row in csv.reader(f):
                if row and row[0].isdigit():
                    last = int(row[0])
    except (EOFError, OSError):  # 받다가 끊겨 깨진 파일 → 처음부터
        path.unlink()
        return None
    return datetime.fromtimestamp(last / 1000, tz=timezone.utc).astimezone(ET).date() if last else None


def regular_bars(raw: list[dict], d: date) -> dict[int, list]:
    """토스 1분봉 응답에서 d 일 정규장(09:30~16:00 ET) 봉만 {시작 ms: 행}."""
    out = {}
    for c in raw:
        # 토스 1분봉 시각은 봉이 끝나는 시각 (09:31 봉 = 09:30~09:31, 장 시작 거래량이 여기 몰림) → 시작 시각으로 바꿈
        t = _ts(c["timestamp"]).astimezone(ET) - timedelta(minutes=1)
        if t.date() != d or not ("09:30" <= t.strftime("%H:%M") < "16:00"):
            continue
        ms = int(t.timestamp() * 1000)
        out[ms] = [ms, c["openPrice"], c["highPrice"], c["lowPrice"], c["closePrice"], c["volume"],
                   round(float(c["volume"]) * float(c["closePrice"]), 2), 0]
    return out


def download_all(out_dir: Path, symbols: list[str] | None = None, start: date | None = None,
                 env: str = ".env", request_interval: float = 0.12) -> None:
    from tossbot.client import TossClient
    from tossbot.config import Config, load_dotenv

    load_dotenv(env)
    cfg = Config.from_env()
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url)
    out_dir.mkdir(parents=True, exist_ok=True)
    start = start or date.today() - timedelta(days=365)
    symbols = symbols or DEFAULT_SYMBOLS

    def candles(symbol: str, interval: str, before: str | None = None) -> dict:
        time.sleep(request_interval)
        params = {"symbol": symbol, "interval": interval, "count": 200, "adjusted": "true"}
        if before:
            params["before"] = before
        return client._request("GET", "/api/v1/candles", params=params) or {}

    sj = out_dir / "symbols.json"
    known = {s["symbol"]: s for s in json.loads(sj.read_text(encoding="utf-8"))["symbols"]} if sj.exists() else {}
    known.update({s["symbol"]: s for s in client.get_stocks(symbols)})
    sj.write_text(json.dumps({"saved_at": datetime.now(timezone.utc).isoformat(), "symbols": list(known.values())},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"대상 {len(symbols)}개: " + ", ".join(symbols), flush=True)
    today = datetime.now(ET).date()

    for i, sym in enumerate(symbols, 1):
        # 일봉 (거래일 목록 겸용): start 60일 전부터
        daily: dict[date, list] = {}
        before = None
        while True:
            res = candles(sym, "1d", before)
            rows = res.get("candles", [])
            for c in rows:
                d = _ts(c["timestamp"]).astimezone(ET).date()
                daily[d] = [d.isoformat(), c["openPrice"], c["highPrice"], c["lowPrice"], c["closePrice"], c["volume"]]
            before = res.get("nextBefore")
            if not rows or not before or min(daily) < start - timedelta(days=60):
                break
        with open(out_dir / f"{sym}_1d.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            w.writerows(daily[d] for d in sorted(daily))
        path = out_dir / f"{sym}_1m.csv.gz"
        last = last_saved_day(path)
        days = [d for d in sorted(daily) if start <= d < today and (last is None or d > last)]
        print(f"[{i}/{len(symbols)}] {sym} 일봉 {len(daily)}일, 1분봉 받을 날 {len(days)}일", flush=True)
        new = not path.exists()
        with gzip.open(path, "at", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["open_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
            for n, d in enumerate(days, 1):
                bars: dict[int, list] = {}
                open_ms = int(datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).timestamp() * 1000)
                before = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).isoformat(timespec="milliseconds")
                try:
                    for _ in range(5):  # 200분씩 (정규장 390분, 조기 마감일은 시간외 봉이 섞여 더 필요)
                        raw = candles(sym, "1m", before).get("candles", [])
                        bars.update(regular_bars(raw, d))
                        if not raw:
                            break
                        oldest = min(raw, key=lambda c: _ts(c["timestamp"]))["timestamp"]
                        if _ts(oldest).timestamp() * 1000 <= open_ms:
                            break
                        before = oldest
                except Exception as exc:  # 이 날부터 다음 실행 때 이어받음
                    print(f"  {sym} {d} 실패: {exc} → 다시 실행하면 이어받음", flush=True)
                    break
                w.writerows(bars[k] for k in sorted(bars))
                if n % 50 == 0:
                    print(f"  {n} / {len(days)}", flush=True)
    print("완료:", out_dir)
