"""매매현황 페이지의 거래 차트: 봉 차트 위에 진입·손절·익절·청산 지점을 그린다.

status_server.py 가 아래 주소 요청에 page() 를 부른다.
  /chart/binance/{종목}/{날짜}/{day|night}   그날 1분봉 (종가 매매는 5분봉), 바이낸스 공개 시세
  /chart/kiwoom/{종목코드}/{YYYYMMDD}        그날 1분봉 (대상 종목·매매 종목), 토스 시세 조회
  /chart/toss/{종목코드}/{매수일}            일봉, 토스 시세 조회
지난 날의 봉은 각 봇 상태 폴더의 charts 에 저장해 두고 다시 쓴다. 조회만 하고 주문은 없다.
거래 정보는 봇이 남긴 상태 파일(day_날짜.json·overnight.json·trades.csv / daytrade_날짜.json / state.json)에서 읽는다.
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
from tossbot.config import KST  # noqa: E402

SDIR = ROOT / "state_binance"
KDIR = ROOT / "state_kiwoom"
TDIR = ROOT / "state"
LIB = "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
REASON = {"target": "익절", "stop": "손절", "time": "시간 정리", "night": "장 시작 정리", "night_stop": "밤사이 손절",
          "TAKE_PROFIT": "익절", "STOP_LOSS": "손절", "TIME_EXIT": "시간 정리", "RSI_EXIT": "RSI 매도", "MAX_HOLD": "보유 기간 끝"}


def esc(x) -> str:
    return html.escape(str(x))


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def parse_t(iso: str | None, tz=ET) -> datetime | None:
    try:
        return datetime.fromisoformat(iso).astimezone(tz) if iso else None
    except (TypeError, ValueError):
        return None


def local_sec(ms_or_dt, step: int, tz=ET) -> int:
    """차트는 시각을 UTC 로 그리므로 현지 시각이 그대로 보이게 옮기고, 봉 시작 시각에 맞춘다."""
    t = datetime.fromtimestamp(ms_or_dt / 1000, tz) if isinstance(ms_or_dt, (int, float)) else ms_or_dt.astimezone(tz)
    sec = int(t.timestamp() + t.utcoffset().total_seconds())
    return sec - sec % step


def hold_text(a: datetime | None, z: datetime | None) -> str:
    if not (a and z):
        return "-"
    mins = int((z - a).total_seconds() // 60)
    if mins >= 2 * 24 * 60:
        return f"{mins // (24 * 60)}일"
    return f"{mins // 60}시간 {mins % 60}분" if mins >= 60 else f"{mins}분"


# ------------------------------------------------------------------ 화면 틀
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
KEY_TEXT = {"entry": "진입가", "stop": "손절선", "tp": "익절선"}


def shell(tab_title: str, inner: str) -> str:
    return ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><style>body{margin:0}</style></head><body>\n'
            f'<title>{esc(tab_title)}</title>\n'
            '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">\n'
            f'<style>{make_status_page.CSS}{CHART_CSS}</style>\n<div class="wrap">'
            '<a class="back" href="/">← 매매현황으로</a>' + inner + '</div></body></html>\n')


def message(tab_title: str, text: str) -> str:
    return shell(tab_title, f'<div class="chartbox"><p class="msg">{esc(text)}</p></div>')


def render(tab_title: str, title: str, stamp: str, facts: list[tuple], bars: list[dict], lines: list[dict],
           marks: list[dict], side: int = 1, view: list | None = None, rng_label: str = "", mark_note: str = "",
           all_label: str = "하루 전체 보기", note: str = "", won: bool = False) -> str:
    """facts = [(이름, 값, 작은 글씨, 색 클래스)], lines = [{price, title, kind(entry/stop/tp/rng)}],
    marks = [{time, kind(entry/exit/fvg), text}] (time 은 bars 의 time 과 같은 형식)."""
    used, uniq = set(), []
    for ln in lines:  # 같은 가격의 선은 한 번만 (먼저 온 것 우선)
        if ln["price"] and round(ln["price"], 8) not in used:
            used.add(round(ln["price"], 8))
            uniq.append(ln)
    kinds = [k for k in ("entry", "stop", "tp") if any(ln["kind"] == k for ln in uniq)]
    data = {"bars": bars, "lines": uniq, "marks": sorted(marks, key=lambda m: str(m["time"])), "side": side, "view": view, "won": won}
    facts_html = '<dl class="facts">' + "".join(
        f'<div><dt>{esc(n)}</dt><dd class="{c}">{esc(v)}<small>{esc(s)}</small></dd></div>' for n, v, s, c in facts) + '</dl>'
    keys = ('<ul class="keys">' + "".join(f'<li class="k-{k}"><i></i>{KEY_TEXT[k]}</li>' for k in kinds)
            + (f'<li class="k-rng"><i></i>{esc(rng_label)}</li>' if rng_label and any(ln["kind"] == "rng" for ln in uniq) else '')
            + (f'<li>{esc(mark_note)}</li>' if mark_note and marks else '') + '</ul>')
    body = (f'<header class="top"><h1>{esc(title)}</h1><p class="stamp">{esc(stamp)}</p></header>' + facts_html
            + '<div class="chartbox"><div id="chart" role="img" aria-label="봉 차트에 진입·손절·익절·청산 지점 표시"></div>' + keys
            + f'<div class="bar"><button type="button" id="all">{esc(all_label)}</button>'
              '<span>마우스를 올리면 그 봉의 가격이 보이고, 휠로 확대·축소, 끌어서 이동할 수 있습니다.</span></div>'
            + (f'<p class="note">{esc(note)}</p>' if note else '') + '</div>'
            + ('<p class="msg">이 구간의 봉이 없어요.</p>' if not bars else ''))
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
var s=chart.addCandlestickSeries(D.won?{{priceFormat:{{type:'price',precision:0,minMove:1}}}}:{{}});var lines=[];
function paint(){{
 chart.applyOptions(theme());
 s.applyOptions({{upColor:v('--up'),borderUpColor:v('--up'),wickUpColor:v('--up'),downColor:v('--down'),borderDownColor:v('--down'),wickDownColor:v('--down')}});
 lines.forEach(function(l){{s.removePriceLine(l);}});lines=[];
 var col={{entry:v('--ink'),stop:v('--down'),tp:v('--up'),rng:v('--muted')}},sty={{entry:0,stop:2,tp:2,rng:1}};
 D.lines.forEach(function(l){{lines.push(s.createPriceLine({{price:l.price,color:col[l.kind],lineWidth:l.kind==='rng'?1:2,lineStyle:sty[l.kind],axisLabelVisible:true,title:l.title}}));}});
 s.setMarkers(D.marks.map(function(m){{
  if(m.kind==='entry')return{{time:m.time,position:D.side===1?'belowBar':'aboveBar',shape:D.side===1?'arrowUp':'arrowDown',color:v('--ink'),text:m.text,size:2}};
  if(m.kind==='exit')return{{time:m.time,position:D.side===1?'aboveBar':'belowBar',shape:'circle',color:v('--ink'),text:m.text,size:1}};
  return{{time:m.time,position:'aboveBar',shape:'square',color:v('--muted'),text:m.text,size:1}};}}));
}}
function fit(){{if(D.view)chart.timeScale().setVisibleRange({{from:D.view[0],to:D.view[1]}});else chart.timeScale().fitContent();}}
s.setData(D.bars);paint();fit();
document.getElementById('all').addEventListener('click',function(){{chart.timeScale().fitContent();}});
var first=true;new ResizeObserver(function(){{chart.applyOptions({{width:el.clientWidth,height:el.clientHeight}});if(first){{first=false;fit();}}}}).observe(el);
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change',paint);
}})();</script>"""
    return shell(tab_title, body + script)


