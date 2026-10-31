"""스플릿 봇 명령 (split check / run / status). 키 값은 화면·로그에 남기지 않는다."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from tossbot.config import load_dotenv

from .botmain import setup_logging
from .client import BinanceClient
from .split import AFTER_STOP_REQUEST, SplitConfig, SplitTrader, fvg_conflict


def split_main(action: str, env: str, values: list[str] | None = None) -> None:
    load_dotenv(env)
    cfg = SplitConfig.from_env()
    if action == "after-stop":
        values = values or []
        if values[:1] == ["off"]:
            plan = {}
        elif len(values) == 3:
            lev, sl, tp = (float(v) for v in values)
            plan = {"lev": lev, "stop_pct": sl, "tp_pct": tp}
        else:
            print("사용법: split after-stop 배율 손절% 익절%  (예: 2 3 10) / split after-stop off")
            return
        d = Path(cfg.state_dir)
        d.mkdir(parents=True, exist_ok=True)
        (d / AFTER_STOP_REQUEST).write_text(json.dumps(plan), encoding="utf-8")
        print("다음 손절 때 한 번 롱 예약:" if plan else "손절 뒤 롱 예약 취소:", plan or "",
              "(켜져 있는 봇이 몇 초 안에 읽음, 꺼져 있으면 켤 때 읽음)")
        return
    if action == "status":
        print(SplitTrader(BinanceClient(), cfg).status())
        return
    setup_logging(cfg.state_dir)
    if not cfg.dry_run and not (cfg.api_key and cfg.api_secret):
        print(f"실제 주문(BINANCE_DRY_RUN=false)인데 API 키가 비어 있어요. 메모장으로 {env} 를 열어 키를 넣어 주세요.")
        return
    warn = fvg_conflict(cfg)
    trader = SplitTrader(BinanceClient(cfg.api_key, cfg.api_secret), cfg)
    if action == "check":
        print(f"설정: {cfg.describe()}")
        try:
            trader.prepare()
            p = trader.price()
            print(f"{cfg.symbol} 현재가 {p:g}, 가격 단위 {trader.f['tick']:g}, 수량 단위 {trader.f['step']:g}, "
                  f"최소 주문 {trader.f['min_notional']:g} USDT, 지정가 허용 범위 +{trader.f['pct_up'] * 100:g}% / "
                  f"-{trader.f['pct_down'] * 100:g}%")
            d = p * cfg.box_pct / 100 / cfg.splits
            notional = trader.st.stake / cfg.splits * cfg.leverage
            print(f"지금 시작하면: 1차 {p:g}, 간격 {d:.4f} ({d / p * 100:.2f}%), {cfg.splits}차 {p - d * (cfg.splits - 1):.4f}, "
                  f"손절 {p - d * (cfg.splits - 1 + cfg.stop_steps):.4f}, 차수당 {notional:.2f} USDT")
            if notional < trader.f["min_notional"] * 1.05:
                print(f"주의: 차수당 금액이 최소 주문 금액({trader.f['min_notional']:g} USDT)보다 작아요. "
                      "SP_CAPITAL 을 늘리거나 SP_SPLITS 를 줄여 주세요.")
            if cfg.api_key and cfg.api_secret:
                print(f"계좌 평가금액(USDT): {trader.c.equity():,.2f}")
                amt, _ = trader.c.position_amt(cfg.symbol)
                print(f"지금 {cfg.symbol} 포지션: {amt:g}")
        except Exception as exc:  # noqa: BLE001
            print(f"바이낸스 연결·조회 실패: {exc}")
        if warn:
            print("주의:", warn)
        print("확인 끝 (주문은 보내지 않았어요)")
        return
    if warn and not cfg.dry_run:
        print("실제 주문을 시작하지 않았어요:", warn)
        return
    from kiwoombot.__main__ import acquire_lock

    lock = acquire_lock(cfg.state_dir)
    if lock is None:
        print("스플릿 봇이 이미 켜져 있어요. 먼저 켠 창이 그대로 돌고 있으니 이 창은 닫아도 됩니다.")
        return
    if not cfg.dry_run:
        logging.getLogger("binancebot").warning("실제 주문 모드: %s", cfg.describe())
    trader.run()
    print(trader.status())
