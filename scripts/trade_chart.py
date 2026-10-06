"""매매현황 페이지의 거래 차트 (바이낸스): 그날 봉 차트 위에 진입·손절·익절·청산 지점을 그린다.

status_server.py 가 /chart/binance/{종목}/{날짜}/{day|night} 요청에 chart_page() 를 부른다.
봉은 바이낸스 공개 시세(키 필요 없음)에서 받아 state_binance/charts 에 저장해 두고 다시 쓴다.
거래 정보는 봇이 남긴 day_날짜.json(낮 FVG), overnight.json 의 history(종가 매매), trades.csv 에서 읽는다.
"""
from __future__ import annotations

import csv
import html
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_status_page  # noqa: E402
from binancebot.backtest import ET, US_HOLIDAYS  # noqa: E402

SDIR = ROOT / "state_binance"
LIB = "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
REASON = {"target": "익절", "stop": "손절", "time": "시간 정리", "night": "장 시작 정리", "night_stop": "밤사이 손절"}


def esc(x) -> str:
    return html.escape(str(x))


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5 or d in US_HOLIDAYS:
        d += timedelta(days=1)
    return d


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def csv_row(symbol: str, day: str, night: bool) -> dict | None:
    try:
        with open(SDIR / "trades.csv", newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("symbol") == symbol and r.get("day") == day
                    and str(r.get("reason", "")).startswith("night") == night]
    except OSError:
        return None
    return rows[-1] if rows else None


def parse_t(iso: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(iso).astimezone(ET) if iso else None
    except (TypeError, ValueError):
        return None


def load_trade(symbol: str, day: str, kind: str) -> dict | None:
    """차트에 그릴 거래 하나. 못 찾으면 None."""
    d = date.fromisoformat(day)
    night = kind == "night"
    row = csv_row(symbol, day, night)
    if night:
        hist = [h for h in (read_json(SDIR / "overnight.json") or {}).get("history", [])
                if h.get("symbol") == symbol and h.get("day") == day]
        h = hist[-1] if hist else {}
        if not h and not row:
            return None
        g = lambda k: float(h.get(k) or (row or {}).get(k) or 0)  # noqa: E731
        exit_day = next_trading_day(d)
        return dict(symbol=symbol, day=day, kind=kind, side=1, entry=g("entry"), stop=g("stop"), tp=0.0, exit=g("exit"),
                    qty=g("qty"), reason=h.get("reason") or (row or {}).get("reason", "night"),
                    entry_t=parse_t(h.get("entry_time")) or datetime.combine(d, time(15, 58), ET),
                    exit_t=parse_t(h.get("exit_time")) or datetime.combine(exit_day, time(9, 30), ET),
                    start=datetime.combine(d, time(9, 30), ET), end=datetime.combine(exit_day, time(10, 30), ET),
                    interval="5m", step=300, rng=None, signal=None)
    st = read_json(SDIR / f"day_{day}.json") or {}
    pos = (st.get("positions") or {}).get(symbol) or {}
    res = pos.get("result") or {}
    if not pos and not row:
        return None
    g = lambda k, src=pos: float(src.get(k) or (row or {}).get(k) or 0)  # noqa: E731
    sig = (st.get("signals") or {}).get(symbol)
    return dict(symbol=symbol, day=day, kind=kind, side=int(g("side") or 1), entry=g("entry"), stop=g("stop"), tp=g("tp"),
                exit=float(res.get("exit") or (row or {}).get("exit") or 0), qty=g("qty"),
                reason=res.get("reason") or (row or {}).get("reason", ""),
                entry_t=parse_t(pos.get("entry_time")), exit_t=parse_t(res.get("exit_time")),
                start=datetime.combine(d, time(9, 0), ET), end=datetime.combine(d, time(16, 0), ET),
                interval="1m", step=60, rng=(st.get("ranges") or {}).get(symbol),
                signal={"level": float(sig[1]), "time": sig[2]} if sig else None)


def fetch_bars(symbol: str, start: datetime, end: datetime, interval: str) -> list[list]:
    """[open_ms, o, h, l, c] 목록. 창이 다 지난 뒤 받은 것은 저장해 두고 다시 쓴다."""
    cache = SDIR / "charts" / f"{symbol}_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}_{interval}.json"
    got = read_json(cache)
    if got:
        return got
    import requests

    cur, end_ms, out = int(start.timestamp() * 1000), int(end.timestamp() * 1000), []
    for _ in range(6):
        r = requests.get("https://fapi.binance.com/fapi/v1/klines", timeout=15,
                         params={"symbol": symbol, "interval": interval, "startTime": cur, "endTime": end_ms - 1, "limit": 1500})
        r.raise_for_status()
        part = r.json()
        if not part:
            break
        out += [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in part]
        cur = int(part[-1][0]) + 1
        if len(part) < 1500:
            break
    if out and datetime.now(ET) > end + timedelta(minutes=10):
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(out), encoding="utf-8")
    return out


