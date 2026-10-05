"""봇 명령 (check / run / loop). 키 값은 화면·로그에 남기지 않는다."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from tossbot.config import load_dotenv

from .backtest import ET
from .bot import BotConfig, FvgTrader, loop, trading_day
from .client import BinanceClient


def setup_logging(state_dir: str) -> None:
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(Path(state_dir) / "bot.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def check(trader: FvgTrader, cfg: BotConfig) -> None:
    c = trader.c
    c.sync_time()
    print(f"설정: {cfg.describe()}")
    print(f"바이낸스 서버 시각 차이: {c.offset_ms} ms")
    trader.load_meta()
    print(f"거래 중인 미국 주식 토큰 {sum(m.get('underlyingType') == 'EQUITY' for m in trader.meta.values())}개")
    if cfg.api_key and cfg.api_secret:
        print(f"평가금액(USDT): {c.equity():,.2f}")
        if c.hedge_mode():
            print("주의: 바이낸스 선물이 '양방향(Hedge) 포지션 모드'예요. 봇은 '단방향(One-way)' 모드에서만 동작해요. "
                  "앱 선물 화면 설정에서 바꿔 주세요.")
        else:
            print("포지션 모드: 단방향 (정상)")
    else:
        print(f"API 키 없음 → 드라이런 평가금액 {cfg.dry_equity:,.0f} USDT 로 계산")
        if not cfg.dry_run:
            print("주의: BINANCE_DRY_RUN=false 인데 키가 없어요. 실제 주문을 하려면 키를 넣어야 해요.")
    now = datetime.now(ET)
    day = now.date()
    while not trading_day(day):
        from datetime import timedelta
        day += timedelta(days=1)
    print(f"{day} (미국 동부) 대상 종목 계산 중... (1분쯤 걸려요)")
    print("대상:", ", ".join(trader.candidates(day)))
    print("확인 끝 (주문은 보내지 않았어요)")


def bot_main(cmd: str, env: str) -> None:
    load_dotenv(env)
    cfg = BotConfig.from_env()
    setup_logging(cfg.state_dir)
    if not cfg.dry_run and not (cfg.api_key and cfg.api_secret):
        print(f"실제 주문(BINANCE_DRY_RUN=false)인데 API 키가 비어 있어요. 메모장으로 {env} 를 열어 키를 넣어 주세요.")
        return

    def make() -> FvgTrader:
        return FvgTrader(BinanceClient(cfg.api_key, cfg.api_secret), cfg)

    if cmd == "check":
        try:
            check(make(), cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"바이낸스 연결·조회 실패: {exc}")
            print("인터넷 연결과 API 키(선물 거래 허용, IP 제한)를 확인해 주세요.")
        return
    from kiwoombot.__main__ import acquire_lock

    lock = acquire_lock(cfg.state_dir)
    if lock is None:
        print("바이낸스 봇이 이미 켜져 있어요. 먼저 켠 창이 그대로 돌고 있으니 이 창은 닫아도 됩니다.")
        return
    if cmd == "loop":
        loop(make, cfg)
    else:
        t = make()
        t.c.sync_time()
        t.run()
