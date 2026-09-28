"""눌림목 전략 백테스트 (일봉 기반).

실전 봇과 같은 판단 함수(selector.analyze / touch_zone, 호가 반올림)를 그대로 사용한다.

    # 1) 과거 일봉 내려받기 (최초 1회, 캐시에 저장)
    python -m tossbot.backtest download --source pykrx --start 2023-09-01
    # 2) 연도별 백테스트 (.env 의 전략 설정을 그대로 사용)
    python -m tossbot.backtest run --years 2024 2025 2026

일봉만으로 장중 체결을 재현하기 위한 가정
  매수 (7일선 터치 구간 [lo, hi], selector.touch_zone)
    - 시가가 구간 안이면 시가에 매수
    - 시가가 구간 위이고 저가가 hi 이하면 hi 에 매수 (위에서 내려와 터치)
    - 시가가 구간 아래(갭하락 이탈)이고 고가가 lo 이상이면 lo 에 매수 (다시 올라와 구간 진입)
    - 같은 날 신호가 여럿이면 급등폭 큰 순서로 빈 자리만큼
  매도
    - 시가가 손절가 이하 → 시가에 손절 (갭하락), 시가가 익절가 이상 → 시가에 익절
    - 장중 저가가 손절가 이하 → 손절가 / 고가가 익절가 이상 → 익절가
      (같은 날 둘 다 닿으면 손절로 가정: 보수적)
    - 매수 당일에도 저가가 손절가 이하면 손절로 가정 (구간 아래에서 올라와 산 경우는 제외)
    - 보유 MAX_HOLD_DAYS 거래일째 종가에 매도 (실전은 15:10)
  비용: 수수료(매수·매도), 매도 시 증권거래세(연도별), 슬리피지(시장가 체결 양쪽)
"""
from __future__ import annotations

import argparse
import bisect
import csv
import logging
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .broker import round_down_to_tick, round_up_to_tick
from .config import Config, load_dotenv
from .selector import Bar, PullbackParams, analyze, touch_zone
from .strategy import BUY_LIMIT_SLIPPAGE, params_from_config

log = logging.getLogger("tossbot.backtest")

# 매도 시 증권거래세(농특세 포함, 코스피·코스닥 동일하게 근사). 연도별 세율은 가정이므로 필요 시 수정
SELL_TAX_BY_YEAR = {2023: 0.0020, 2024: 0.0018, 2025: 0.0015, 2026: 0.0020}
DEFAULT_CACHE = Path("data/ohlcv")


# ---------------------------------------------------------------- settings
@dataclass
class BacktestSettings:
    params: PullbackParams = field(default_factory=PullbackParams)
    initial_cash: float = 1_000_000
    num_slots: int = 10
    stop_loss_pct: float = 4.7
    take_profit_pct: float = 15.0
    max_hold_days: int = 10
    cooldown_days: int = 5
    commission: float = 0.00015
    slippage: float = 0.001  # 시장가 체결 시 불리한 방향 슬리피지

    @classmethod
    def from_config(cls, cfg: Config) -> "BacktestSettings":
        return cls(
            params=params_from_config(cfg),
            initial_cash=cfg.total_budget,
            num_slots=cfg.num_stocks,
            stop_loss_pct=cfg.stop_loss_pct,
            take_profit_pct=cfg.take_profit_pct,
            max_hold_days=cfg.max_hold_days,
            cooldown_days=cfg.rebuy_cooldown_days,
        )


@dataclass
class Trade:
    symbol: str
    name: str
    entry_date: date
    entry_price: float
    qty: int
    surge_date: date
    exit_date: date | None = None
    exit_price: float | None = None
    reason: str = ""
    hold_days: int = 0
    pnl: float = 0.0  # 비용 포함 손익 (원)

    @property
    def ret(self) -> float:
        cost = self.entry_price * self.qty
        return self.pnl / cost if cost else 0.0