# ------------------------------------------------------------------ 바이낸스
def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5 or d in US_HOLIDAYS:
        d += timedelta(days=1)
    return d


def csv_row(symbol: str, day: str, night: bool) -> dict | None:
    try:
        with open(SDIR / "trades.csv", newline="", encoding="utf-8") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("symbol") == symbol and r.get("day") == day
                    and str(r.get("reason", "")).startswith("night") == night]
    except OSError:
        return None
    return rows[-1] if rows else None


def load_trade(symbol: str, day: str, kind: str) -> dict | None:
    """차트에 그릴 바이낸스 거래 하나. 못 찾으면 None."""
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
    """바이낸스 [open_ms, o, h, l, c] 목록. 창이 다 지난 뒤 받은 것은 저장해 두고 다시 쓴다."""
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


def binance_page(symbol: str, day: str, kind: str) -> str:
    base = symbol.replace("USDT", "")
    tab = f"{base} {day} 거래 차트"
    t = load_trade(symbol, day, kind)
    if not t:
        return message(tab, f"{base} {day} 거래 기록을 찾지 못했어요.")
    raw = fetch_bars(symbol, t["start"], t["end"], t["interval"])
    step, side = t["step"], t["side"]
    ret = side * (t["exit"] / t["entry"] - 1) * 100 if t["entry"] and t["exit"] else 0.0
    lines = [{"price": t["entry"], "title": "진입", "kind": "entry"}, {"price": t["stop"], "title": "손절", "kind": "stop"},
             {"price": t["tp"], "title": "익절", "kind": "tp"}]
    if t["rng"]:
        lines += [{"price": t["rng"][0], "title": "첫 5분 고가", "kind": "rng"}, {"price": t["rng"][1], "title": "첫 5분 저가", "kind": "rng"}]
    marks = []
    if t["signal"] and kind == "day":
        hh, mm = (int(x) for x in t["signal"]["time"].split(":"))
        marks.append({"time": local_sec(datetime.combine(date.fromisoformat(day), time(hh, mm), ET), step), "kind": "fvg", "text": "FVG"})
    if t["entry_t"]:
        marks.append({"time": local_sec(t["entry_t"], step), "kind": "entry", "text": f"진입 {t['entry']:g}"})
    if t["exit_t"] and t["exit"]:
        marks.append({"time": local_sec(t["exit_t"], step), "kind": "exit", "text": f"{REASON.get(t['reason'], '청산')} {t['exit']:g}"})
    view = None
    if kind == "day" and marks:  # 처음에는 장 시작~청산 뒤 45분(적어도 1시간)만 크게 보여 줌
        t0 = local_sec(datetime.combine(date.fromisoformat(day), time(9, 20), ET), step)
        view = [t0, max(max(m["time"] for m in marks) + 45 * 60, t0 + 70 * 60)]
    facts = [("방향 · 결과", f'{"롱" if side == 1 else "숏"} · {REASON.get(t["reason"], t["reason"] or "-")}', "", ""),
             ("수익률", f"{ret:+.2f}%", "수수료 전", "up" if ret > 0 else "down" if ret < 0 else "flat"),
             ("진입 → 청산", f'{t["entry"]:g} → {t["exit"]:g}',
              f'{t["entry_t"].strftime("%H:%M") if t["entry_t"] else "-"} → {t["exit_t"].strftime("%m-%d %H:%M") if t["exit_t"] else "-"}', ""),
             ("손절 · 익절", f'{t["stop"]:g} · {(format(t["tp"], "g") if t["tp"] else "없음")}', f'보유 {hold_text(t["entry_t"], t["exit_t"])}', "")]
    return render(tab, f'{base} · {day} · {"종가 매매" if kind == "night" else "낮 전략(FVG)"}',
                  f'{"5분봉" if step == 300 else "1분봉"} · 시각은 미국 동부', facts,
                  [{"time": local_sec(b[0], step), "open": b[1], "high": b[2], "low": b[3], "close": b[4]} for b in raw],
                  lines, marks, side, view, "첫 5분봉 고가·저가",
                  "▲▼ 진입 · ● 청산" + (" · ■ FVG 가 생긴 봉" if t["signal"] and kind == "day" else ""),
                  "전체 구간 보기" if kind == "night" else "하루 전체 보기")


