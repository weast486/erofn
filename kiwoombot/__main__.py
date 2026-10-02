"""키움 단타 봇 실행.

    python -m kiwoombot check        # 토큰·잔고·등락률 순위·분봉 조회가 되는지만 확인 (주문 없음)
    python -m kiwoombot candidates   # 오늘(장 전) 대상 종목만 고르고 끝
    python -m kiwoombot run          # 하루 매매 (08:50 대상 고르기 → 09:00~ 매수 → exit_time 정리)

설정은 .env.kiwoom (KIWOOM_APP_KEY, KIWOOM_SECRET_KEY, KIWOOM_MOCK, KIWOOM_DRY_RUN, KW_* 전략 값).
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path

from tossbot.config import KST, load_dotenv

from .client import KiwoomClient, num
from .config import KiwoomConfig
from .daytrade import DayTrader


def setup_logging(state_dir: str) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(Path(state_dir) / "daytrade.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)


def check(client: KiwoomClient) -> None:
    print(f"서버: {'모의투자' if client.mock else '실전'} ({client.base_url})")
    bal = client.balance()
    print(f"추정예탁자산: {num(bal.get('prsm_dpst_aset_amt')):,.0f}원, 보유 종목 {len(client.holdings())}개")
    print(f"미체결 주문: {len(client.unfilled())}건")
    ranks = client.change_rate_ranking(max_pages=1)
    print(f"등락률 상위 {len(ranks)}개, 예: " + ", ".join(f"{r.get('stk_nm')} {r.get('flu_rt')}%" for r in ranks[:5]))
    if ranks:
        code = str(ranks[0].get("stk_cd", "")).split("_")[0]
        rows = client.minute_chart(code)
        print(f"{code} 1분봉 {len(rows)}개, 최근: {rows[0] if rows else '-'}")
        daily = client.daily_chart(code, datetime.now(KST).strftime("%Y%m%d"))
        print(f"{code} 일봉 {len(daily)}개, 최근: {daily[0] if daily else '-'}")
    print("확인 끝 (주문은 보내지 않았어요)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kiwoombot", description="키움 단타 봇 (전일 +15%% 종목 전일 종가 재돌파)")
    p.add_argument("command", choices=["check", "candidates", "run"])
    p.add_argument("--env", default=".env.kiwoom")
    args = p.parse_args(argv)
    load_dotenv(args.env)
    cfg = KiwoomConfig.from_env()
    setup_logging(cfg.state_dir)
    client = KiwoomClient(cfg.app_key, cfg.secret_key, mock=cfg.mock)
    if args.command == "check":
        check(client)
        return
    trader = DayTrader(client, cfg)
    if args.command == "candidates":
        trader.prepare()
        return
    trader.run()


if __name__ == "__main__":
    main()