@dataclass
class Result:
    start: date
    end: date
    initial_cash: float
    final_equity: float
    trades: list[Trade]
    equity_curve: list[tuple[date, float]]
    open_positions: int

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_cash - 1

    @property
    def max_drawdown(self) -> float:
        peak, mdd = -math.inf, 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1)
        return mdd

    def summary(self) -> dict:
        closed = [t for t in self.trades if t.exit_date]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_win, gross_loss = sum(t.pnl for t in wins), -sum(t.pnl for t in losses)
        return {
            "기간": f"{self.start} ~ {self.end}",
            "수익률": self.total_return,
            "최종자산": self.final_equity,
            "MDD": self.max_drawdown,
            "거래수": len(closed),
            "승률": len(wins) / len(closed) if closed else 0.0,
            "평균수익": sum(t.ret for t in wins) / len(wins) if wins else 0.0,
            "평균손실": sum(t.ret for t in losses) / len(losses) if losses else 0.0,
            "손익비(PF)": gross_win / gross_loss if gross_loss else math.inf,
            "평균보유일": sum(t.hold_days for t in closed) / len(closed) if closed else 0.0,
            "청산사유": dict(Counter(t.reason for t in closed)),
            "미청산": self.open_positions,
        }


# ------------------------------------------------------------------ engine
@dataclass
class _Series:
    name: str
    bars: list[Bar]
    days: list[date]
    index: dict[date, int]


def _prepare(data: dict[str, tuple[str, list[Bar]]]) -> dict[str, _Series]:
    out = {}
    for sym, (name, bars) in data.items():
        bars = sorted((b for b in bars if b.close > 0 and b.open > 0), key=lambda b: b.day)
        if len(bars) < 30:
            continue
        days = [b.day for b in bars]
        out[sym] = _Series(name, bars, days, {d: i for i, d in enumerate(days)})
    return out


def _sell_value(qty: int, price: float, year: int, s: BacktestSettings) -> float:
    tax = SELL_TAX_BY_YEAR.get(year, 0.0020)
    return qty * price * (1 - s.commission - tax)