# ------------------------------------------------------------------ 국내 (토스 시세 조회)
_toss = []


def toss_client():
    if not _toss:
        from tossbot.client import TossClient
        from tossbot.config import Config, load_dotenv

        load_dotenv(str(ROOT / ".env"))
        cfg = Config.from_env()
        _toss.append(TossClient(cfg.client_id, cfg.client_secret, cfg.base_url, cfg.account_seq))
    return _toss[0]


def _ohlc(c: dict) -> tuple[float, float, float, float]:
    return float(c["openPrice"]), float(c["highPrice"]), float(c["lowPrice"]), float(c["closePrice"])


def kr_minute_bars(code: str, day: date) -> list[dict]:
    """그날 정규장 1분봉 [{t(봉 시작, KST), o, h, l, c}]. 토스 분봉의 시각은 봉이 끝나는 시각이라 1분 당긴다."""
    cache = KDIR / "charts" / f"{code}_{day:%Y%m%d}_1m.json"
    got = read_json(cache)
    if got:
        return [dict(b, t=datetime.fromisoformat(b["t"])) for b in got]
    client = toss_client()
    rows: dict[datetime, dict] = {}
    before = datetime.combine(day, time(15, 40), KST).isoformat(timespec="milliseconds")
    for _ in range(4):  # 200분씩 최대 4번
        raw = (client._request("GET", "/api/v1/candles", params={"symbol": code, "interval": "1m", "count": 200,
                                                                  "adjusted": "true", "before": before}) or {}).get("candles", [])
        for c in raw:
            start = datetime.fromisoformat(c["timestamp"].replace("Z", "+00:00")).astimezone(KST) - timedelta(minutes=1)
            if start.date() == day and time(9, 0) <= start.time() <= time(15, 30):
                o, h, l, cl = _ohlc(c)
                rows[start] = {"t": start, "o": o, "h": h, "l": l, "c": cl}
        if not raw or any(t.time() == time(9, 0) for t in rows) or min(c["timestamp"] for c in raw) < f"{day.isoformat()}T09":
            break
        before = min(c["timestamp"] for c in raw)
    out = [rows[t] for t in sorted(rows)]
    if out and datetime.now(KST).date() > day:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([dict(b, t=b["t"].isoformat()) for b in out]), encoding="utf-8")
    return out


