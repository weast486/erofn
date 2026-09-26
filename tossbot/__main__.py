"""CLI 진입점.

    python -m tossbot check        # API 연결/계좌/매수가능금액 확인
    python -m tossbot select       # 지금 기준 종가배팅 후보만 출력 (주문 없음, 15시 무렵 실행)
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
from .selector import SelectionParams, select_stocks
from .state import StateStore
from .strategy import ClosingBetStrategy

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


def build(cfg: Config) -> tuple[TossClient, ClosingBetStrategy]:
    client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url, cfg.account_seq)
    strategy = ClosingBetStrategy(cfg, Broker(client, cfg.dry_run), StateStore(cfg.state_file))
    return client, strategy


def cmd_check(cfg: Config, client: TossClient, _s) -> None:
    for acc in client.get_accounts():
        print(f"계좌 {acc['accountNo']} seq={acc['accountSeq']} type={acc['accountType']}")
    print("사용 계좌 seq:", client.get_account_seq())
    print("매수 가능 금액(KRW):", client.get_buying_power("KRW")["cashBuyingPower"])
    day = trading_day_from_api(client.get_market_calendar_kr())
    print(f"오늘 {day.today} 개장={day.is_open} (다음 거래일 {day.next_business_day})")
    print("DRY_RUN:", cfg.dry_run)


def cmd_select(cfg: Config, client: TossClient, strategy: ClosingBetStrategy) -> None:
    """지금 시점 기준 종가배팅 후보 미리보기 (주문 없음). 장중 15시 무렵에 실행해야 의미가 있다."""
    params = SelectionParams(
        slot_budget=cfg.slot_budget,
        min_change_pct=cfg.min_change_pct,
        max_change_pct=cfg.max_change_pct,
        min_trading_amount=cfg.min_trading_amount,
        min_volume_ratio=cfg.min_volume_ratio,
        min_close_to_high=cfg.min_close_to_high,
    )
    picks = select_stocks(
        client, set(strategy.state.positions), cfg.num_stocks, params, datetime.now(KST).date()
    )
    print(f"{'순위':>4} {'종목':<16} {'현재가':>9} {'점수':>5} {'등락':>6} {'거래대금(억)':>10} {'거래량배':>7} {'고가대비':>7}")
    for i, c in enumerate(picks, 1):
        m = c.metrics
        print(
            f"{i:>4} {c.name[:10]+'('+c.symbol+')':<16} {c.close:>9,.0f} {c.score:>5.2f} {m['change']:>6.1%} "
            f"{m['trading_amount']/1e8:>10,.0f} {m['vol_ratio']:>7.1f} {m['close_to_high']:>7.1%}"
        )
    if not picks:
        print("조건을 통과한 종목이 없습니다 (오늘은 0종목 매수).")


def cmd_status(cfg: Config, _c, strategy: ClosingBetStrategy) -> None:
    st = strategy.state
    print(f"[{'DRY_RUN' if cfg.dry_run else 'LIVE'}] 마지막 매수일: {st.last_buy_date}, 보유 {len(st.positions)}/{cfg.num_stocks}")
    for p in st.positions.values():
        print(
            f"  {p.symbol} {p.name} {p.quantity}주 @ {p.entry_price:,.0f} status={p.status} "
            f"손절={strategy.stop_price(p.entry_price):,}({'조건주문' if p.stop_co_id else '봇감시'}) "
            f"익절={strategy.take_profit_price(p.entry_price):,}({'조건주문' if p.tp_co_id else '봇감시'})"
        )
    if st.history:
        rets = [h["return_pct"] for h in st.history if h.get("return_pct") is not None]
        print(f"청산 {len(st.history)}건, 평균 수익률 {sum(rets)/len(rets):.2f}%" if rets else f"청산 {len(st.history)}건")
        for h in st.history[-20:]:
            print(f"  {h['closed_at']} {h['symbol']} {h['name']} {h['reason']} {h.get('return_pct')}%")


def cmd_liquidate(cfg: Config, _c, strategy: ClosingBetStrategy) -> None:
    strategy.sync_orders()
    strategy.liquidate_all("MANUAL")
    cmd_status(cfg, _c, strategy)


def cmd_run(cfg: Config, client: TossClient, strategy: ClosingBetStrategy) -> None:
    log.info(
        "종가배팅 자동매매 시작 [%s] 매일 %s~%s 매수, 최대 %d종목 x %s원, 손절 -%s%% 익절 +%s%%",
        "DRY_RUN" if cfg.dry_run else "LIVE", cfg.buy_start, cfg.buy_end, cfg.num_stocks,
        f"{cfg.slot_budget:,}", cfg.stop_loss_pct, cfg.take_profit_pct,
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
    parser = argparse.ArgumentParser(prog="tossbot", description="토스증권 종가배팅 자동매매 봇")
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
