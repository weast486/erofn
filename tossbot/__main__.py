"""CLI 진입점.

    python -m tossbot config       # .env 를 반영한 실제 적용 설정 (API 키는 가림)
    python -m tossbot check        # API 연결/계좌/매수가능금액 확인
    python -m tossbot select       # 지금 기준 매수 후보 출력 (주문 없음)
    python -m tossbot status       # 봇 보유 포지션 및 매매 이력
    python -m tossbot run          # 자동매매 상주 실행
    python -m tossbot liquidate    # 봇 보유 종목 즉시 전량 매도 (비상용)
    python -m tossbot sell 종목코드  # 봇 보유 종목 중 그 종목만 즉시 시장가 매도 (봇이 켜져 있으면 봇에게 맡김)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from .broker import Broker
from .client import TossClient
from .config import KST, Config, load_dotenv
from .market_calendar import TradingDay, trading_day_from_api
from .selector import build_watchlist, entry_signal
from .state import StateStore
from .breakout import select_breakouts
from .rsi import select_rsi
from .strategy import BreakoutStrategy, ComboStrategy, PullbackStrategy, make_strategy, params_from_config

log = logging.getLogger("tossbot")


def setup_logging(cfg: Config, verbose: bool) -> None:
    Path(cfg.log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in (
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(cfg.log_dir) / "tossbot.log", encoding="utf-8"),
    ):
        handler.setFormatter(fmt)
        root.addHandler(handler)


def build(cfg: Config) -> tuple[TossClient, PullbackStrategy]:
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url, cfg.account_seq)
    strategy = make_strategy(cfg, Broker(client, cfg.dry_run), StateStore(cfg.state_file))
    return client, strategy


def cmd_config(cfg: Config, _c, _s) -> None:
    """.env 를 반영한 실제 적용 설정 출력 (API 키는 가림). API 호출 없음."""
    from dataclasses import fields

    for f in fields(cfg):
        value = getattr(cfg, f.name)
        if f.name in ("client_id", "client_secret"):
            value = "(입력됨)" if value else "(비어 있음)"
        print(f"{f.name:<28} {value}")
    print(f"{'slot_budget (1주 가격 상한)':<28} {cfg.slot_budget:,}")


def cmd_check(cfg: Config, client: TossClient, _s) -> None:
    for acc in client.get_accounts():
        print(f"계좌 {acc['accountNo']} seq={acc['accountSeq']} type={acc['accountType']}")
    print("사용 계좌 seq:", client.get_account_seq())
    print("매수 가능 금액(KRW):", client.get_buying_power("KRW")["cashBuyingPower"])
    day = trading_day_from_api(client.get_market_calendar_kr())
    print(f"오늘 {day.today} 개장={day.is_open} (다음 거래일 {day.next_business_day})")
    print("DRY_RUN:", cfg.dry_run)


def cmd_select(cfg: Config, client: TossClient, strategy: PullbackStrategy) -> None:
    """지금 기준 매수 후보 출력 (주문 없음). 신고가 전략은 15시 무렵 실행해야 의미가 있다."""
    today = datetime.now(KST).date()
    if isinstance(strategy, BreakoutStrategy):
        cands = select_breakouts(client, set(strategy.state.positions), cfg.num_stocks, strategy.params(), today)
        print(f"{'종목':<16} {'현재가':>9} {'20일최고':>9} {'등락':>7} {'거래대금(억)':>10}")
        for c in cands:
            print(f"{c.name[:10]+'('+c.symbol+')':<16} {c.price:>9,.0f} {c.prev_high:>9,.0f} {c.change:>+7.1%} {c.day_amount/1e8:>10,.0f}")
        if not cands:
            print("조건을 만족하는 신고가 돌파 종목이 없습니다.")
        if isinstance(strategy, ComboStrategy):
            rs = select_rsi(client, strategy.universe, set(strategy.state.positions), cfg.num_stocks,
                            strategy.rsi_params(), today)
            print(f"\nRSI 과매도 후보 (코스피 시총 상위 {len(strategy.universe)}, RSI({cfg.rsi_period}) < {cfg.rsi_buy:g})")
            for c in rs:
                print(f"{c.name[:10]+'('+c.symbol+')':<16} {c.price:>9,.0f}  RSI {c.rsi:5.1f}")
            if not rs:
                print("RSI 과매도 종목이 없습니다.")
        return
    params = params_from_config(cfg)
    watch = build_watchlist(client, set(strategy.state.surge_seen), params, today)
    if not watch:
        print("감시할 급등 종목이 없습니다.")
        return
    prices = {p["symbol"]: float(p["lastPrice"]) for p in client.get_prices(list(watch))}
    print(f"{'종목':<16} {'급등일':>6} {'급등폭':>6} {'현재가':>9} {cfg.ma_period}일선 {'괴리':>6}  신호")
    rows = []
    for w in watch.values():
        price = prices.get(w.symbol)
        if price is None:
            continue
        ok, ma = entry_signal(w, price, params)
        rows.append((price / ma - 1, w, price, ma, ok))
    for gap, w, price, ma, ok in sorted(rows, key=lambda r: abs(r[0])):
        print(
            f"{w.name[:10]+'('+w.symbol+')':<16} {w.surge_date:%m/%d} {w.surge_pct:>6.1%} {price:>9,.0f} "
            f"{ma:>7,.0f} {gap:>+6.1%}  {'매수' if ok else ''}"
        )


def cmd_status(cfg: Config, _c, strategy: PullbackStrategy) -> None:
    st = strategy.state
    print(f"[{'DRY_RUN' if cfg.dry_run else 'LIVE'}] 보유 {len(st.positions)}/{cfg.num_stocks}")
    for p in st.positions.values():
        tp = strategy.tp_for(p)
        tp_text = f"{tp:,}({'조건주문' if p.tp_co_id else '봇감시'})" if tp else f"RSI {cfg.rsi_sell:g} 회복 시"
        print(
            f"  [{'RSI' if p.kind == 'rsi' else '신고가'}] {p.symbol} {p.name} {p.quantity}주 @ {p.entry_price:,.0f} "
            f"{p.hold_days}일째 status={p.status} "
            f"손절={strategy.stop_for(p):,}({'조건주문' if p.stop_co_id else '봇감시'}) 익절={tp_text}"
        )
    if st.history:
        rets = [h["return_pct"] for h in st.history if h.get("return_pct") is not None]
        print(f"청산 {len(st.history)}건, 평균 수익률 {sum(rets)/len(rets):.2f}%" if rets else f"청산 {len(st.history)}건")
        for h in st.history[-20:]:
            kind = "RSI" if h.get("kind") == "rsi" else "신고가"
            print(f"  {h['closed_at']} [{kind}] {h['symbol']} {h['name']} {h['reason']} {h.get('return_pct')}%")


def cmd_liquidate(cfg: Config, _c, strategy: PullbackStrategy) -> None:
    strategy.sync_orders()
    strategy.liquidate_all("MANUAL")
    cmd_status(cfg, _c, strategy)


ALIVE_FILE = "bot_alive.txt"  # 켜져 있는 봇이 돌 때마다 고쳐 쓰는 파일 (sell 명령이 봇이 켜져 있는지 아는 용도)
SELL_PREFIX = "sell_request_"  # sell 명령이 남기는 요청 파일: sell_request_종목코드.txt


def sell_requests(cfg: Config) -> list[Path]:
    return sorted(Path(cfg.state_dir).glob(f"{SELL_PREFIX}*.txt"))


def handle_sell_requests(cfg: Config, strategy: PullbackStrategy) -> None:
    """켜져 있는 봇이 즉시 매도 요청 파일을 읽어 처리 (파일을 먼저 지워 한 번만 실행)."""
    for path in sell_requests(cfg):
        symbol = path.stem[len(SELL_PREFIX):]
        try:
            path.unlink()
        except OSError:
            continue
        strategy.manual_sell(symbol)


def cmd_sell(cfg: Config, client: TossClient, strategy: PullbackStrategy, wanted: list[str]) -> None:
    """봇 보유 종목 중 고른 종목만 즉시 시장가 매도. 봇이 켜져 있으면 요청만 남기고 봇이 판다 (기록이 어긋나지 않게)."""
    held = strategy.state.positions
    symbols = []
    for w in wanted:
        hit = [p.symbol for p in held.values() if w in (p.symbol, p.name)]
        if not hit:
            print(f"{w}: 봇이 보유한 종목이 아닙니다. (보유: {', '.join(f'{p.symbol} {p.name}' for p in held.values()) or '없음'})")
            continue
        symbols += hit
    if not symbols:
        return
    now = datetime.now(KST)
    day = trading_day_from_api(client.get_market_calendar_kr(now.date().isoformat()))
    if not (day.is_open and day.market_open <= now < day.market_close):
        print("지금은 장중이 아니라 시장가 매도를 할 수 없습니다. 장중(09:00~15:30)에 다시 실행해 주세요.")
        return
    alive = Path(cfg.state_dir) / ALIVE_FILE
    running = alive.exists() and time.time() - alive.stat().st_mtime < cfg.monitor_interval_seconds * 2 + 30
    if running:
        for s in symbols:
            (Path(cfg.state_dir) / f"{SELL_PREFIX}{s}.txt").write_text(now.isoformat(), encoding="utf-8")
        print(f"켜져 있는 봇에게 매도를 요청했습니다: {', '.join(symbols)} (최대 1~2분)")
    else:
        print("봇이 꺼져 있어 여기서 바로 매도합니다.")
        strategy.sync_orders()
        for s in symbols:
            strategy.manual_sell(s)
    for _ in range(60):  # 최대 2분 동안 결과 확인
        if running:
            strategy.state = strategy.store.load()
        else:
            strategy.sync_orders()
        if not any(s in strategy.state.positions for s in symbols):
            break
        time.sleep(2)
    for s in symbols:
        if s in strategy.state.positions:
            p = strategy.state.positions[s]
            print(f"  {s} {p.name}: 아직 처리 중 (상태 {p.status}, 남은 {p.quantity}주) — 잠시 뒤 status 로 확인해 주세요")
        else:
            h = next((x for x in reversed(strategy.state.history) if x.get("symbol") == s), {})
            print(f"  {s} {h.get('name', '')}: 매도 완료 {h.get('return_pct')}%")


def cmd_run(cfg: Config, client: TossClient, strategy: PullbackStrategy) -> None:
    if cfg.strategy == "combo":
        log.info(
            "신고가 + RSI 한 계좌 자동매매 시작 [%s] %s~%s, 최대 %d종목. 신고가 먼저(손절 -%s%% 익절 +%s%%), "
            "남는 자리 RSI(%d) < %s (코스피 시총 상위 목록, RSI %s 이상·%d거래일째 매도, 손절 -%s%%)",
            "DRY_RUN" if cfg.dry_run else "LIVE", cfg.buy_start, cfg.buy_end, cfg.num_stocks,
            cfg.stop_loss_pct, cfg.take_profit_pct, cfg.rsi_period, cfg.rsi_buy, cfg.rsi_sell,
            cfg.rsi_max_hold_days, cfg.rsi_stop_loss_pct,
        )
    elif cfg.strategy == "breakout":
        log.info(
            "신고가 돌파 자동매매 시작 [%s] %d일 신고가(%d일 내 첫), 거래대금 %s억 이상, %s~%s 매수, "
            "최대 %d종목 x %s원, 손절 -%s%% 익절 +%s%%",
            "DRY_RUN" if cfg.dry_run else "LIVE", cfg.breakout_entry_days, cfg.breakout_first_in_days,
            f"{cfg.min_day_amount / 1e8:,.0f}", cfg.buy_start, cfg.buy_end, cfg.num_stocks,
            f"{cfg.slot_budget:,}", cfg.stop_loss_pct, cfg.take_profit_pct,
        )
    else:
        log.info(
            "눌림목 자동매매 시작 [%s] %d일선 터치, 손절 -%s%% 익절 +%s%%",
            "DRY_RUN" if cfg.dry_run else "LIVE", cfg.ma_period, cfg.stop_loss_pct, cfg.take_profit_pct,
        )
    cached: TradingDay | None = None
    while True:
        now = datetime.now(KST)
        try:
            alive = Path(cfg.state_dir) / ALIVE_FILE
            alive.parent.mkdir(parents=True, exist_ok=True)
            alive.write_text(now.isoformat(), encoding="utf-8")
            handle_sell_requests(cfg, strategy)
            if cached is None or cached.today != now.date():
                cached = trading_day_from_api(client.get_market_calendar_kr(now.date().isoformat()))
                log.info("%s 개장=%s", cached.today, cached.is_open)
            strategy.tick(now, cached)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("tick 처리 중 오류")

        in_session = cached is not None and cached.is_open and cached.market_open <= now < cached.market_close
        wait_until = time.time() + (cfg.monitor_interval_seconds if in_session else 300)
        while time.time() < wait_until:  # 쉬는 동안에도 즉시 매도 요청이 오면 바로 깨어남
            if in_session and sell_requests(cfg):
                break
            time.sleep(2)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tossbot", description="토스증권 자동매매 봇 (신고가 돌파 / 신고가+RSI / 눌림목)")
    parser.add_argument("command", choices=["config", "check", "select", "status", "run", "liquidate", "sell"])
    parser.add_argument("symbols", nargs="*", help="sell: 즉시 매도할 종목코드 (여러 개 가능)")
    parser.add_argument("--env", default=".env", help=".env 파일 경로")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    cfg = Config.from_env()
    if args.command == "config":  # API 키 없이도 설정만 확인
        cmd_config(cfg, None, None)
        return
    setup_logging(cfg, args.verbose)
    client, strategy = build(cfg)
    if args.command == "sell":
        if not args.symbols:
            cmd_status(cfg, client, strategy)
            print("사용법: python -m tossbot sell 종목코드")
            return
        cmd_sell(cfg, client, strategy, args.symbols)
        return
    {
        "check": cmd_check,
        "select": cmd_select,
        "status": cmd_status,
        "run": cmd_run,
        "liquidate": cmd_liquidate,
    }[args.command](cfg, client, strategy)


if __name__ == "__main__":
    main()