def local_sec(ms_or_dt, step: int) -> int:
    """차트는 시각을 UTC 로 그리므로 미국 동부 시각이 그대로 보이게 옮기고, 봉 시작 시각에 맞춘다."""
    t = datetime.fromtimestamp(ms_or_dt / 1000, ET) if isinstance(ms_or_dt, (int, float)) else ms_or_dt.astimezone(ET)
    sec = int(t.timestamp() + t.utcoffset().total_seconds())
    return sec - sec % step


CHART_CSS = """
.back{color:var(--muted);font-size:13px;text-decoration:none} .back:hover{color:var(--ink)}
.back:focus-visible{outline:2px solid var(--down);outline-offset:2px}
.chartbox{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px;display:flex;flex-direction:column;gap:12px;min-width:0}
#chart{width:100%;height:min(62vh,560px);min-height:340px}
.keys{display:flex;flex-wrap:wrap;gap:6px 18px;margin:0;padding:0;list-style:none;font-size:13px;color:var(--muted)}
.keys i{display:inline-block;width:22px;height:0;border-top:2px solid var(--muted);vertical-align:middle;margin-right:6px}
.keys .k-entry i{border-color:var(--ink)} .keys .k-stop i{border-color:var(--down);border-top-style:dashed}
.keys .k-tp i{border-color:var(--up);border-top-style:dashed} .keys .k-rng i{border-top-style:dotted}
.msg{padding:28px;text-align:center;color:var(--muted)}
"""


