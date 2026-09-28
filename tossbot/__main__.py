"""CLI 진입점.

    python -m tossbot check        # API 연결/계좌/매수가능금액 확인
    python -m tossbot select       # 급등 종목 감시 목록과 7일선 대비 위치 출력 (주문 없음)
    python -m tossbot status       # 봇 보유 포지션 및 매매 이력
    python -m tossbot run          # 자동매매 상주 실행
    python -m tossbot liquidate    # 봇 보유 종목 즉시 전량 매도 (비상용)
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
from .strategy import PullbackStrategy, params_from_config

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
    strategy = PullbackStrategy(cfg, Broker(client, cfg.dry_run), StateStore(cfg.state_file))
    return client, strategy


def cmd_check(cfg: Config, client: TossClient, _s) -> None:
    for acc in client.get_accounts():
        print(f"계좌 {acc['accountNo']} seq={acc['accountSeq']} type={acc['accountType']}")
    print("사용 계좌 seq:", client.get_account_seq())
    print("매수 가능 금액(KRW):", client.get_buying_power("KRW")["cashBuyingPower"])
    day = trading_day_from_api(client.get_market_calendar_kr())
    print(f"오늘 {day.today} 개장={day.is_open} (다음 거래일 {day.next_business_day})")
    print("DRY_RUN:", cfg.dry_run)


def cmd_select(cfg: Config, client: TossClient, strategy: PullbackStrategy) -> None:
    """급등 종목 감시 목록과 현재 7일선 대비 위치 출력 (주문 없음)."""
    today = datetime.now(KST).date()
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
        print(
            f"  {p.symbol} {p.name} {p.quantity}주 @ {p.entry_price:,.0f} {p.hold_days}일째 status={p.status} "
            f"손절={strategy.stop_price(p.entry_price):,}({'조건주문' if p.stop_co_id else '봇감시'}) "
            f"익절={strategy.take_profit_price(p.entry_price):,}({'조건주문' if p.tp_co_id else '봇감시'})"
        )
    if st.history:
        rets = [h["return_pct"] for h in st.history if h.get("return_pct") is not None]
        print(f"청산 {len(st.history)}건, 평균 수익률 {sum(rets)/len(rets):.2f}%" if rets else f"청산 {len(st.history)}건")
        for h in st.history[-20:]:
            print(f"  {h['closed_at']} {h['symbol']} {h['name']} {h['reason']} {h.get('return_pct')}%")


def cmd_liquidate(cfg: Config, _c, strategy: PullbackStrategy) -> None:
    strategy.sync_orders()
    strategy.liquidate_all("MANUAL")
    cmd_status(cfg, _c, strategy)


def cmd_run(cfg: Config, client: TossClient, strategy: PullbackStrategy) -> None:
    log.info(
        "눌림목 자동매매 시작 [%s] +%s%% 급등 후 %d일선 터치 시 매수 (이탈 한도 -%s%%) (%s~%s), 최대 %d종목 x %s원, "
        "손절 -%s%% 익절 +%s%%",
        "DRY_RUN" if cfg.dry_run else "LIVE", cfg.surge_pct, cfg.ma_period, cfg.ma_max_break_pct,
        cfg.buy_start, cfg.buy_end, cfg.num_stocks, f"{cfg.slot_budget:,}", cfg.stop_loss_pct, cfg.take_profit_pct,
    )
    cached: TradingDay | None = None
    while True:
        now = datetime.now(KST)
        try:
            if cached is None or cached.today != now.date():
                cached = trading_day_from_api(client.get_market_calendar_kr(now.date().isoformat()))
                log.info("%s 개장=%s", cached.today, cached.is_open)
            strategy.tick(now, cached)
        except KeyboardInterrupt:
            raise
        except Exception:
            log.exception("tick 처리 중 오류")

        in_session = cached is not None and cached.is_open and cached.market_open <= now < cached.market_close
        time.sleep(cfg.monitor_interval_seconds if in_session else 300)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tossbot", description="토스증권 눌림목 자동매매 봇")
    parser.add_argument("command", choices=["check", "select", "status", "run", "liquidate"])
    parser.add_argument("--env", default=".env", help=".env 파일 경로")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    cfg = Config.from_env()
    setup_logging(cfg, args.verbose)
    client, strategy = build(cfg)
    {
        "check": cmd_check,
        "select": cmd_select,
        "status": cmd_status,
        "run": cmd_run,
        "liquidate": cmd_liquidate,
    }[args.command](cfg, client, strategy)


if __name__ == "__main__":
    main()
