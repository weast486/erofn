"""신고가 돌파 전략 매매내역 리포트 생성 (엑셀 + CSV).

    python scripts/make_breakout_report.py [--cache data/ohlcv] [--out reports/breakout_20d_sl47_tp20]

전략 설정을 바꿔 다시 만들려면 아래 STRATEGY 값을 수정한다.
엑셀의 요약 시트들은 '거래내역' 시트를 참조하는 수식이라, 거래내역을 고치면 자동으로 다시 계산된다.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openpyxl import Workbook  # noqa: E402
from openpyxl.comments import Comment  # noqa: E402
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

import tossbot.backtest as bt  # noqa: E402

YEARS = (2024, 2025, 2026)
LAST_DAY = date(2026, 9, 23)
STRATEGY = dict(
    entry_days=20, first_in_days=20, min_day_amount=2e10,
    stop_loss_pct=4.7, take_profit_pct=20.0, exit_on_low=False, rank_by="amount",
)
REASON_KO = {"TAKE_PROFIT": "익절", "STOP_LOSS": "손절", "": "보유중", None: "보유중"}
WEEKDAY_KO = "월화수목금토일"

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
SUB_FILL = PatternFill("solid", fgColor="D9E1F2")
INPUT_FONT = Font(name=FONT, color="0000FF")  # 백테스트 엔진이 계산한 값 (수식 아님)
THIN = Side(style="thin", color="BFBFBF")


# ---------------------------------------------------------------- data
def bucket(x: float, edges: list[float], fmt) -> str:
    for i, e in enumerate(edges):
        if x < e:
            lo = edges[i - 1] if i else None
            return f"{i + 1}) " + (f"{fmt(lo)} ~ {fmt(e)}" if lo is not None else f"{fmt(e)} 미만")
    return f"{len(edges) + 1}) {fmt(edges[-1])} 이상"


def pct(v):
    return f"{v:+.0%}" if v else "0%"


def features(sym, entry_day, series, ix, ixd):
    ser = series[sym]
    i = ser.index[entry_day]
    bar, prev = ser.bars[i], ser.bars[i - 1]
    closes = [b.close for b in ser.bars]
    prev_high = max(closes[i - 20:i])
    avg_amt = sum(b.close * b.volume for b in ser.bars[i - 20:i]) / 20
    avg_vol = sum(b.volume for b in ser.bars[i - 20:i]) / 20
    k = ixd.get(entry_day)
    kospi = {}
    if k is not None and k >= 20:
        kb = ix[k]
        ma20 = sum(b.close for b in ix[k - 19:k + 1]) / 20
        kospi = dict(k_chg=kb.close / ix[k - 1].close - 1, k_5d=kb.close / ix[k - 5].close - 1,
                     k_ma20="위" if kb.close > ma20 else "아래")
    return dict(
        chg=bar.close / prev.close - 1,
        amount=bar.close * bar.volume,
        breakout=bar.close / prev_high - 1,
        avg_amount=avg_amt,
        vol_ratio=bar.volume / avg_vol if avg_vol else 0,
        body=bar.close / bar.open - 1,
        upper_tail=bar.high / bar.close - 1,
        **kospi,
    )


def excursion(ser, entry_day, exit_day, entry_price):
    """보유 기간(매수 다음 날 ~ 매도일/기간 말) 최고·최저 수익률."""
    i = ser.index[entry_day]
    j = ser.index.get(exit_day, len(ser.bars) - 1) if exit_day else len(ser.bars) - 1
    seg = ser.bars[i + 1:j + 1]
    if not seg:
        return 0.0, 0.0
    return max(b.high for b in seg) / entry_price - 1, min(b.low for b in seg) / entry_price - 1


def build_rows(data, ix):
    series = bt._prepare(data)
    ixd = {b.day: i for i, b in enumerate(ix)}
    b = bt.BreakoutSettings(**STRATEGY)
    trades, missed, yearly = [], [], {}
    for y in YEARS:
        start, end = date(y, 1, 1), min(date(y, 12, 31), LAST_DAY)
        res = bt.run_breakout(data, start, end, bt.BacktestSettings(), b)
        yearly[y] = res.summary() | {"end": end}
        taken = {(t.symbol, t.entry_date) for t in res.trades}
        # 자리·현금 제한 없이 → 조건을 만족한 모든 신호의 결과
        allres = bt.run_breakout(data, start, end, bt.BacktestSettings(num_slots=10**6, initial_cash=1e12), b)
        for kind, src in (("taken", res.trades), ("missed", [t for t in allres.trades if (t.symbol, t.entry_date) not in taken])):
            for t in src:
                ser = series[t.symbol]
                f = features(t.symbol, t.entry_date, series, ix, ixd)
                mfe, mae = excursion(ser, t.entry_date, t.exit_date, t.entry_price)
                last_close = ser.bars[ser.index.get(end, len(ser.bars) - 1)].close if not t.exit_date else None
                row = dict(
                    year=y, symbol=t.symbol, name=t.name, entry=t.entry_date, weekday=WEEKDAY_KO[t.entry_date.weekday()],
                    month=t.entry_date.strftime("%Y-%m"), entry_price=round(t.entry_price), qty=t.qty,
                    cost=round(t.entry_price * t.qty), exit=t.exit_date, exit_price=round(t.exit_price) if t.exit_price else None,
                    reason=REASON_KO[t.reason], hold=t.hold_days,
                    pnl=round(t.pnl) if t.exit_date else round((last_close * (1 - 0.0022) - t.entry_price * 1.00015) * t.qty),
                    ret=t.ret if t.exit_date else (last_close / t.entry_price - 1),
                    mfe=mfe, mae=mae, **f,
                )
                row["b_k5"] = "5일 상승" if row.get("k_5d", 0) > 0 else "5일 하락"
                row["b_chg"] = bucket(row["chg"], [0.03, 0.05, 0.10, 0.15, 0.20], pct)
                row["b_amt"] = bucket(row["amount"] / 1e8, [300, 500, 1000, 2000], lambda v: f"{v:,.0f}억")
                row["b_brk"] = bucket(row["breakout"], [0.02, 0.05, 0.10], pct)
                row["b_vol"] = bucket(row["vol_ratio"], [2, 3, 5, 10], lambda v: f"{v:g}배")
                row["b_body"] = "양봉" if row["body"] > 0 else "음봉·보합"
                (trades if kind == "taken" else missed).append(row)
    trades.sort(key=lambda r: (r["entry"], -r["amount"]))
    missed.sort(key=lambda r: (r["entry"], -r["amount"]))
    return trades, missed, yearly


# ---------------------------------------------------------------- excel
COLUMNS = [  # (키, 헤더, 너비, 표시형식)
    ("year", "연도", 7, "0"), ("month", "매수월", 9, "@"), ("entry", "매수일(신호일)", 12, "yyyy-mm-dd"),
    ("weekday", "요일", 5, "@"), ("symbol", "종목코드", 9, "@"), ("name", "종목명", 16, "@"),
    ("entry_price", "매수가", 9, "#,##0"), ("qty", "수량", 6, "#,##0"), ("cost", "매수금액", 10, "#,##0"),
    ("exit", "매도일", 12, "yyyy-mm-dd"), ("exit_price", "매도가", 9, "#,##0"), ("reason", "결과", 7, "@"),
    ("hold", "보유일", 7, "0"), ("pnl", "손익(원)", 10, "#,##0;[Red]-#,##0"), ("ret", "수익률", 8, "0.0%;[Red]-0.0%"),
    ("mfe", "보유중 최고", 9, "0.0%"), ("mae", "보유중 최저", 9, "0.0%;[Red]-0.0%"),
    ("chg", "당일 상승률", 9, "0.0%"), ("amount", "당일 거래대금(억)", 12, '#,##0,,,"억"'),
    ("breakout", "20일 돌파폭", 9, "0.0%"), ("avg_amount", "20일평균 거래대금(억)", 12, '#,##0,,,"억"'),
    ("vol_ratio", "거래량 배수", 9, "0.0"), ("body", "캔들 몸통(종/시)", 10, "0.0%;[Red]-0.0%"),
    ("upper_tail", "윗꼬리(고/종)", 9, "0.0%"),
    ("k_chg", "코스피 당일", 9, "0.0%;[Red]-0.0%"), ("k_5d", "코스피 5일", 9, "0.0%;[Red]-0.0%"),
    ("k_ma20", "코스피 20일선", 9, "@"),
    ("b_k5", "구간:코스피5일", 10, "@"), ("b_chg", "구간:상승률", 12, "@"), ("b_amt", "구간:거래대금", 14, "@"),
    ("b_brk", "구간:돌파폭", 12, "@"), ("b_vol", "구간:거래량배수", 12, "@"), ("b_body", "구간:캔들", 9, "@"),
]
COL = {k: get_column_letter(i + 1) for i, (k, *_rest) in enumerate(COLUMNS)}


def header(ws, row, labels, widths=None):
    for c, text in enumerate(labels, 1):
        cell = ws.cell(row=row, column=c, value=text)
        cell.font = Font(name=FONT, bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        if widths:
            ws.column_dimensions[get_column_letter(c)].width = widths[c - 1]


def write_trades(ws, rows, note):
    header(ws, 1, [h for _, h, _, _ in COLUMNS], [w for _, _, w, _ in COLUMNS])
    ws.row_dimensions[1].height = 30
    for r, row in enumerate(rows, 2):
        for c, (key, _, _, fmt) in enumerate(COLUMNS, 1):
            v = row.get(key)
            cell = ws.cell(row=r, column=c, value=v)
            cell.number_format = fmt
            cell.font = Font(name=FONT)
    ws.freeze_panes = "G2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(rows) + 1}"
    ws.cell(row=1, column=1).comment = Comment(note, "tossbot")
    return len(rows) + 1


def rng(key, last):
    return f"거래내역!${COL[key]}$2:${COL[key]}${last}"


def write_yearly(ws, last, yearly):
    ws["A1"] = "연도별 요약"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["A2"] = "검은 글씨 = 거래내역 시트를 참조하는 수식 / 파란 글씨 = 백테스트 엔진 계산값 (연말 미청산 평가 포함)"
    ws["A2"].font = Font(name=FONT, italic=True, color="808080")
    labels = ["연도", "매수", "익절", "손절", "보유중", "승률", "실현손익(원)", "평균 수익률(청산)",
              "평균 보유일", "평균 최고수익률", "수익률(엔진)", "MDD(엔진)", "연말 자산(엔진)"]
    header(ws, 4, labels, [8, 8, 8, 8, 8, 8, 13, 12, 10, 12, 11, 10, 14])
    r0 = 5
    for n, y in enumerate(YEARS):
        r = r0 + n
        yr, rs = rng("year", last), rng("reason", last)
        ws[f"A{r}"] = str(y)
        ws[f"B{r}"] = f"=COUNTIFS({yr},{y})"
        ws[f"C{r}"] = f'=COUNTIFS({yr},{y},{rs},"익절")'
        ws[f"D{r}"] = f'=COUNTIFS({yr},{y},{rs},"손절")'
        ws[f"E{r}"] = f'=COUNTIFS({yr},{y},{rs},"보유중")'
        ws[f"F{r}"] = f"=IFERROR(C{r}/(C{r}+D{r}),0)"
        ws[f"G{r}"] = f'=SUMIFS({rng("pnl", last)},{yr},{y},{rs},"<>보유중")'
        ws[f"H{r}"] = f'=IFERROR(AVERAGEIFS({rng("ret", last)},{yr},{y},{rs},"<>보유중"),0)'
        ws[f"I{r}"] = f'=IFERROR(AVERAGEIFS({rng("hold", last)},{yr},{y},{rs},"<>보유중"),0)'
        ws[f"J{r}"] = f'=IFERROR(AVERAGEIFS({rng("mfe", last)},{yr},{y}),0)'
        s = yearly[y]
        for col, v in (("K", s["수익률"]), ("L", s["MDD"]), ("M", s["최종자산"])):
            ws[f"{col}{r}"] = round(v, 6) if col != "M" else round(v)
            ws[f"{col}{r}"].font = INPUT_FONT
    rt = r0 + len(YEARS)
    ws[f"A{rt}"] = "합계"
    for col in "BCDEG":
        ws[f"{col}{rt}"] = f"=SUM({col}{r0}:{col}{rt - 1})"
    ws[f"F{rt}"] = f"=IFERROR(C{rt}/(C{rt}+D{rt}),0)"
    rs = rng("reason", last)
    ws[f"H{rt}"] = f'=IFERROR(AVERAGEIFS({rng("ret", last)},{rs},"<>보유중"),0)'
    ws[f"I{rt}"] = f'=IFERROR(AVERAGEIFS({rng("hold", last)},{rs},"<>보유중"),0)'
    ws[f"J{rt}"] = f'=IFERROR(AVERAGE({rng("mfe", last)}),0)'
    ws[f"K{rt}"] = f"=SUM(K{r0}:K{rt - 1})"
    ws[f"K{rt}"].comment = Comment("연도별 수익률의 단순 합 (연도마다 100만원으로 새로 시작)", "tossbot")
    for r in range(r0, rt + 1):
        for col, fmt in zip("BCDEFGHIJKLM", ["0", "0", "0", "0", "0%", "#,##0;[Red]-#,##0", "0.0%;[Red]-0.0%",
                                            "0.0", "0.0%", "0.0%;[Red]-0.0%", "0.0%;[Red]-0.0%", "#,##0"]):
            ws[f"{col}{r}"].number_format = fmt
            if ws[f"{col}{r}"].font.color is None or ws[f"{col}{r}"].font.color.rgb != "000000FF":
                ws[f"{col}{r}"].font = Font(name=FONT, bold=(r == rt))
        ws[f"A{r}"].font = Font(name=FONT, bold=True)
    for col in "KLM":
        for r in range(r0, rt):
            ws[f"{col}{r}"].font = INPUT_FONT
    ws[f"A{rt + 2}"] = ("승률 = 익절 / (익절 + 손절). 손익·수익률은 수수료 0.015%, 매도세(2024 0.18%·2025 0.15%·2026 0.20% 가정), "
                        "슬리피지 0.1% 반영. 보유중 종목은 기간 말 종가 기준 평가손익.")
    ws[f"A{rt + 2}"].font = Font(name=FONT, italic=True, color="808080")
    ws.freeze_panes = "B5"


def write_monthly(ws, last, months):
    ws["A1"] = "월별 성과 (청산된 거래 기준, 매수월로 집계)"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    header(ws, 3, ["매수월", "매수", "익절", "손절", "보유중", "승률", "실현손익(원)", "평균 수익률", "누적 손익(원)"],
           [10, 7, 7, 7, 8, 8, 13, 11, 13])
    mo, rs = rng("month", last), rng("reason", last)
    for n, m in enumerate(months):
        r = 4 + n
        ws[f"A{r}"] = m
        ws[f"B{r}"] = f'=COUNTIFS({mo},A{r})'
        ws[f"C{r}"] = f'=COUNTIFS({mo},A{r},{rs},"익절")'
        ws[f"D{r}"] = f'=COUNTIFS({mo},A{r},{rs},"손절")'
        ws[f"E{r}"] = f'=COUNTIFS({mo},A{r},{rs},"보유중")'
        ws[f"F{r}"] = f"=IFERROR(C{r}/(C{r}+D{r}),0)"
        ws[f"G{r}"] = f'=SUMIFS({rng("pnl", last)},{mo},A{r},{rs},"<>보유중")'
        ws[f"H{r}"] = f'=IFERROR(AVERAGEIFS({rng("ret", last)},{mo},A{r},{rs},"<>보유중"),0)'
        ws[f"I{r}"] = f"=G{r}" if n == 0 else f"=I{r - 1}+G{r}"
        for col, fmt in zip("BCDEFGHI", ["0", "0", "0", "0", "0%", "#,##0;[Red]-#,##0", "0.0%;[Red]-0.0%", "#,##0;[Red]-#,##0"]):
            ws[f"{col}{r}"].number_format = fmt
            ws[f"{col}{r}"].font = Font(name=FONT)
        ws[f"A{r}"].font = Font(name=FONT, bold=True)
    ws.freeze_panes = "B4"


GROUPS = [
    ("b_k5", "매수일 코스피 5일 흐름 (종가가 5거래일 전보다 높으면 상승)"),
    ("k_ma20", "매수일 코스피 20일선 위치"),
    ("b_chg", "매수일 당일 상승률"),
    ("b_amt", "매수일 당일 거래대금"),
    ("b_brk", "20일 신고가 돌파폭 (종가 / 직전 20일 최고 종가)"),
    ("b_vol", "거래량 배수 (당일 / 직전 20일 평균)"),
    ("b_body", "매수일 캔들 (종가 vs 시가)"),
    ("weekday", "매수 요일"),
]


def write_groups(ws, last, rows):
    ws["A1"] = "조건별 분석 (청산된 거래 기준) — 전략 수정 아이디어 검토용"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["A2"] = "연도별 손익이 같은 방향인지 보면 우연인지 판단하는 데 도움이 됩니다. 손익분기 승률은 약 21% (익절 +20% / 손절 -4.7%, 비용 포함)."
    ws["A2"].font = Font(name=FONT, italic=True, color="808080")
    widths = [36, 7, 7, 7, 8, 13, 11] + [12] * len(YEARS)
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    r = 4
    rs, yr = rng("reason", last), rng("year", last)
    for key, title in GROUPS:
        ws.cell(row=r, column=1, value=title).font = Font(name=FONT, bold=True, size=11)
        ws.cell(row=r, column=1).fill = SUB_FILL
        r += 1
        header(ws, r, ["구분", "건수", "익절", "손절", "승률", "손익합(원)", "평균 수익률"] + [f"{y} 손익" for y in YEARS])
        r += 1
        values = sorted({row[key] for row in rows if row.get(key) is not None}, key=lambda v: (str(v)[0].isdigit() is False, str(v)))
        if key == "weekday":
            values = [d for d in "월화수목금" if d in values]
        g = rng(key, last)
        for v in values:
            ws.cell(row=r, column=1, value=v)
            ws[f"B{r}"] = f'=COUNTIFS({g},A{r},{rs},"<>보유중")'
            ws[f"C{r}"] = f'=COUNTIFS({g},A{r},{rs},"익절")'
            ws[f"D{r}"] = f'=COUNTIFS({g},A{r},{rs},"손절")'
            ws[f"E{r}"] = f"=IFERROR(C{r}/(C{r}+D{r}),0)"
            ws[f"F{r}"] = f'=SUMIFS({rng("pnl", last)},{g},A{r},{rs},"<>보유중")'
            ws[f"G{r}"] = f'=IFERROR(AVERAGEIFS({rng("ret", last)},{g},A{r},{rs},"<>보유중"),0)'
            for n, y in enumerate(YEARS):
                col = get_column_letter(8 + n)
                ws[f"{col}{r}"] = f'=SUMIFS({rng("pnl", last)},{g},$A{r},{rs},"<>보유중",{yr},{y})'
                ws[f"{col}{r}"].number_format = "#,##0;[Red]-#,##0"
                ws[f"{col}{r}"].font = Font(name=FONT)
            for col, fmt in zip("BCDEFG", ["0", "0", "0", "0%", "#,##0;[Red]-#,##0", "0.0%;[Red]-0.0%"]):
                ws[f"{col}{r}"].number_format = fmt
                ws[f"{col}{r}"].font = Font(name=FONT)
            ws[f"A{r}"].font = Font(name=FONT)
            r += 1
        r += 1


def write_guide(ws, yearly, n_trades, n_missed):
    lines = [
        ("토스증권 자동매매 — 신고가 돌파 전략 백테스트 매매내역", "title"),
        (f"생성일: {date.today().isoformat()} / 기간: 2024-01-01 ~ {LAST_DAY.isoformat()} / 생성 스크립트: scripts/make_breakout_report.py", "note"),
        ("", None),
        ("전략 규칙", "h"),
        ("매수: 종가 기준 20일 신고가 (종가 > 직전 20거래일 최고 종가)", None),
        ("  + 한 달 내 첫 신고가 (직전 20거래일 동안 20일 신고가가 없었음)", None),
        ("  + 당일 거래대금 200억원 이상, 직전 20일 평균 거래대금 30억원 이상", None),
        ("  + 종가 10만원 이하, 상한가(+29.5% 이상) 마감 제외, 코스피·코스닥 보통주", None),
        ("  → 신고가 날 종가에 매수 (실전: 15:10~15:20). 신호가 많으면 당일 거래대금 큰 순서, 최대 10종목 x 10만원", None),
        ("매도: 손절 -4.7% (장중 저가가 닿으면 손절가, 갭하락이면 시가) / 익절 +20% (고가가 닿으면 익절가, 갭상승이면 시가)", None),
        ("  같은 날 손절가·익절가 모두 닿으면 손절로 가정 (보수적). 보유기간 제한·시장 필터 없음", None),
        ("", None),
        ("가정", "h"),
        ("데이터: FinanceData/marcap (KRX 전 종목 일별, 상장폐지 포함), 무상증자·분할·병합은 KRX 기준가로 수정주가 변환", None),
        ("비용: 수수료 0.015% (매수·매도), 매도 시 증권거래세 2024 0.18% / 2025 0.15% / 2026 0.20% (가정), 슬리피지 0.1% (매수·손절)", None),
        ("연도마다 100만원으로 새로 시작. 연말(2026은 9/23) 미청산 종목은 기간 말 종가로 평가", None),
        ("코스피 지수: 네이버 금융 일봉", None),
        ("", None),
        ("시트 안내", "h"),
        ("연도별요약: 연도별 매수·익절·손절·승률·손익 (거래내역 참조 수식) + 엔진 계산 수익률·MDD", None),
        ("월별: 매수월별 성과와 누적 손익", None),
        ("조건별분석: 코스피 흐름, 상승률, 거래대금, 돌파폭, 거래량, 캔들, 요일별 성과 (전략 수정 아이디어 검토용)", None),
        (f"거래내역: 실제 매매 {n_trades}건 (매수일 지표, 코스피 상태, 보유 중 최고·최저 수익률 포함). 필터를 걸어 쓰세요", None),
        (f"놓친신호: 조건은 만족했지만 10자리가 차 있어 사지 못한 신호 {n_missed}건과, 샀다면 어땠을지 (자리 제한 없이 계산)", None),
        ("", None),
        ("열 설명 (거래내역·놓친신호)", "h"),
        ("보유중 최고/최저: 매수 다음 날부터 매도일까지 고가·저가 기준 최대 수익률 / 최대 손실률 (익절이 가까웠는지, 손절이 아슬했는지 확인)", None),
        ("20일 돌파폭: 매수일 종가 / 직전 20일 최고 종가 - 1   ·   거래량 배수: 당일 거래량 / 직전 20일 평균", None),
        ("캔들 몸통: 종가/시가 - 1 (양수 = 양봉)   ·   윗꼬리: 고가/종가 - 1", None),
        ("코스피 5일: 코스피 종가 / 5거래일 전 종가 - 1   ·   구간: 조건별분석 시트의 묶음 기준", None),
        ("", None),
        ("주의: 과거 백테스트 결과이며 미래 수익을 보장하지 않습니다. 일봉 기준이라 장중 체결 순서는 가정입니다.", "note"),
    ]
    ws.column_dimensions["A"].width = 130
    for r, (text, kind) in enumerate(lines, 1):
        cell = ws.cell(row=r, column=1, value=text)
        if kind == "title":
            cell.font = Font(name=FONT, bold=True, size=16)
        elif kind == "h":
            cell.font = Font(name=FONT, bold=True, size=12, color="1F3864")
        elif kind == "note":
            cell.font = Font(name=FONT, italic=True, color="808080")
        else:
            cell.font = Font(name=FONT)


def write_csv(path: Path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([h for _, h, _, _ in COLUMNS])
        for row in rows:
            w.writerow([row.get(k) if not isinstance(row.get(k), float) else round(row.get(k), 6) for k, *_ in COLUMNS])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", type=Path, default=Path("data/ohlcv"))
    ap.add_argument("--out", type=Path, default=Path("reports/breakout_20d_sl47_tp20"))
    args = ap.parse_args()
    data = bt.load_cache(args.cache)
    ix = bt.load_index(args.cache, "KOSPI")
    trades, missed, yearly = build_rows(data, ix)

    args.out.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws_guide = wb.active
    ws_guide.title = "안내"
    ws_year, ws_month, ws_group = wb.create_sheet("연도별요약"), wb.create_sheet("월별"), wb.create_sheet("조건별분석")
    ws_trades, ws_missed = wb.create_sheet("거래내역"), wb.create_sheet("놓친신호")
    last = write_trades(ws_trades, trades, "실제 매매 (10자리·현금 제한 적용). 요약 시트들이 이 시트를 참조합니다.")
    write_trades(ws_missed, missed, "조건은 만족했지만 자리가 없어 사지 못한 신호. 자리 제한 없이 샀다고 가정한 결과입니다.")
    write_guide(ws_guide, yearly, len(trades), len(missed))
    write_yearly(ws_year, last, yearly)
    write_monthly(ws_month, last, sorted({r["month"] for r in trades}))
    write_groups(ws_group, last, trades)
    xlsx = args.out / "매매내역_신고가돌파.xlsx"
    wb.save(xlsx)
    write_csv(args.out / "거래내역.csv", trades)
    write_csv(args.out / "놓친신호.csv", missed)
    print(xlsx, len(trades), len(missed))


if __name__ == "__main__":
    main()
