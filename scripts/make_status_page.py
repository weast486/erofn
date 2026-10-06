"""세 봇(토스 스윙·키움 단타·바이낸스)의 매매현황을 한 페이지(HTML)로 만든다.

봇이 남긴 상태 파일·로그만 읽는다 (API 조회·주문 없음, .env 는 드라이런 여부 한 줄만 확인).
  python scripts/make_status_page.py [--out state/매매현황_페이지.html] [--local]
바탕화면 바로가기용: windows\\status_page.bat (토스 현재가 조회 → 페이지 만들기 → 브라우저로 열기)
토스 현재가는 `windows\\export_trades.bat` 이 만든 state/매매현황.json 의 값을 쓴다.
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import re
import struct
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------ 읽기
def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def env_flag(path: Path, key: str) -> bool | None:
    """.env 파일에서 key=true/false 한 줄만 본다 (다른 값은 읽지 않음)."""
    for line in read_lines(path):
        m = re.match(rf"\s*{re.escape(key)}\s*=\s*(\w+)", line)
        if m:
            return m.group(1).lower() in ("1", "true", "yes", "on")
    return None


def icon_data_uri(path: Path) -> str:
    """ICO 안의 PNG(64px 우선)를 data URI 로."""
    try:
        b = path.read_bytes()
        n = struct.unpack_from("<H", b, 4)[0]
        best = None
        for i in range(n):
            w = b[6 + 16 * i] or 256
            size, off = struct.unpack_from("<II", b, 6 + 16 * i + 8)
            if best is None or abs(w - 64) < abs(best[0] - 64):
                best = (w, off, size)
        png = b[best[1]:best[1] + best[2]]
        return "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ 표시 도우미
def esc(x) -> str:
    return html.escape(str(x))


def won(x: float) -> str:
    return f"{x:,.0f}"


def signed(x: float, unit: str = "", digits: int = 0) -> str:
    return f"{x:+,.{digits}f}{unit}" if x else f"0{unit}"


def chart_link(on: bool, href: str, label: str) -> str:
    """현황 서버로 볼 때만 종목 이름을 차트 링크로 (아티팩트·파일로 볼 때는 그냥 이름)."""
    return f'<a href="{href}">{label} <span aria-hidden="true">↗</span></a>' if on else label


def tone(x: float) -> str:
    return "up" if x > 0 else "down" if x < 0 else "flat"


def short_dt(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m-%d %H:%M")
    except (TypeError, ValueError):
        return esc(iso or "-")


# ------------------------------------------------------------------ 토스
def toss_section(links: bool = False) -> tuple[str, str]:
    snap = read_json(ROOT / "state" / "매매현황.json") or {}
    state = read_json(ROOT / "state" / "state.json") or {}
    sl, tp = float(snap.get("stop_loss_pct") or 4.7), float(snap.get("take_profit_pct") or 20)
    prices = {p["symbol"]: p.get("last_price") for p in snap.get("positions", [])}
    positions = list((state.get("positions") or {}).values()) or snap.get("positions", [])
    history = state.get("history") or snap.get("history") or []
    live = env_flag(ROOT / ".env", "DRY_RUN") is False

    equity, equity_day = None, ""
    for line in reversed(read_lines(ROOT / "logs" / "tossbot.log")):
        m = re.search(r"^(\d{4}-\d{2}-\d{2}).*봇 평가금액 ([\d,]+)원", line)
        if m:
            equity_day, equity = m.group(1), float(m.group(2).replace(",", ""))
            break

    rows, cost_sum, value_sum = [], 0.0, 0.0
    for p in positions:
        entry, qty = float(p["entry_price"]), int(p["quantity"])
        last = float(prices.get(p["symbol"]) or p.get("last_price") or entry)
        ret = (last / entry - 1) * 100
        pnl = (last - entry) * qty
        cost_sum += entry * qty
        value_sum += last * qty
        rsi = p.get("kind") == "rsi"
        lo, hi = (-10.0, 10.0) if rsi else (-sl, tp)
        pos_pct = min(100.0, max(0.0, (ret - lo) / (hi - lo) * 100))
        zero_pct = (0 - lo) / (hi - lo) * 100
        gauge = (f'<div class="gauge" role="img" aria-label="손절 {lo:g}% 부터 {hi:g}% 사이에서 현재 {ret:+.1f}%">'
                 f'<span class="g-zero" style="left:{zero_pct:.1f}%"></span>'
                 f'<span class="g-dot {tone(ret)}" style="left:{pos_pct:.1f}%"></span></div>'
                 f'<div class="g-ends"><span>손절 {won(entry * (1 + lo / 100))}</span>'
                 f'<span>{"RSI 50↑ 매도" if rsi else "익절 " + won(entry * (1 + hi / 100))}</span></div>')
        rows.append(
            f'<tr><th scope="row">{chart_link(links, "/chart/toss/%s/%s" % (p["symbol"], str(p.get("opened_at", ""))[:10]), esc(p["name"]))}<small>{esc(p["symbol"])}</small></th>'
            f'<td><span class="tag">{"RSI" if rsi else "신고가"}</span></td>'
            f'<td class="num">{qty}</td><td class="num">{won(entry)}</td><td class="num">{won(last)}</td>'
            f'<td class="num {tone(ret)}">{signed(ret, "%", 2)}</td>'
            f'<td class="num {tone(pnl)}">{signed(pnl)}</td>'
            f'<td class="num">{int(p.get("hold_days", 0))}일</td><td class="gcell">{gauge}</td></tr>')

    unreal = value_sum - cost_sum
    unreal_pct = unreal / cost_sum * 100 if cost_sum else 0.0
    realized = sum((float(h["exit_price"]) - float(h["entry_price"])) * int(h.get("quantity", 0)) for h in history)

    reasons = {"STOP_LOSS": "손절", "TAKE_PROFIT": "익절", "RSI_EXIT": "RSI 매도", "MAX_HOLD": "보유 기간 끝", "MANUAL": "직접 매도"}
    hist_rows = [
        f'<tr><th scope="row">{chart_link(links, "/chart/toss/%s/%s" % (h["symbol"], str(h.get("opened_at", ""))[:10]), esc(h["name"]))}<small>{esc(h["symbol"])}</small></th>'
        f'<td><span class="tag">{"RSI" if h.get("kind") == "rsi" else "신고가"}</span></td>'
        f'<td class="num">{int(h.get("quantity", 0))}</td><td class="num">{won(h["entry_price"])}</td>'
        f'<td class="num">{won(h["exit_price"])}</td>'
        f'<td class="num {tone(h["return_pct"])}">{signed(h["return_pct"], "%", 2)}</td>'
        f'<td class="num {tone(h["return_pct"])}">{signed((h["exit_price"] - h["entry_price"]) * h.get("quantity", 0))}</td>'
        f'<td>{esc(reasons.get(h.get("reason"), h.get("reason", "")))}</td><td class="num">{short_dt(h.get("closed_at"))}</td></tr>'
        for h in reversed(history)]

    price_note = f'현재가는 {short_dt(snap.get("generated_at"))} 조회 값' if snap.get("generated_at") else "현재가 조회 기록 없음 (매수가로 표시)"
    summary = (
        f'<dl class="facts">'
        f'<div><dt>봇 평가금액</dt><dd>{won(equity) + "원" if equity else "-"}<small>{esc(equity_day)} 매수 때</small></dd></div>'
        f'<div><dt>보유</dt><dd>{len(positions)}종목<small>최대 {int(snap.get("num_stocks") or 10)}</small></dd></div>'
        f'<div><dt>평가손익</dt><dd class="{tone(unreal)}">{signed(unreal)}원<small>{signed(unreal_pct, "%", 2)}</small></dd></div>'
        f'<div><dt>실현손익</dt><dd class="{tone(realized)}">{signed(realized)}원<small>끝난 거래 {len(history)}건</small></dd></div>'
        f'</dl>')
    body = (
        f'<h3>보유 종목</h3><p class="note">{esc(price_note)}. 막대는 손절선과 익절선 사이에서 지금 위치, 세로선이 매수가입니다.</p>'
        f'<div class="scroll"><table><thead><tr><th>종목</th><th>구분</th><th class="num">수량</th><th class="num">매수가</th>'
        f'<th class="num">현재가</th><th class="num">수익률</th><th class="num">평가손익</th><th class="num">보유</th>'
        f'<th>손절 ↔ 익절</th></tr></thead><tbody>{"".join(rows) or empty_row(9, "보유 종목 없음")}</tbody></table></div>'
        f'<h3>끝난 거래</h3><div class="scroll"><table><thead><tr><th>종목</th><th>구분</th><th class="num">수량</th>'
        f'<th class="num">매수가</th><th class="num">매도가</th><th class="num">수익률</th><th class="num">손익</th>'
        f'<th>사유</th><th class="num">매도 시각</th></tr></thead><tbody>{"".join(hist_rows) or empty_row(9, "아직 끝난 거래 없음")}</tbody></table></div>')
    return bot_block("toss", "토스 스윙 봇", "국내 주식 · 20일 신고가 + RSI 평균회귀 · 15:10 종가 매수",
                     live, summary, body), ("실전" if live else "드라이런")


def empty_row(cols: int, text: str) -> str:
    return f'<tr><td colspan="{cols}" class="empty">{esc(text)}</td></tr>'


# ------------------------------------------------------------------ 키움
KW_STATUS = {"watch": "감시 중", "bought": "매수", "skip_gap": "갭상승 → 제외", "missed_chase": "너무 올라 추격 안 함",
             "full": "자리 다 참", "too_expensive": "금액 부족", "expired": "돌파 없이 시간 끝"}
KW_TRADE = {"pending": "주문 중", "open": "보유", "closing": "매도 중", "closed": "끝", "canceled": "취소"}


def kiwoom_section(links: bool = False) -> tuple[str, str]:
    days = sorted((ROOT / "state_kiwoom").glob("daytrade_*.json"))
    states = [s for s in (read_json(p) for p in days) if s]
    latest = states[-1] if states else {}
    dry = env_flag(ROOT / ".env.kiwoom", "KIWOOM_DRY_RUN")
    live = dry is False
    logs = read_lines(ROOT / "state_kiwoom" / "daytrade.log")
    setting = next((l.split("설정: ", 1)[1] for l in reversed(logs) if "설정: " in l), "")
    waiting = next((l.split(" ", 2)[2] for l in reversed(logs) if "다음 실행" in l), "")
    orderable = next((re.search(r"주문가능금액\(9시 직후\) ([\d,]+)원", l) for l in reversed(logs) if "9시 직후" in l), None)

    day = latest.get("date", "")
    day_txt = f"{day[:4]}-{day[4:6]}-{day[6:]}" if len(day) == 8 else "-"
    cand_rows = [
        f'<tr><th scope="row">{chart_link(links, "/chart/kiwoom/%s/%s" % (c["code"], day), esc(c["name"]))}<small>{esc(c["code"])}</small></th>'
        f'<td class="num up">{signed(c["prev_change"], "%", 1)}</td><td class="num">{won(c["prev_close"])}</td>'
        f'<td>{esc(KW_STATUS.get(c.get("status"), c.get("status", "")))}</td></tr>'
        for c in latest.get("candidates", [])]

    trade_rows, total_pnl, n_trades = [], 0.0, 0
    for s in reversed(states):
        d = s.get("date", "")
        for t in s.get("trades", []):
            qty, entry, exit_px = int(t.get("qty") or 0), float(t.get("entry_price") or 0), float(t.get("exit_price") or 0)
            if not qty:
                continue
            n_trades += 1
            ret = (exit_px / entry - 1) * 100 if entry and exit_px else 0.0
            pnl = (exit_px - entry) * qty if exit_px else 0.0
            total_pnl += pnl
            trade_rows.append(
                f'<tr><td class="num">{d[4:6]}-{d[6:]}</td><th scope="row">{chart_link(links, "/chart/kiwoom/%s/%s" % (t["code"], d), esc(t["name"]))}<small>{esc(t["code"])}</small></th>'
                f'<td class="num">{qty}</td><td class="num">{won(entry)}</td>'
                f'<td class="num">{won(exit_px) if exit_px else "-"}</td>'
                f'<td class="num {tone(ret)}">{signed(ret, "%", 2) if exit_px else "-"}</td>'
                f'<td class="num {tone(pnl)}">{signed(pnl) if exit_px else "-"}</td>'
                f'<td>{esc(t.get("exit_reason") or KW_TRADE.get(t.get("status"), ""))}</td></tr>')

    equity = float(latest.get("equity") or 0)
    summary = (
        f'<dl class="facts">'
        f'<div><dt>평가금액</dt><dd>{won(equity) + "원" if equity else "-"}<small>{esc(day_txt)} 기준</small></dd></div>'
        f'<div><dt>주문가능금액</dt><dd>{orderable.group(1) + "원" if orderable else "-"}<small>마지막 조회</small></dd></div>'
        f'<div><dt>단타 거래</dt><dd>{n_trades}건<small>기록 {len(states)}일</small></dd></div>'
        f'<div><dt>단타 손익</dt><dd class="{tone(total_pnl)}">{signed(total_pnl)}원<small>수수료·세금 전</small></dd></div>'
        f'</dl>')
    body = (
        (f'<p class="note">{esc(setting)}</p>' if setting else "")
        + (f'<p class="note">봇 창: {esc(waiting)}</p>' if waiting else "")
        + f'<h3>{esc(day_txt)} 대상 종목</h3><div class="scroll"><table><thead><tr><th>종목</th><th class="num">전일 상승률</th>'
          f'<th class="num">전일 종가</th><th>결과</th></tr></thead><tbody>{"".join(cand_rows) or empty_row(4, "대상 종목 없음")}</tbody></table></div>'
        + f'<h3>단타 거래 내역</h3><div class="scroll"><table><thead><tr><th class="num">날짜</th><th>종목</th><th class="num">수량</th>'
          f'<th class="num">매수가</th><th class="num">매도가</th><th class="num">수익률</th><th class="num">손익</th><th>상태</th></tr></thead>'
          f'<tbody>{"".join(trade_rows) or empty_row(8, "아직 거래 없음")}</tbody></table></div>'
        + '<p class="note">ETF 오버나이트(229200) 매매는 봇이 상태 파일에 남기지 않아 여기에 나오지 않습니다.</p>')
    return bot_block("kiwoom", "키움 단타 봇", "국내 주식 · 전일 +20% 종목 종가 재돌파 · 9:05 까지 매수, 12시 정리 + ETF 오버나이트",
                     live, summary, body), ("실전" if live else "드라이런")


# ------------------------------------------------------------------ 바이낸스
BN_REASON = {"target": "익절", "stop": "손절", "time": "시간 정리", "night": "종가 매매 · 장 시작 정리", "night_stop": "종가 매매 · 손절"}
BN_PHASE = {"new": "준비 중", "watch": "신호 감시 중", "position": "보유 중", "done": "끝"}


def binance_section(links: bool = False) -> tuple[str, str]:
    sdir = ROOT / "state_binance"
    dry = env_flag(ROOT / ".env.binance", "BINANCE_DRY_RUN")
    live = dry is False
    logs = read_lines(sdir / "bot.log")
    waiting = next((l.split(" ", 2)[2] for l in reversed(logs) if "다음 실행" in l), "")
    mode_line = next((l.split(" ", 2)[2] for l in reversed(logs) if "주문 모드" in l), "")

    top, top_day = [], ""
    for line in reversed(logs):
        m = re.search(r"전날\((\d{4}-\d{2}-\d{2})\) 거래대금 상위 \d+: (.+)$", line)
        if m:
            top_day = m.group(1)
            for part in m.group(2).split(", "):
                pm = re.match(r"(\w+?)USDT ([\d,]+)M", part.strip())
                if pm:
                    top.append((pm.group(1), float(pm.group(2).replace(",", ""))))
            break
    peak = max((v for _, v in top), default=1.0)
    bars = "".join(
        f'<li><span class="b-name">{esc(sym)}</span><span class="b-track"><span class="b-fill" style="width:{v / peak * 100:.1f}%"></span></span>'
        f'<span class="b-val">{v:,.0f}M</span></li>' for sym, v in top)

    days = sorted(sdir.glob("day_*.json"))
    latest = read_json(days[-1]) if days else None
    open_rows = []
    if latest:
        for sym, p in (latest.get("positions") or {}).items():
            if p.get("result"):
                continue
            side = "롱" if p.get("side", 1) > 0 else "숏"
            open_rows.append(
                f'<tr><th scope="row">{esc(sym.replace("USDT", ""))}</th><td>{side}</td><td class="num">{esc(p.get("qty", "-"))}</td>'
                f'<td class="num">{esc(p.get("entry", "-"))}</td><td class="num">{esc(p.get("stop", "-"))}</td>'
                f'<td class="num">{esc(p.get("tp", "-"))}</td></tr>')

    night = (read_json(sdir / "overnight.json") or {}).get("positions") or {}
    for sym, p in night.items():
        open_rows.append(
            f'<tr><th scope="row">{esc(sym.replace("USDT", ""))}<small>종가 매매 {esc(p.get("day", ""))}</small></th><td>롱</td>'
            f'<td class="num">{esc(p.get("qty", "-"))}</td><td class="num">{esc(p.get("entry", "-"))}</td>'
            f'<td class="num">{esc(p.get("stop", "-"))}</td><td class="num">다음 9:30</td></tr>')

    trade_rows, total, n = [], 0.0, 0
    try:
        with open(sdir / "trades.csv", newline="", encoding="utf-8") as fh:
            trades = list(csv.DictReader(fh))
    except OSError:
        trades = []
    for t in reversed(trades):
        ret, pnl = float(t.get("ret_pct") or 0), float(t.get("pnl") or 0)
        n += 1
        total += pnl
        side = "롱" if float(t.get("side") or 1) > 0 else "숏"
        name = esc(t.get("symbol", "").replace("USDT", ""))
        if links and re.fullmatch(r"[A-Z0-9]+", t.get("symbol", "")) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", t.get("day", "")):
            kind = "night" if str(t.get("reason", "")).startswith("night") else "day"
            name = f'<a href="/chart/binance/{t["symbol"]}/{t["day"]}/{kind}">{name} <span aria-hidden="true">↗</span></a>'
        trade_rows.append(
            f'<tr><td class="num">{esc(t.get("day", "")[5:])}</td><th scope="row">{name}'
            f'<small>{"드라이런" if t.get("mode") == "dry" else "실전"}</small></th><td>{side}</td>'
            f'<td class="num">{esc(t.get("qty", ""))}</td><td class="num">{esc(t.get("entry", ""))}</td>'
            f'<td class="num">{esc(t.get("exit", ""))}</td><td class="num {tone(ret)}">{signed(ret, "%", 2)}</td>'
            f'<td class="num {tone(pnl)}">{signed(pnl, "", 2)}</td><td>{esc(BN_REASON.get(t.get("reason"), t.get("reason", "")))}</td></tr>')

    phase = BN_PHASE.get((latest or {}).get("phase"), "첫 실행 대기")
    summary = (
        f'<dl class="facts">'
        f'<div><dt>오늘 상태</dt><dd>{esc(phase)}<small>{esc((latest or {}).get("day", "기록 없음"))}</small></dd></div>'
        f'<div><dt>보유 포지션</dt><dd>{len(open_rows)}개<small>낮 2종목 + 종가 2종목</small></dd></div>'
        f'<div><dt>거래</dt><dd>{n}건<small>기록 {len(days)}일</small></dd></div>'
        f'<div><dt>손익 (USDT)</dt><dd class="{tone(total)}">{signed(total, "", 2)}<small>수수료 전</small></dd></div>'
        f'</dl>')
    body = (
        (f'<p class="note">{esc(mode_line)}</p>' if mode_line else "")
        + (f'<p class="note">봇 창: {esc(waiting)}</p>' if waiting else "")
        + (f'<h3>감시 대상 — {esc(top_day)} 거래대금 상위 {len(top)}</h3><ol class="bars">{bars}</ol>'
           f'<p class="note">단위 M = 백만 USDT. 이 중 첫 5분봉 뒤 FVG 신호가 먼저 닿는 2종목까지 진입합니다.</p>' if top else "")
        + f'<h3>보유 포지션</h3><div class="scroll"><table><thead><tr><th>종목</th><th>방향</th><th class="num">수량</th>'
          f'<th class="num">진입가</th><th class="num">손절</th><th class="num">익절</th></tr></thead>'
          f'<tbody>{"".join(open_rows) or empty_row(6, "보유 포지션 없음")}</tbody></table></div>'
        + f'<h3>거래 내역</h3><div class="scroll"><table><thead><tr><th class="num">날짜</th><th>종목</th><th>방향</th>'
          f'<th class="num">수량</th><th class="num">진입가</th><th class="num">청산가</th><th class="num">수익률</th>'
          f'<th class="num">손익</th><th>사유</th></tr></thead><tbody>{"".join(trade_rows) or empty_row(9, "아직 거래 없음")}</tbody></table></div>')
    return bot_block("binance", "바이낸스 봇", "미국 주식 선물 · 낮: 첫 5분봉 + 1분봉 FVG 5배 x 2종목 · 밤: 급락 종목 종가 매수 1배 x 2종목",
                     live, summary, body), ("실전" if live else "드라이런")


# ------------------------------------------------------------------ 페이지
ICONS = {"toss": "toss_bot.ico", "kiwoom": "kiwoom_bot.ico", "binance": "binance_bot.ico"}


def bot_block(key: str, name: str, desc: str, live: bool, summary: str, body: str) -> str:
    icon = icon_data_uri(ROOT / "windows" / ICONS[key])
    img = f'<img src="{icon}" alt="" width="44" height="44">' if icon else ""
    pill = '<span class="pill live">실전</span>' if live else '<span class="pill dry">드라이런 · 주문 안 나감</span>'
    return (f'<section class="bot" id="{key}"><header class="bot-head">{img}<div class="bot-title"><h2>{esc(name)}</h2>'
            f'<p>{esc(desc)}</p></div>{pill}</header>{summary}{body}</section>')


CSS = """
/* 레이아웃: 한 줄 머리말 → 봇 3개가 세로로 쌓이는 장부. 봇마다 요약 4칸 → 표. */
:root{
  --bg:#F2F4F7; --surface:#FFFFFF; --ink:#141922; --muted:#5B6575; --line:#DDE2EA; --soft:#EEF1F5;
  --up:#D42F3E; --down:#1D5BD1; --live:#0E7A4F; --live-bg:#DDF3E8; --dry:#8A5A00; --dry-bg:#FBEBC8;
  --font:'IBM Plex Sans KR','Malgun Gothic','Apple SD Gothic Neo',sans-serif;
  --mono:'IBM Plex Mono','Consolas',monospace;
}
@media (prefers-color-scheme: dark){ :root:not([data-theme="light"]){
  --bg:#0E1218; --surface:#171C25; --ink:#E7EBF1; --muted:#97A1B2; --line:#2A3241; --soft:#1F2632;
  --up:#FF6B77; --down:#74A6FF; --live:#5FD6A2; --live-bg:#123526; --dry:#F2C15A; --dry-bg:#3A2C0C; color-scheme:dark } }
:root[data-theme="dark"]{
  --bg:#0E1218; --surface:#171C25; --ink:#E7EBF1; --muted:#97A1B2; --line:#2A3241; --soft:#1F2632;
  --up:#FF6B77; --down:#74A6FF; --live:#5FD6A2; --live-bg:#123526; --dry:#F2C15A; --dry-bg:#3A2C0C; color-scheme:dark }
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font-family:var(--font);font-size:15px;line-height:1.55}
.wrap{max-width:1040px;margin:0 auto;padding-inline:16px;padding-block:28px 56px;display:flex;flex-direction:column;gap:20px}
.top{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:6px 20px}
h1{font-size:26px;font-weight:700;margin:0;letter-spacing:-0.02em}
.stamp{color:var(--muted);font-size:13px;margin:0}
.stamp b{color:var(--ink);font-family:var(--mono);font-weight:500}
.legend{color:var(--muted);font-size:13px;margin:0}
.bot{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px;display:flex;flex-direction:column;gap:14px;min-width:0}
.bot-head{display:flex;flex-wrap:wrap;align-items:center;gap:12px}
.bot-head img{border-radius:10px;flex:none}
.bot-title{flex:1 1 220px;min-width:0}
h2{font-size:19px;font-weight:700;margin:0;letter-spacing:-0.01em}
.bot-title p{margin:0;color:var(--muted);font-size:13px}
.pill{font-size:12.5px;font-weight:600;padding:4px 10px;border-radius:999px;white-space:nowrap}
.pill.live{background:var(--live-bg);color:var(--live)} .pill.dry{background:var(--dry-bg);color:var(--dry)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:10px;overflow:hidden;margin:0}
.facts div{background:var(--surface);padding:12px 14px;min-width:0}
.facts dt{font-size:12.5px;color:var(--muted)}
.facts dd{margin:2px 0 0;font-family:var(--mono);font-size:19px;font-weight:500;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.facts small{display:block;font-family:var(--font);font-size:12px;font-weight:400;color:var(--muted)}
h3{font-size:14px;font-weight:600;margin:6px 0 0}
.note{margin:0;color:var(--muted);font-size:13px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:9px 12px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--line)}
tbody tr:last-child th,tbody tr:last-child td{border-bottom:0}
thead th{background:var(--soft);color:var(--muted);font-size:12.5px;font-weight:500}
tbody th{font-weight:600}
tbody th small{display:block;font-family:var(--mono);font-size:11.5px;font-weight:400;color:var(--muted)}
.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
thead .num{font-family:var(--font)}
.up{color:var(--up)} .down{color:var(--down)} .flat{color:var(--muted)}
.tag{font-size:12px;padding:2px 8px;border-radius:6px;background:var(--soft);color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:18px}
.gcell{min-width:210px}
.gauge{position:relative;height:6px;border-radius:3px;background:var(--soft);margin:7px 6px 5px}
.g-zero{position:absolute;top:-4px;width:2px;height:14px;background:var(--muted);transform:translateX(-1px)}
.g-dot{position:absolute;top:-4px;width:14px;height:14px;border-radius:50%;transform:translateX(-7px);border:2px solid var(--surface)}
.g-dot.up{background:var(--up)} .g-dot.down{background:var(--down)} .g-dot.flat{background:var(--muted)}
.g-ends{display:flex;justify-content:space-between;gap:10px;font-family:var(--mono);font-size:11px;color:var(--muted)}
.bars{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:6px}
.bars li{display:grid;grid-template-columns:52px 1fr 64px;align-items:center;gap:10px;font-family:var(--mono);font-size:13px}
.b-track{height:10px;background:var(--soft);border-radius:5px;overflow:hidden}
.b-fill{display:block;height:100%;background:var(--muted);border-radius:5px}
.b-val{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.foot{color:var(--muted);font-size:12.5px;margin:0}
tbody th a{color:inherit;text-decoration:underline;text-decoration-color:var(--line);text-underline-offset:3px}
tbody th a:hover{text-decoration-color:var(--ink)}
tbody th a:focus-visible{outline:2px solid var(--down);outline-offset:2px}
.bar{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;color:var(--muted);font-size:13px}
.bar button{font:inherit;font-weight:600;color:var(--surface);background:var(--ink);border:0;border-radius:8px;padding:8px 16px;cursor:pointer}
.bar button:hover{opacity:.85} .bar button:focus-visible{outline:2px solid var(--down);outline-offset:2px}
.bar button[disabled]{opacity:.5;cursor:default}
"""


def build(toolbar: str = "", links: bool = False) -> str:
    now = datetime.now().astimezone()
    blocks = [toss_section(links)[0], kiwoom_section(links)[0], binance_section(links)[0]]
    return (
        '<title>봇 매매현황</title>\n'
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+KR:wght@400;500;600;700&display=swap">\n'
        f'<style>{CSS}</style>\n'
        '<div class="wrap">'
        f'<header class="top"><h1>봇 매매현황</h1><p class="stamp">기준 시각 <b>{now.strftime("%Y-%m-%d %H:%M")}</b> (한국)</p></header>'
        + (toolbar or '<p class="legend">이 페이지는 만든 순간의 기록입니다.</p>')
        + '<p class="legend">수익은 <span class="up">빨강</span>, 손실은 <span class="down">파랑</span>으로 표시합니다.</p>'
        + "".join(blocks)
        + '<p class="foot">봇이 PC에 남긴 상태 파일과 로그로 만들었습니다. 증권사 계좌 잔고와는 수수료·세금·체결 시점만큼 다를 수 있습니다.</p>'
        '</div>\n')


def main() -> None:
    ap = argparse.ArgumentParser(description="세 봇 매매현황 페이지 만들기")
    ap.add_argument("--out", default=str(ROOT / "state" / "매매현황_페이지.html"))
    ap.add_argument("--local", action="store_true", help="PC 브라우저에서 바로 여는 완전한 HTML 문서로 저장")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    page = build()
    if args.local:  # 아티팩트로 올릴 때는 게시 쪽에서 문서 틀을 씌우므로 본문만 저장
        page = ('<!doctype html>\n<html lang="ko"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<style>body{margin:0}</style></head><body>\n' + page + '</body></html>\n')
    out.write_text(page, encoding="utf-8")
    print(f"만들었습니다: {out}")


if __name__ == "__main__":
    main()