def kr_daily_bars(code: str, count: int = 200) -> list[dict]:
    """일봉 [{day 'YYYY-MM-DD', o, h, l, c}] 오래된 순."""
    out = []
    for c in toss_client().get_candles(code, "1d", count):
        o, h, l, cl = _ohlc(c)
        out.append({"day": c["timestamp"][:10], "o": o, "h": h, "l": l, "c": cl})
    return sorted(out, key=lambda b: b["day"])


KW_STATUS = {"watch": "감시 중", "bought": "매수", "skip_gap": "갭상승 뒤 조건 안 맞음", "missed_chase": "너무 올라 추격 안 함",
             "full": "자리 다 참", "too_expensive": "금액 부족", "expired": "돌파 없이 매수 시간 끝"}


def kiwoom_page(code: str, ymd: str) -> str:
    day = datetime.strptime(ymd, "%Y%m%d").date()
    st = read_json(KDIR / f"daytrade_{ymd}.json") or {}
    cand = next((c for c in st.get("candidates", []) if c.get("code") == code), None)
    trades = [t for t in st.get("trades", []) if t.get("code") == code and float(t.get("entry_price") or 0) > 0]
    tr = trades[-1] if trades else None
    name = (cand or tr or {}).get("name", code)
    tab = f"{name} {day} 차트"
    if not cand and not tr:
        return message(tab, f"{code} {day} 기록(대상·매매)을 찾지 못했어요.")
    raw = kr_minute_bars(code, day)
    bars = [{"time": local_sec(b["t"], 60, KST), "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]} for b in raw]
    prev_close = float((cand or {}).get("prev_close") or 0)
    lines, marks, view = [], [], None
    t0 = local_sec(datetime.combine(day, time(9, 0), KST), 60)
    if tr:
        entry, exit_px = float(tr["entry_price"]), float(tr.get("exit_price") or 0)
        et, xt = parse_t(tr.get("ordered_at"), KST), parse_t(tr.get("exit_at"), KST)
        ret = (exit_px / entry - 1) * 100 if exit_px else 0.0
        lines += [{"price": entry, "title": "매수", "kind": "entry"}, {"price": float(tr.get("stop") or 0), "title": "손절", "kind": "stop"},
                  {"price": float(tr.get("target") or 0), "title": "익절", "kind": "tp"}]
        if et:
            marks.append({"time": local_sec(et, 60, KST), "kind": "entry", "text": f"매수 {entry:,.0f}"})
        if xt and exit_px:
            marks.append({"time": local_sec(xt, 60, KST), "kind": "exit", "text": f"{REASON.get(tr.get('exit_reason'), '매도')} {exit_px:,.0f}"})
        view = [t0, max(max((m["time"] for m in marks), default=t0) + 30 * 60, t0 + 60 * 60)]
        facts = [("결과", REASON.get(tr.get("exit_reason"), "보유 중" if not exit_px else "매도"), f'{int(tr.get("qty") or 0)}주', ""),
                 ("수익률", f"{ret:+.2f}%" if exit_px else "-", "수수료·세금 전", "up" if ret > 0 else "down" if ret < 0 else "flat"),
                 ("매수 → 매도", f'{entry:,.0f} → {exit_px:,.0f}' if exit_px else f"{entry:,.0f}",
                  f'{et.strftime("%H:%M") if et else "-"} → {xt.strftime("%H:%M") if xt else "-"}', ""),
                 ("손절 · 익절", f'{float(tr.get("stop") or 0):,.0f} · {float(tr.get("target") or 0):,.0f}', f"보유 {hold_text(et, xt)}", "")]
    else:
        view = [t0, t0 + 60 * 60]
        first = raw[0]["o"] if raw else 0.0
        gap = (first / prev_close - 1) * 100 if prev_close and first else 0.0
        facts = [("결과", KW_STATUS.get(cand.get("status"), cand.get("status", "-")), "매매 없음", ""),
                 ("전일 상승률", f'{float(cand.get("prev_change") or 0):+.1f}%', "기준봉", "up"),
                 ("전일 종가", f"{prev_close:,.0f}", "이 선을 아래에서 넘으면 매수", ""),
                 ("시가", f"{first:,.0f}" if first else "-", f"전일 종가 대비 {gap:+.1f}%" if first else "", "")]
    if prev_close:
        lines.append({"price": prev_close, "title": "전일 종가", "kind": "rng"})
    return render(tab, f"{name} · {day} · 키움 단타", "1분봉 · 한국 시각", facts, bars, lines, marks, 1, view,
                  "전일 종가 (돌파 기준)", "▲ 매수 · ● 매도",
                  note="매수는 9:05 까지만, 12:00 에 남은 수량을 정리합니다. 시세는 토스에서 조회해 키움 체결가와 조금 다를 수 있습니다.", won=True)


def toss_page(symbol: str, opened: str) -> str:
    state = read_json(TDIR / "state.json") or {}
    snap = read_json(TDIR / "매매현황.json") or {}
    pos = next((p for p in (state.get("positions") or {}).values()
                if p.get("symbol") == symbol and str(p.get("opened_at", ""))[:10] == opened), None)
    hist = next((h for h in reversed(state.get("history") or [])
                 if h.get("symbol") == symbol and str(h.get("opened_at", ""))[:10] == opened), None)
    t = hist or pos
    tab = f"{(t or {}).get('name', symbol)} {opened} 차트"
    if not t:
        return message(tab, f"{symbol} {opened} 매수 기록을 찾지 못했어요.")
    raw = kr_daily_bars(symbol, 200)
    bars = [{"time": b["day"], "open": b["o"], "high": b["h"], "low": b["l"], "close": b["c"]} for b in raw]
    entry, qty = float(t["entry_price"]), int(t.get("quantity") or 0)
    rsi = t.get("kind") == "rsi"
    sl = 10.0 if rsi else float(snap.get("stop_loss_pct") or 4.7)
    tp = 0.0 if rsi else float(snap.get("take_profit_pct") or 20.0)
    exit_px = float((hist or {}).get("exit_price") or 0)
    closed = str((hist or {}).get("closed_at", ""))[:10]
    last = raw[-1]["c"] if raw else entry
    now_px = exit_px or last
    ret = (now_px / entry - 1) * 100
    lines = [{"price": entry, "title": "매수", "kind": "entry"}, {"price": entry * (1 - sl / 100), "title": "손절", "kind": "stop"},
             {"price": entry * (1 + tp / 100) if tp else 0.0, "title": "익절", "kind": "tp"}]
    before = [b for b in raw if b["day"] < opened][-20:]
    if before and not rsi:
        lines.append({"price": max(b["c"] for b in before), "title": "직전 20일 최고 종가", "kind": "rng"})
    days = [b["day"] for b in raw]
    marks = []
    if opened in days:
        marks.append({"time": opened, "kind": "entry", "text": f"매수 {entry:,.0f}"})
    if closed in days and exit_px:
        marks.append({"time": closed, "kind": "exit", "text": f"{REASON.get(hist.get('reason'), '매도')} {exit_px:,.0f}"})
    view = None
    if opened in days:
        i = days.index(opened)
        view = [days[max(0, i - 60)], days[-1]]
    a, z = parse_t(t.get("opened_at"), KST), parse_t((hist or {}).get("closed_at"), KST) or datetime.now(KST)
    facts = [("구분 · 상태", f'{"RSI 평균회귀" if rsi else "20일 신고가"} · {REASON.get((hist or {}).get("reason"), "보유 중")}', f"{qty}주", ""),
             ("수익률", f"{ret:+.2f}%", "매도가 기준" if exit_px else "최근 종가 기준", "up" if ret > 0 else "down" if ret < 0 else "flat"),
             ("매수 → " + ("매도" if exit_px else "현재"), f"{entry:,.0f} → {now_px:,.0f}", f"{opened} → {closed or (days[-1] if days else '-')}", ""),
             ("손절 · 익절", f'{entry * (1 - sl / 100):,.0f} · ' + (f"{entry * (1 + tp / 100):,.0f}" if tp else "RSI 50↑ 매도"),
              f"보유 {hold_text(a, z)}", "")]
    return render(tab, f'{t.get("name", symbol)} · {opened} 매수 · 토스 스윙', "일봉 · 한국 시각", facts, bars, lines, marks, 1, view,
                  "직전 20일 최고 종가 (돌파 기준)", "▲ 매수 · ● 매도", "전체 기간 보기",
                  note="손절·익절선은 매수가와 설정 비율로 계산한 값이라 실제 조건주문 가격과 호가 단위만큼 다를 수 있습니다.", won=True)


# ------------------------------------------------------------------ 주소 → 페이지
def page(bot: str, *args: str) -> str:
    try:
        if bot == "binance":
            return binance_page(*args)
        if bot == "kiwoom":
            return kiwoom_page(*args)
        if bot == "toss":
            return toss_page(*args)
    except Exception as exc:  # noqa: BLE001
        return message("차트", f"차트를 만들지 못했어요 ({type(exc).__name__}: {str(exc)[:120]}). 인터넷 연결을 확인하고 새로고침해 주세요.")
    return message("차트", "모르는 주소예요.")


def chart_page(symbol: str, day: str, kind: str) -> str:  # 예전 이름 (바이낸스)
    return page("binance", symbol, day, kind)
