"""대상 날짜의 15시대 1분봉만 토스에서 받아 data/minute_tail 에 저장 (조회만, 이어받기)."""
import sys, os, json, time as _t; sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
from pathlib import Path
from datetime import date, datetime, time, timedelta
from tossbot import backtest as bt
from tossbot.config import KST
import trade_chart
while datetime.now(KST).time() < time(15, 32) and datetime.now(KST).time() > time(14, 55):
    _t.sleep(20)
data = bt.load_cache(Path('data/ohlcv_long'))
full = {f[:-4] for f in os.listdir('data/minute')}
out = Path('data/minute_tail'); N = 20
todo = []
for sym, (name, B) in data.items():
    ch = bt.new_high_flags([x.close for x in B], N)
    for i in range(40, len(B)):
        b = B[i]
        if b.day < date(2023, 10, 1): continue
        ref = max(x.close for x in B[i - N:i])
        if b.high <= ref or ref > 100000 or any(ch[i - N:i]): continue
        if b.close * b.volume < 1.5e10 or sum(x.close * x.volume for x in B[i - 20:i]) / 20 < 3e9: continue
        k = f"{sym}_{b.day}"
        if k in full or (out / f"{k}.json").exists(): continue
        todo.append((b.day, sym))
todo.sort(reverse=True)
print(len(todo), 'to fetch', flush=True)
c = trade_chart.toss_client(); ok = empty = 0
for n, (day, sym) in enumerate(todo):
    before = datetime.combine(day, time(15, 40), KST).isoformat(timespec="milliseconds")
    try:
        raw = (c._request("GET", "/api/v1/candles", params={"symbol": sym, "interval": "1m", "count": 45,
                                                             "adjusted": "true", "before": before}) or {}).get("candles", [])
    except Exception as exc:  # noqa: BLE001
        print('ERR', sym, day, type(exc).__name__, str(exc)[:80], flush=True); _t.sleep(3); continue
    rows = []
    for x in raw:
        t = datetime.fromisoformat(x["timestamp"].replace("Z", "+00:00")).astimezone(KST)
        if t.date() == day:
            rows.append({"t": t.strftime("%H:%M"), "o": float(x["openPrice"]), "h": float(x["highPrice"]),
                         "l": float(x["lowPrice"]), "c": float(x["closePrice"]), "v": float(x.get("volume") or 0)})
    rows.sort(key=lambda r: r["t"])
    (out / f"{sym}_{day}.json").write_text(json.dumps(rows), encoding="utf-8")
    ok += bool(rows); empty += not rows
    if n % 200 == 0: print(n, ok, empty, day, flush=True)
    _t.sleep(0.2)
print('done', ok, empty, flush=True)
