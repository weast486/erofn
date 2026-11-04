"""OKX FVG 드라이런 결과를 reports/okx_dryrun.md 로 다시 만든다 (state_okx/day_*.json 기준, 주문·설정은 안 건드림).

    python scripts/okx_dryrun_report.py [--state state_okx] [--start 2026-10-28] [--equity 100]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

HEAD = """# OKX FVG 봇 드라이런 시범 운용 ({eq:g}달러)

- 시작: {start} (미국 동부 날짜). 규칙은 바이낸스 FVG 봇과 같음 (전날 거래대금 상위 10, 첫 5분봉 + 1분봉 FVG,
  하루 최대 2종목, 종목당 평가금액 x 5배, 2R 익절, 15:20 정리)
- 평가금액 {eq:g} USDT 고정 (드라이런은 손익을 다음 거래 금액에 더하지 않음 → 매 거래 약 {notional:g}달러어치)
- 체결 가정: 진입가·손절가·익절가 그대로, 시간 정리는 그때 가격. 손익에 수수료·미끄러짐 없음
  (아래 요약에 OKX 수수료 추정 0.05% x 왕복을 따로 뺌)
- 원본: `state_okx\\day_날짜.json`, `state_okx\\trades.csv`, `state_okx\\bot.log`
- 다시 만들기: `python scripts/okx_dryrun_report.py`

## 날마다

| 날짜(동부) | 대상 상위 10 | FVG 신호 | 거래 | 결과 | 그날 손익(USDT) | 누적(USDT) |
|---|---|---|---|---|---|---|
"""
REASON = {"target": "익절", "stop": "손절", "time": "시간 정리", "manual": "수동"}


def side(x: int) -> str:
    return "롱" if x == 1 else "숏"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state_okx")
    ap.add_argument("--start", default="2026-10-28")
    ap.add_argument("--equity", type=float, default=100.0)
    ap.add_argument("--leverage", type=float, default=5.0)
    ap.add_argument("--fee", type=float, default=0.05, help="한쪽 수수료 %%")
    ap.add_argument("--out", default="reports/okx_dryrun.md")
    a = ap.parse_args()
    rows, cum, trades, wins, fees, stops = [], 0.0, 0, 0, 0.0, []
    for p in sorted(Path(a.state).glob("day_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        if d["day"] < a.start:
            continue
        cands = ", ".join(s.replace("USDT", "") for s in d.get("candidates", []))
        sigs = ", ".join(f"{s.replace('USDT', '')} {side(v[0])} {v[2]}" for s, v in d.get("signals", {}).items()) or "0개"
        done = [x for x in d.get("positions", {}).values() if x.get("result")]
        if done:
            deal = "<br>".join(f"{x['symbol'].replace('USDT', '')} {side(x['side'])} {x['entry']:g}→{x['result']['exit']:g}"
                               for x in done)
            res = "<br>".join(f"{REASON.get(x['result']['reason'], x['result']['reason'])} {x['result']['ret_pct']:+.2f}%"
                              for x in done)
        else:
            deal = "없음" if d.get("phase") == "done" else f"진행 중·미완료 ({d.get('phase')})"
            res = ("제외: " + ", ".join(s.replace("USDT", "") for s in d.get("dropped", []))) if d.get("dropped") else "신호 없음"
        pnl = sum(x["result"]["pnl"] for x in done)
        cum += pnl
        for x in done:
            trades += 1
            wins += x["result"]["pnl"] > 0
            fees += x["entry"] * x["qty"] * a.fee / 100 * 2
            stops.append(abs(x["entry"] - x["stop"]) / x["entry"] * 100)
        rows.append(f"| {d['day']} | {cands} | {sigs} | {deal} | {res} | {pnl:+.2f} | {cum:+.2f} |")
    out = HEAD.format(eq=a.equity, start=a.start, notional=a.equity * a.leverage) + "\n".join(rows) + "\n"
    out += "\n## 요약\n\n"
    if rows:
        out += (f"- 운용 거래일 {len(rows)}일, 거래 {trades}번 (이익 {wins} / 손실 {trades - wins})\n"
                f"- 누적 손익 {cum:+.2f} USDT ({cum / a.equity * 100:+.1f}%), 수수료 추정 {fees:.2f} USDT 빼면 "
                f"{cum - fees:+.2f} USDT ({(cum - fees) / a.equity * 100:+.1f}%)\n")
        if stops:
            out += f"- 손절 거리 평균 {sum(stops) / len(stops):.2f}% (5배면 손절 한 번에 평가금액의 약 {sum(stops) / len(stops) * a.leverage:.1f}%)\n"
    else:
        out += "- 아직 끝난 거래일 없음\n"
    Path(a.out).write_text(out, encoding="utf-8")
    print(f"{a.out}: {len(rows)}일, 거래 {trades}번, 누적 {cum:+.2f} USDT")


if __name__ == "__main__":
    main()
