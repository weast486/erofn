"""봇 명령 (check / run / loop). 키 값은 화면·로그에 남기지 않는다."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from tossbot.config import load_dotenv

from .backtest import ET
from .bot import BotConfig, FvgTrader, loop, trading_day
from .client import BinanceClient


def make_client(cfg: BotConfig):
    if cfg.exchange == "okx":
        from .okx_client import OkxClient

        return OkxClient(cfg.api_key, cfg.api_secret, cfg.api_passphrase, demo=cfg.demo)
    return BinanceClient(cfg.api_key, cfg.api_secret)


def setup_logging(state_dir: str) -> None:
    try:  # 창을 클릭해도 봇이 멈추지 않게 (winconsole.py)
        from winconsole import disable_quick_edit
        disable_quick_edit()
    except Exception:  # noqa: BLE001
        pass
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
    print(f"{cfg.exchange.upper()} 서버 시각 차이: {c.offset_ms} ms")
    trader.load_meta()
    print(f"거래 중인 미국 주식 토큰 {sum(m.get('underlyingType') == 'EQUITY' for m in trader.meta.values())}개")
    if cfg.api_key and cfg.api_secret:
        print(f"평가금액(USDT): {c.equity():,.2f}")
        if cfg.exchange == "okx":
            conf = c.config()
            if str(conf.get("acctLv")) == "1":
                print("주의: OKX 계정이 '현물' 모드예요. 선물을 하려면 앱 > 거래 설정 > 계정 모드를 '현물 및 선물'(또는 그 위)로 바꿔 주세요.")
        if c.hedge_mode():
            print("주의: 선물이 '양방향(Hedge / 롱·숏) 포지션 모드'예요. 봇은 '단방향(One-way / 순포지션)' 모드에서만 동작해요. "
                  "앱 선물 설정에서 바꿔 주세요.")
        else:
            print("포지션 모드: 단방향 (정상)")
    else:
        print(f"API 키 없음 → 드라이런 평가금액 {cfg.dry_equity:,.0f} USDT 로 계산")
        if not cfg.dry_run:
            print("주의: DRY_RUN=false 인데 키가 없어요. 실제 주문을 하려면 키를 넣어야 해요.")
    now = datetime.now(ET)
    day = now.date()
    while not trading_day(day):
        from datetime import timedelta
        day += timedelta(days=1)
    print(f"{day} (미국 동부) 대상 종목 계산 중... (1분쯤 걸려요)")
    print("대상:", ", ".join(trader.candidates(day)))
    print("확인 끝 (주문은 보내지 않았어요)")


def okx_order_test(env: str, symbol: str, yes: bool = False) -> None:
    """실전 전 OKX 실제 주문 응답 확인: 최소 수량 IOC 매수 → 체결·포지션 조회 → 먼 손절 등록 → 취소 → 시장가 매도.
    봇이 실전에서 쓰는 메서드를 그대로 한 번씩 부른다. 비용은 최소 수량 왕복 수수료(몇 센트)뿐."""
    import time

    from .client import filters_of, fmt, round_tick
    from .okx_client import OkxClient, to_inst

    load_dotenv(env)
    cfg = BotConfig.from_env()
    if cfg.exchange != "okx" or not (cfg.api_key and cfg.api_secret and cfg.api_passphrase):
        print(f"{env} 에 EXCHANGE=okx 와 OKX 키 3개(KEY·SECRET·PASSPHRASE)가 있어야 해요.")
        return
    c = OkxClient(cfg.api_key, cfg.api_secret, cfg.api_passphrase, demo=cfg.demo)
    results: list[tuple[str, bool, str]] = []

    def step(name: str, ok: bool, info: str = "") -> None:
        results.append((name, ok, info))
        print(f"[{'OK' if ok else '실패'}] {name} {info}")

    c.sync_time()
    meta = {m["symbol"]: m for m in c.exchange_info()["symbols"]}
    if symbol not in meta:
        print(f"{symbol} 이 OKX 에 없어요.")
        return
    f = filters_of(meta[symbol])
    price = c.prices()[symbol]
    qty = f["step"]
    conf = c.config()
    print(f"계좌 모드 acctLv={conf.get('acctLv')} 포지션 모드={conf.get('posMode')} 평가금액 {c.equity():,.2f} USDT")
    print(f"시험 종목 {symbol} 현재가 {price}, 최소 수량 {fmt(qty)}주 ≈ {qty * price:.2f} USDT (5배 → 증거금 약 {qty * price / 5:.2f})")
    if str(conf.get("acctLv")) == "1":
        print("계정 모드가 '현물'이라 선물 주문이 안 돼요. 앱에서 '현물 및 선물' 이상으로 바꾼 뒤 다시 해 주세요.")
        return
    if conf.get("posMode") == "long_short_mode":
        print("포지션 모드가 양방향(롱·숏)이에요. 앱에서 단방향(순포지션)으로 바꾼 뒤 다시 해 주세요.")
        return
    if c.position_amt(symbol)[0] != 0:
        print(f"{symbol} 에 이미 포지션이 있어 시험하지 않아요.")
        return
    if not yes and input("실제 주문을 보냅니다. 계속하려면 yes 입력: ").strip().lower() != "yes":
        print("취소했어요.")
        return
    opened = 0.0
    try:
        c.set_leverage(symbol, cfg.exchange_leverage)
        step("레버리지 설정", True, f"{cfg.exchange_leverage}배")
        px = round_tick(price * 1.005, f["tick"])
        r = c.order(symbol=symbol, side="BUY", type="LIMIT", timeInForce="IOC", quantity=fmt(qty), price=fmt(px))
        opened = float(r.get("executedQty") or 0)
        step("IOC 매수", opened > 0, f"주문가 {px} → 체결 {r.get('executedQty')}주 평균 {r.get('avgPrice')} 상태 {r.get('status')}")
        if opened <= 0:
            return
        time.sleep(1)
        amt, avg = c.position_amt(symbol)
        step("포지션 조회", abs(amt - opened) < qty / 2, f"{amt}주 평균 {avg}")
        fl = c.fills(symbol, order_id=r["orderId"])
        step("체결 내역 조회", bool(fl), f"{len(fl)}건 {[(x['side'], x['price'], x['qty']) for x in fl]}")
        stop = round_tick(price * 0.9, f["tick"])
        c.stop_market(symbol, "SELL", stop)
        algos = c._req("GET", "/api/v5/trade/orders-algo-pending", {"ordType": "conditional", "instId": to_inst(symbol)},
                       signed=True)
        step("거래소 손절 등록", bool(algos), f"손절가 {stop}, 걸린 손절 {len(algos)}개")
        c.cancel_all(symbol)
        time.sleep(1)
        left = c._req("GET", "/api/v5/trade/orders-algo-pending", {"ordType": "conditional", "instId": to_inst(symbol)},
                      signed=True)
        step("손절 취소", not left, f"남은 손절 {len(left)}개")
    except Exception as exc:  # noqa: BLE001
        step("오류", False, str(exc))
    finally:
        if opened > 0:
            try:
                c.cancel_all(symbol)
                r = c.order(symbol=symbol, side="SELL", type="MARKET", quantity=fmt(opened), reduceOnly="true")
                time.sleep(1)
                amt, _ = c.position_amt(symbol)
                step("시장가 정리", amt == 0, f"체결 {r.get('executedQty')}주 평균 {r.get('avgPrice')}, 남은 포지션 {amt}")
            except Exception as exc:  # noqa: BLE001
                step("시장가 정리", False, f"{exc} → OKX 앱에서 {symbol} 포지션을 직접 정리해 주세요")
    bad = [n for n, ok, _ in results if not ok]
    print("\n시험 결과:", "모두 정상 — 실전 주문 준비됨" if not bad else "실패 항목 " + ", ".join(bad) + " (화면을 캡처해 알려 주세요)")


KEEP = ("BINANCE_API_KEY", "BINANCE_API_SECRET", "BN_STATE_DIR", "OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE",
        "OKX_DEMO", "OKX_DRY_RUN")


def bot_main(cmd: str, env: str) -> None:
    if cmd == "update-settings":
        from kiwoombot.__main__ import update_settings

        example = Path(".env.okx.example") if Path(env).name.startswith(".env.okx") else Path(".env.binance.example")
        changes = update_settings(Path(env), example, KEEP)
        print(f"{env} 를 최신 설정으로 바꿨어요 (이전 파일: {Path(env).name}.bak, API 키는 그대로).")
        for k, before, after in changes:
            print(f"  {k}: {before} → {after}")
        if not changes:
            print("  바뀐 항목 없음 (이미 최신)")
        return
    load_dotenv(env)
    cfg = BotConfig.from_env()
    setup_logging(cfg.state_dir)
    if not cfg.dry_run and not (cfg.api_key and cfg.api_secret and (cfg.exchange != "okx" or cfg.api_passphrase)):
        print(f"실제 주문(DRY_RUN=false)인데 API 키가 비어 있어요. 메모장으로 {env} 를 열어 키를 넣어 주세요.")
        return

    def make() -> FvgTrader:
        return FvgTrader(make_client(cfg), cfg)

    if cmd == "check":
        try:
            check(make(), cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"{cfg.exchange.upper()} 연결·조회 실패: {exc}")
            print("인터넷 연결과 API 키(선물 거래 허용, IP 제한)를 확인해 주세요.")
        return
    from kiwoombot.__main__ import acquire_lock

    lock = acquire_lock(cfg.state_dir)
    if lock is None:
        print(f"{cfg.exchange.upper()} FVG 봇이 이미 켜져 있어요. 먼저 켠 창이 그대로 돌고 있으니 이 창은 닫아도 됩니다.")
        return
    if not cfg.dry_run:
        logging.getLogger("binancebot").warning("실제 주문 모드: %s", cfg.describe())
    if cmd == "loop":
        loop(make, cfg)
    else:
        t = make()
        t.c.sync_time()
        t.run()
