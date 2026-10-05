"""바이낸스 선물 공개 API 로 TradFi(미국 주식 등) 무기한 선물 1분봉·펀딩비 받기 (키 필요 없음, 주문 없음).

저장: data/binance/{SYMBOL}_1m.csv.gz  (평일 UTC 12:00~22:00 봉만 = 미국 정규장 앞뒤, 서머타임 무관)
      data/binance/{SYMBOL}_funding.csv
      data/binance/symbols.json        (대상 종목 exchangeInfo, 나머지 비코인 종목 요약)
끊기면 다시 실행하면 이어받음. 끝나면 4MB 이하 zip 여러 개(data/binance_1.zip ...)로 묶음.
"""
from __future__ import annotations

import csv
import gzip
import json
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://fapi.binance.com"
KEEP_UTC_HOURS = range(12, 22)  # 12:00~21:59 UTC


def _get(session, path: str, params: dict | None = None, tries: int = 5):
    for i in range(tries):
        try:
            r = session.get(BASE + path, params=params, timeout=20)
            if r.status_code in (418, 429):  # 요청 너무 많음 → 쉬고 다시
                time.sleep(int(r.headers.get("Retry-After", 30)))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # 네트워크 오류는 몇 번 재시도
            if i == tries - 1:
                raise
            print(f"  재시도 ({e})", flush=True)
            time.sleep(2 ** i)


def tradfi_symbols(info: dict) -> list[dict]:
    """코인이 아닌 USDT 무기한 선물(주식·ETF·원자재 등)."""
    out = []
    for s in info.get("symbols", []):
        if s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
            continue
        if "PERPETUAL" not in str(s.get("contractType", "")):
            continue
        utype = str(s.get("underlyingType", "")).upper()
        sub = [str(x).upper() for x in s.get("underlyingSubType") or []]
        ctype = str(s.get("contractType", "")).upper()
        if utype not in ("COIN", "INDEX", "") or "TRAD" in ctype or any("TRAD" in x or "STOCK" in x for x in sub):
            out.append(s)
    return out


def keep_bar(open_ms: int) -> bool:
    t = datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc)
    return t.weekday() < 5 and t.hour in KEEP_UTC_HOURS


def last_saved_ms(path: Path) -> int | None:
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
    return last


def download_klines(session, symbol: str, start_ms: int, out: Path) -> int:
    """start_ms 부터 지금까지 1분봉. 이미 받은 부분은 건너뜀. 저장한 봉 수 반환."""
    last = last_saved_ms(out)
    cur = (last + 60_000) if last else start_ms
    now = int(time.time() * 1000)
    n = 0
    new = not out.exists()
    with gzip.open(out, "at", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["open_ms", "open", "high", "low", "close", "volume", "quote_volume", "trades"])
        while cur < now - 60_000:
            rows = _get(session, "/fapi/v1/klines",
                        {"symbol": symbol, "interval": "1m", "startTime": cur, "limit": 1500})
            if not rows:
                break
            for k in rows:
                if int(k[6]) >= now:  # 아직 안 끝난 봉
                    continue
                if keep_bar(int(k[0])):
                    w.writerow([k[0], k[1], k[2], k[3], k[4], k[5], k[7], k[8]])
                    n += 1
            nxt = int(rows[-1][0]) + 60_000
            if nxt <= cur:
                break
            cur = nxt
            time.sleep(0.25)
    return n


def download_funding(session, symbol: str, start_ms: int, out: Path) -> None:
    rows, cur = [], start_ms
    while True:
        part = _get(session, "/fapi/v1/fundingRate", {"symbol": symbol, "startTime": cur, "limit": 1000})
        if not part:
            break
        rows += part
        if len(part) < 1000:
            break
        cur = int(part[-1]["fundingTime"]) + 1
        time.sleep(0.25)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["funding_ms", "rate", "mark_price"])
        for r in rows:
            w.writerow([r["fundingTime"], r["fundingRate"], r.get("markPrice", "")])


def download_all(out_dir: Path, symbols: list[str] | None = None, add: list[str] | None = None,
                 since: str = "2026-01-01") -> list[Path]:
    """add: TradFi 목록은 그대로 두고 이 종목(예 BTCUSDT ETHUSDT)만 since 부터 받아 따로 묶음(binance_extra_N.zip).
    symbols.json 에는 추가 종목 정보도 함께 저장."""
    import requests

    from tossbot.backtest import split_zip

    out_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    info = _get(session, "/fapi/v1/exchangeInfo")
    cand = tradfi_symbols(info)
    if add:
        want = {s.upper() for s in add}
        extra = [s for s in info["symbols"] if s["symbol"] in want]
        sj = out_dir / "symbols.json"
        old = json.loads(sj.read_text(encoding="utf-8")) if sj.exists() else {"symbols": cand}
        keep = [s for s in old["symbols"] if s["symbol"] not in want]
        sj.write_text(json.dumps(dict(old, symbols=keep + extra), ensure_ascii=False, indent=1), encoding="utf-8")
        start0 = int(datetime.fromisoformat(since).replace(tzinfo=timezone.utc).timestamp() * 1000)
        files = [sj]
        for s in extra:
            sym = s["symbol"]
            start = max(start0, int(s.get("onboardDate") or 0))
            print(f"{sym} {datetime.fromtimestamp(start / 1000, tz=timezone.utc):%Y-%m-%d} 부터", flush=True)
            k, fr = out_dir / f"{sym}_1m.csv.gz", out_dir / f"{sym}_funding.csv"
            print(f"  1분봉 {download_klines(session, sym, start, k)}개 추가", flush=True)
            download_funding(session, sym, start, fr)
            files += [k, fr]
        parts = split_zip([f for f in files if f.exists()], out_dir.parent / (out_dir.name + "_extra"))
        print(f"완료. 묶음 파일 {len(parts)}개: " + ", ".join(str(p) for p in parts))
        return parts
    if symbols:
        want = {s.upper() for s in symbols}
        cand = [s for s in info["symbols"] if s["symbol"] in want]
    types = sorted({(s.get("contractType"), s.get("underlyingType")) for s in info["symbols"]}, key=str)
    (out_dir / "symbols.json").write_text(json.dumps(
        {"saved_at": datetime.now(timezone.utc).isoformat(), "contract_underlying_types": types, "symbols": cand},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"대상 {len(cand)}개: " + ", ".join(s["symbol"] for s in cand), flush=True)
    files = [out_dir / "symbols.json"]
    for i, s in enumerate(cand, 1):
        sym = s["symbol"]
        start = int(s.get("onboardDate") or 0) or int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        print(f"[{i}/{len(cand)}] {sym} 상장 {datetime.fromtimestamp(start / 1000, tz=timezone.utc):%Y-%m-%d}", flush=True)
        k = out_dir / f"{sym}_1m.csv.gz"
        n = download_klines(session, sym, start, k)
        fr = out_dir / f"{sym}_funding.csv"
        download_funding(session, sym, start, fr)
        print(f"  1분봉 {n}개 추가", flush=True)
        files += [k, fr]
    parts = split_zip([f for f in files if f.exists()], out_dir.parent / out_dir.name)
    print(f"완료. 묶음 파일 {len(parts)}개: " + ", ".join(str(p) for p in parts))
    print("이 파일들을 모두 구글 드라이브에 올려 주세요.")
    return parts