def chart_page(symbol: str, day: str, kind: str) -> str:
    base = symbol.replace("USDT", "")
    head = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}</style></head><body>\n'
            f'<title>{esc(base)} {esc(day)} 거래 차트</title>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">\n'
            f'<style>{make_status_page.CSS}{CHART_CSS}</style>\n<div class="wrap">'
            '<a class="back" href="/">← 매매현황으로</a>')
    tail = '</div></body></html>\n'
    t = load_trade(symbol, day, kind)
    if not t:
        return head + f'<div class="chartbox"><p class="msg">{esc(base)} {esc(day)} 거래 기록을 찾지 못했어요.</p></div>' + tail
    try:
        bars = fetch_bars(symbol, t["start"], t["end"], t["interval"])
    except Exception as exc:  # noqa: BLE001
        return head + f'<div class="chartbox"><p class="msg">봉 차트를 받지 못했어요 ({esc(type(exc).__name__)}). 인터넷 연결을 확인하고 새로고침해 주세요.</p></div>' + tail
    step, side = t["step"], t["side"]
    ret = side * (t["exit"] / t["entry"] - 1) * 100 if t["entry"] and t["exit"] else 0.0
    tone = "up" if ret > 0 else "down" if ret < 0 else "flat"
    hold = ""
    if t["entry_t"] and t["exit_t"]:
        mins = int((t["exit_t"] - t["entry_t"]).total_seconds() // 60)
        hold = f"{mins // 60}시간 {mins % 60}분" if mins >= 60 else f"{mins}분"
    lines = [{"price": t["entry"], "title": "진입", "kind": "entry"}, {"price": t["stop"], "title": "손절", "kind": "stop"}]
    if t["tp"]:
        lines.append({"price": t["tp"], "title": "익절", "kind": "tp"})
    if t["rng"]:
        used = {round(x["price"], 8) for x in lines}  # 손절선과 겹치는 첫 봉 반대편은 한 번만 그림
        lines += [{"price": px, "title": ttl, "kind": "rng"} for px, ttl in ((t["rng"][0], "첫 5분 고가"), (t["rng"][1], "첫 5분 저가"))
                  if round(px, 8) not in used]
    marks = []
    if t["signal"] and kind == "day":
        hh, mm = (int(x) for x in t["signal"]["time"].split(":"))
        marks.append({"time": local_sec(datetime.combine(date.fromisoformat(day), time(hh, mm), ET), step), "kind": "fvg", "text": "FVG"})
    if t["entry_t"]:
        marks.append({"time": local_sec(t["entry_t"], step), "kind": "entry", "text": f"진입 {t['entry']:g}"})
    if t["exit_t"] and t["exit"]:
        marks.append({"time": local_sec(t["exit_t"], step), "kind": "exit", "text": f"{REASON.get(t['reason'], '청산')} {t['exit']:g}"})
    data = {"bars": [{"time": local_sec(b[0], step), "open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in bars],
            "lines": lines, "marks": sorted(marks, key=lambda m: m["time"]), "side": side}
    if kind == "day" and marks:  # 처음에는 장 시작~청산 뒤 45분(적어도 1시간)만 크게 보여 줌
        t0 = local_sec(datetime.combine(date.fromisoformat(day), time(9, 20), ET), step)
        data["view"] = [t0, max(max(m["time"] for m in marks) + 45 * 60, t0 + 70 * 60)]
    facts = (
        '<dl class="facts">'
        f'<div><dt>방향 · 결과</dt><dd>{"롱" if side == 1 else "숏"} · {esc(REASON.get(t["reason"], t["reason"] or "-"))}</dd></div>'
        f'<div><dt>수익률</dt><dd class="{tone}">{ret:+.2f}%<small>수수료 전</small></dd></div>'
        f'<div><dt>진입 → 청산</dt><dd>{t["entry"]:g} → {t["exit"]:g}<small>{t["entry_t"].strftime("%H:%M") if t["entry_t"] else "-"} → '
        f'{t["exit_t"].strftime("%m-%d %H:%M") if t["exit_t"] else "-"}</small></dd></div>'
        f'<div><dt>손절 · 익절</dt><dd>{t["stop"]:g} · {(format(t["tp"], "g") if t["tp"] else "없음")}<small>보유 {esc(hold or "-")}</small></dd></div>'
        '</dl>')
    keys = ('<ul class="keys"><li class="k-entry"><i></i>진입가</li><li class="k-stop"><i></i>손절선</li>'
            + ('<li class="k-tp"><i></i>익절선</li>' if t["tp"] else '')
            + ('<li class="k-rng"><i></i>첫 5분봉 고가·저가</li>' if t["rng"] else '')
            + '<li>▲▼ 진입 · ● 청산' + (' · ■ FVG 가 생긴 봉' if t["signal"] and kind == "day" else '') + '</li></ul>')
    title = f'{esc(base)} · {esc(day)} · {"종가 매매" if kind == "night" else "낮 전략(FVG)"}'
    body = (
        f'<header class="top"><h1>{title}</h1><p class="stamp">{"5분봉" if step == 300 else "1분봉"} · 시각은 미국 동부</p></header>'
        + facts
        + '<div class="chartbox"><div id="chart" role="img" aria-label="봉 차트에 진입·손절·익절·청산 지점 표시"></div>' + keys
        + '<div class="bar"><button type="button" id="all">하루 전체 보기</button>'
          '<span>마우스를 올리면 그 봉의 가격이 보이고, 휠로 확대·축소, 끌어서 이동할 수 있습니다.</span></div></div>'
        + (f'<p class="msg">이 구간의 봉이 없어요.</p>' if not bars else ''))
    script = f"""
<script src="{LIB}"></script>
<script>(function(){{
var D={json.dumps(data)};var el=document.getElementById('chart');
if(!window.LightweightCharts){{el.innerHTML='<p class="msg">차트 프로그램을 불러오지 못했어요. 인터넷 연결을 확인해 주세요.</p>';return;}}
function v(n){{return getComputedStyle(document.documentElement).getPropertyValue(n).trim();}}
function theme(){{return{{layout:{{background:{{type:'solid',color:v('--surface')}},textColor:v('--muted'),fontFamily:"IBM Plex Mono, Consolas, monospace"}},
grid:{{vertLines:{{color:v('--soft')}},horzLines:{{color:v('--soft')}}}},rightPriceScale:{{borderColor:v('--line')}},
timeScale:{{borderColor:v('--line'),timeVisible:true,secondsVisible:false}}}};}}
var chart=LightweightCharts.createChart(el,Object.assign({{width:el.clientWidth,height:el.clientHeight,crosshair:{{mode:0}}}},theme()));
var s=chart.addCandlestickSeries();var lines=[];
function paint(){{
 chart.applyOptions(theme());
 s.applyOptions({{upColor:v('--up'),borderUpColor:v('--up'),wickUpColor:v('--up'),downColor:v('--down'),borderDownColor:v('--down'),wickDownColor:v('--down')}});
 lines.forEach(function(l){{s.removePriceLine(l);}});lines=[];
 var col={{entry:v('--ink'),stop:v('--down'),tp:v('--up'),rng:v('--muted')}},sty={{entry:0,stop:2,tp:2,rng:1}};
 D.lines.forEach(function(l){{if(l.price)lines.push(s.createPriceLine({{price:l.price,color:col[l.kind],lineWidth:l.kind==='rng'?1:2,lineStyle:sty[l.kind],axisLabelVisible:true,title:l.title}}));}});
 s.setMarkers(D.marks.map(function(m){{
  if(m.kind==='entry')return{{time:m.time,position:D.side===1?'belowBar':'aboveBar',shape:D.side===1?'arrowUp':'arrowDown',color:v('--ink'),text:m.text,size:2}};
  if(m.kind==='exit')return{{time:m.time,position:D.side===1?'aboveBar':'belowBar',shape:'circle',color:v('--ink'),text:m.text,size:1}};
  return{{time:m.time,position:'aboveBar',shape:'square',color:v('--muted'),text:m.text,size:1}};}}));
}}
function fit(){{if(D.view)chart.timeScale().setVisibleRange({{from:D.view[0],to:D.view[1]}});else chart.timeScale().fitContent();}}
s.setData(D.bars);paint();fit();
var all=document.getElementById('all');if(all)all.addEventListener('click',function(){{chart.timeScale().fitContent();}});
var first=true;new ResizeObserver(function(){{chart.applyOptions({{width:el.clientWidth,height:el.clientHeight}});if(first){{first=false;fit();}}}}).observe(el);
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',paint);
}})();</script>"""
    return head + body + script + tail