def run_backtest(
    data: dict[str, tuple[str, list[Bar]]], start: date, end: date, s: BacktestSettings | None = None
) -> Result:
    s = s or BacktestSettings()
    p = s.params
    series = _prepare(data)
    calendar = sorted({d for ser in series.values() for d in ser.days if start <= d <= end})

    # 날짜별 후보: 최근 surge_lookback_days 거래일 안에 급등일이 있는 종목 (빠른 1차 필터)
    candidates: dict[date, set[str]] = defaultdict(set)
    for sym, ser in series.items():
        closes = [b.close for b in ser.bars]
        for i in range(1, len(closes)):
            if closes[i] / closes[i - 1] - 1 >= p.surge_pct / 100:
                for j in range(i + 1, min(i + 1 + p.surge_lookback_days, len(closes))):
                    candidates[ser.days[j]].add(sym)

    cash = s.initial_cash
    positions: dict[str, Trade] = {}
    cooldown_until: dict[str, date] = {}
    trades: list[Trade] = []
    equity_curve: list[tuple[date, float]] = []
    last_close: dict[str, float] = {}

    def close_position(t: Trade, day: date, price: float, reason: str) -> None:
        nonlocal cash
        proceeds = _sell_value(t.qty, price, day.year, s)
        t.exit_date, t.exit_price, t.reason = day, price, reason
        t.pnl = proceeds - t.qty * t.entry_price * (1 + s.commission)
        cash += proceeds
        del positions[t.symbol]
        cooldown_until[t.symbol] = day + timedelta(days=s.cooldown_days)

    for day in calendar:
        # 1) 보유 종목 청산 판단
        for sym, t in list(positions.items()):
            ser = series[sym]
            i = ser.index.get(day)
            t.hold_days += 1
            if i is None:  # 거래정지 등
                continue
            b = ser.bars[i]
            stop = round_down_to_tick(t.entry_price * (1 - s.stop_loss_pct / 100))
            tp = round_up_to_tick(t.entry_price * (1 + s.take_profit_pct / 100))
            if b.open <= stop:
                close_position(t, day, b.open * (1 - s.slippage), "STOP_LOSS")
            elif b.open >= tp:
                close_position(t, day, b.open, "TAKE_PROFIT")
            elif b.low <= stop:
                close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")
            elif b.high >= tp:
                close_position(t, day, tp, "TAKE_PROFIT")
            elif s.max_hold_days and t.hold_days >= s.max_hold_days:
                close_position(t, day, b.close * (1 - s.slippage), "TIME_EXIT")

        # 2) 신규 매수
        slots = s.num_slots - len(positions)
        if slots > 0:
            signals = []
            for sym in candidates.get(day, ()):
                if sym in positions or cooldown_until.get(sym, date.min) > day:
                    continue
                ser = series[sym]
                i = ser.index[day]
                item, _ = analyze(sym, ser.name, ser.bars[max(0, i - 80):i], day, p)
                if item is None:
                    continue
                lo, hi = touch_zone(item, p)
                b = ser.bars[i]
                if lo <= b.open <= hi:
                    fill, kind = b.open, "open"
                elif b.open > hi and b.low <= hi:
                    fill, kind = hi, "touch"
                elif b.open < lo and b.high >= lo:
                    fill, kind = lo, "rebound"
                else:
                    continue
                if fill <= item.pre_surge_close or fill > p.slot_budget:
                    continue
                signals.append((item.surge_pct, sym, item, fill, kind, b))
            signals.sort(key=lambda x: x[0], reverse=True)
            for _, sym, item, fill, kind, b in signals[:slots]:
                price = fill * (1 + s.slippage)
                qty = int(p.slot_budget // round_up_to_tick(fill * (1 + BUY_LIMIT_SLIPPAGE)))
                cost = qty * price * (1 + s.commission)
                if qty <= 0 or cost > cash:
                    continue
                cash -= cost
                t = Trade(sym, item.name, day, price, qty, item.surge_date)
                positions[sym] = t
                trades.append(t)
                stop = round_down_to_tick(price * (1 - s.stop_loss_pct / 100))
                if kind != "rebound" and b.low <= stop:
                    close_position(t, day, stop * (1 - s.slippage), "STOP_LOSS")

        # 3) 평가
        for sym in positions:
            i = series[sym].index.get(day)
            if i is not None:
                last_close[sym] = series[sym].bars[i].close
        equity = cash + sum(t.qty * last_close.get(sym, t.entry_price) for sym, t in positions.items())
        equity_curve.append((day, equity))

    final = equity_curve[-1][1] if equity_curve else s.initial_cash
    return Result(start, end, s.initial_cash, final, trades, equity_curve, len(positions))


# -------------------------------------------------------------------- data
def load_cache(cache_dir: Path) -> dict[str, tuple[str, list[Bar]]]:
    names = {}
    names_file = cache_dir / "_names.csv"
    if names_file.exists():
        with open(names_file, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                names[row["code"]] = row["name"]
    data = {}
    for path in cache_dir.glob("*.csv"):
        if path.name.startswith("_"):
            continue
        with open(path, encoding="utf-8") as f:
            bars = [
                Bar(date.fromisoformat(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"]), float(r["volume"]))
                for r in csv.DictReader(f)
            ]
        data[path.stem] = (names.get(path.stem, path.stem), bars)
    return data


def _write_bars(path: Path, rows) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerows(rows)


def _is_common_stock(code: str, name: str) -> bool:
    # 보통주 코드는 6자리이고 끝자리가 0 (2024년부터 신규 상장은 영문 포함 코드도 있음)
    return (
        len(code) == 6 and code.isalnum() and code.endswith("0")
        and "스팩" not in name and "리츠" not in name
    )


def adjust_splits(rows: list[tuple]) -> list[tuple]:
    """무상증자·액면분할·병합 수정: KRX 기준가(종가 - 전일대비)가 실제 전일 종가와 다르면
    그 비율만큼 이전 봉들의 가격(과 거래량)을 조정한다.

    rows: (date, open, high, low, close, volume, base) 를 날짜순으로.
    반환: (date, open, high, low, close, volume) 수정주가.
    """
    factors = [1.0] * len(rows)
    k = 1.0
    for i in range(len(rows) - 1, 0, -1):
        factors[i] = k
        base, prev_close = rows[i][6], rows[i - 1][4]
        if base > 0 and prev_close > 0 and abs(base / prev_close - 1) > 0.02:
            k *= base / prev_close
    factors[0] = k
    return [
        (d, o * f, h * f, l * f, c * f, v / f)
        for (d, o, h, l, c, v, _), f in zip(rows, factors)
    ]


def import_marcap(data_dir: Path, start: str, end: str | None, cache_dir: Path) -> None:
    """FinanceData/marcap (KRX 전 종목 일별 데이터, 상장폐지 포함) parquet 을 캐시로 변환."""
    import pandas as pd

    y0 = date.fromisoformat(start).year
    y1 = date.fromisoformat(end).year if end else date.today().year
    files = [data_dir / f"marcap-{y}.parquet" for y in range(y0, y1 + 1)]
    cols = ["Code", "Name", "Market", "Date", "Open", "High", "Low", "Close", "Changes", "Volume"]
    df = pd.concat([pd.read_parquet(f, columns=cols) for f in files if f.exists()])
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"])]
    df = df[(df["Date"] >= start) & (df["Date"] <= (end or "2100-01-01"))]
    df = df.sort_values(["Code", "Date"])

    cache_dir.mkdir(parents=True, exist_ok=True)
    names = {}
    for code, g in df.groupby("Code", sort=False):
        name = str(g["Name"].iloc[-1])
        if not _is_common_stock(code, name):
            continue
        names[code] = name
        rows = [
            (d.date().isoformat(), o, h, l, c, v, c - ch)
            for d, o, h, l, c, ch, v in zip(g["Date"], g["Open"], g["High"], g["Low"], g["Close"], g["Changes"], g["Volume"])
        ]
        adj = adjust_splits(rows)
        # 거래정지일(거래량 0·시가 0)은 제외
        _write_bars(cache_dir / f"{code}.csv", [r for r in adj if r[5] > 0 and r[1] > 0])
    with open(cache_dir / "_names.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name"])
        w.writerows(sorted(names.items()))
    log.info("marcap → 캐시 %d 종목 (%s ~ %s)", len(names), start, end or "최신")


def download(source: str, start: str, end: str | None, cache_dir: Path) -> None:
    """KOSPI·KOSDAQ 보통주 일봉(수정주가)을 캐시에 저장. 이미 받은 종목은 건너뜀."""
    import pandas as pd  # noqa: F401  (pykrx / FinanceDataReader 의존성)

    cache_dir.mkdir(parents=True, exist_ok=True)
    end = end or date.today().isoformat()
    names: dict[str, str] = {}

    if source == "pykrx":
        from pykrx import stock

        # 기간 중 상장됐던 종목(상장폐지 포함)을 모으기 위해 매월 초 종목 목록을 합친다
        month = date.fromisoformat(start).replace(day=1)
        while month <= date.fromisoformat(end):
            d = stock.get_nearest_business_day_in_a_week(month.strftime("%Y%m%d"))
            for market in ("KOSPI", "KOSDAQ"):
                for code in stock.get_market_ticker_list(d, market=market):
                    if code not in names:
                        names[code] = stock.get_market_ticker_name(code)
            month = (month + timedelta(days=32)).replace(day=1)

        def fetch(code):
            df = stock.get_market_ohlcv_by_date(start.replace("-", ""), end.replace("-", ""), code, adjusted=True)
            return [(i.date().isoformat(), r["시가"], r["고가"], r["저가"], r["종가"], r["거래량"]) for i, r in df.iterrows()]
    elif source == "fdr":
        import FinanceDataReader as fdr

        for market in ("KOSPI", "KOSDAQ"):
            listing = fdr.StockListing(market)
            code_col = "Code" if "Code" in listing.columns else "Symbol"
            for _, r in listing.iterrows():
                names[str(r[code_col])] = str(r["Name"])

        def fetch(code):
            df = fdr.DataReader(code, start, end)
            return [(i.date().isoformat(), r["Open"], r["High"], r["Low"], r["Close"], r["Volume"]) for i, r in df.iterrows()]
    else:
        raise ValueError(f"알 수 없는 데이터 소스: {source}")

    names = {c: n for c, n in names.items() if _is_common_stock(c, n)}
    with open(cache_dir / "_names.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "name"])
        w.writerows(sorted(names.items()))

    todo = [c for c in sorted(names) if not (cache_dir / f"{c}.csv").exists()]
    log.info("종목 %d개 중 %d개 다운로드 (%s)", len(names), len(todo), source)
    for n, code in enumerate(todo, 1):
        try:
            rows = fetch(code)
        except Exception as exc:
            log.warning("%s 다운로드 실패: %s", code, exc)
            continue
        _write_bars(cache_dir / f"{code}.csv", rows)
        if n % 100 == 0:
            log.info("  %d / %d", n, len(todo))


# --------------------------------------------------------------------- CLI
def _fmt_pct(x: float) -> str:
    return f"{x:+.1%}"


def print_report(results: list[Result]) -> None:
    rows = [r.summary() for r in results]
    header = ["기간", "수익률", "MDD", "거래수", "승률", "평균수익", "평균손실", "손익비(PF)", "평균보유일", "최종자산"]
    print(" | ".join(header))
    for r in rows:
        print(" | ".join([
            r["기간"], _fmt_pct(r["수익률"]), _fmt_pct(r["MDD"]), str(r["거래수"]), f"{r['승률']:.0%}",
            _fmt_pct(r["평균수익"]), _fmt_pct(r["평균손실"]), f"{r['손익비(PF)']:.2f}",
            f"{r['평균보유일']:.1f}", f"{r['최종자산']:,.0f}",
        ]))
    for res, r in zip(results, rows):
        print(f"  {res.start.year} 청산사유: {r['청산사유']}, 기간 말 미청산 {r['미청산']}종목")


def save_trades(results: list[Result], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "trades.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["연도", "종목코드", "종목명", "급등일", "매수일", "매수가", "수량", "매도일", "매도가", "사유", "보유일", "손익", "수익률"])
        for res in results:
            for t in res.trades:
                w.writerow([
                    res.start.year, t.symbol, t.name, t.surge_date, t.entry_date, round(t.entry_price),
                    t.qty, t.exit_date or "", round(t.exit_price) if t.exit_price else "", t.reason or "보유중",
                    t.hold_days, round(t.pnl), f"{t.ret:.4f}",
                ])
    return path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tossbot.backtest", description="눌림목 전략 백테스트")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("download", help="과거 일봉 다운로드")
    d.add_argument("--source", choices=["marcap", "pykrx", "fdr"], default="marcap")
    d.add_argument("--marcap-dir", type=Path, default=Path("marcap/data"),
                   help="git clone https://github.com/FinanceData/marcap 한 폴더의 data 경로")
    d.add_argument("--start", default="2023-09-01", help="지표 계산을 위해 백테스트 시작 3~4개월 전부터")
    d.add_argument("--end", default=None)
    d.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    r = sub.add_parser("run", help="연도별 백테스트 실행")
    r.add_argument("--years", type=int, nargs="+", default=[2024, 2025, 2026])
    r.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    r.add_argument("--out", type=Path, default=Path("backtest_results"))
    r.add_argument("--env", default=".env")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    if args.command == "download":
        if args.source == "marcap":
            import_marcap(args.marcap_dir, args.start, args.end, args.cache)
        else:
            download(args.source, args.start, args.end, args.cache)
        return

    load_dotenv(args.env)
    settings = BacktestSettings.from_config(Config.from_env())
    data = load_cache(args.cache)
    if not data:
        raise SystemExit(f"{args.cache} 에 데이터가 없습니다. 먼저 download 를 실행하세요.")
    last_day = max(b.day for _, bars in data.values() for b in bars[-1:])
    results = [
        run_backtest(data, date(y, 1, 1), min(date(y, 12, 31), last_day), settings) for y in args.years
    ]
    print(f"종목 {len(data)}개, 연도마다 {settings.initial_cash:,.0f}원으로 새로 시작\n")
    print_report(results)
    print(f"\n거래 내역: {save_trades(results, args.out)}")


if __name__ == "__main__":
    main()
