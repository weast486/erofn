"""조건 주문 명령 (테스트 매매만): python -m binancebot manual add / list / cancel / run"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from tossbot.config import load_dotenv

from .client import BinanceClient
from .manual import ManualTrader, Store, describe, new_order, show, sym_of


def close_time(a):
    """--before-open = 다음 미국 정규장 시작(동부 9:30) / --until "MM-DD HH:MM" 또는 "HH:MM" (한국 시각)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    if a.before_open:
        from .bot import trading_day
        et = ZoneInfo("America/New_York")
        t = datetime.now(et)
        d = t.date() if t.hour * 60 + t.minute < 570 else t.date() + timedelta(days=1)
        while not trading_day(d):
            d += timedelta(days=1)
        return datetime(d.year, d.month, d.day, 9, 30, tzinfo=et)
    if a.until:
        kst = ZoneInfo("Asia/Seoul")
        now = datetime.now(kst)
        try:
            if " " in a.until.strip():
                md, hm = a.until.split()
                t = datetime(now.year, int(md.split("-")[0]), int(md.split("-")[1]), *map(int, hm.split(":")), tzinfo=kst)
            else:
                t = now.replace(hour=int(a.until.split(":")[0]), minute=int(a.until.split(":")[1]), second=0, microsecond=0)
                t = t if t > now else t + timedelta(days=1)
        except (ValueError, IndexError):
            raise ValueError('종료 시각은 "MM-DD HH:MM" 또는 "HH:MM" (한국 시각)으로 적어 주세요')
        return t
    return None


def manual_main(a) -> None:
    load_dotenv(a.env)
    state_dir = os.environ.get("BN_STATE_DIR", "state_binance")
    equity = float(os.environ.get("BN_DRY_EQUITY", "1000") or 1000)
    store = Store(state_dir)
    client = BinanceClient()      # 키 없이 공개 시세만 사용 → 실제 주문을 보낼 수 없음

    if a.action == "add":
        if not a.target or not a.side:
            print("종목과 방향이 필요해요. 예: python -m binancebot manual add KORU --side short --ma 1h vwma 100 --stop-pct 3 --tp-r 2")
            return
        if a.ma:
            entry = {"type": "ma", "tf": a.ma[0], "kind": a.ma[1].lower(), "len": int(a.ma[2])}
        elif a.high and a.low:
            entry = {"type": "mid", "high": a.high, "low": a.low, "ratio": a.ratio, "auto": not a.fixed}
        elif a.price:
            entry = {"type": "price", "price": a.price}
        else:
            print("진입 위치가 필요해요: --price 가격 / --ma 봉 종류 기간 / --high 고점 --low 저점")
            return
        if a.stop_ma:
            stop = {"type": "ma_close", "tf": a.stop_ma[0], "kind": a.stop_ma[1].lower(), "len": int(a.stop_ma[2])}
        elif a.stop:
            stop = {"type": "price", "price": a.stop}
        elif a.stop_pct:
            stop = {"type": "pct", "pct": a.stop_pct}
        elif a.stop_anchor:
            stop = {"type": "anchor"}
        else:
            print("손절이 꼭 있어야 해요: --stop 가격 / --stop-pct % / --stop-anchor / --stop-ma 봉 종류 기간")
            return
        tp = {"type": "ma", "tf": a.tp_ma[0], "kind": a.tp_ma[1].lower(), "len": int(a.tp_ma[2])} if a.tp_ma else {"type": "price", "price": a.tp} if a.tp else {"type": "r", "r": a.tp_r} if a.tp_r else {"type": "far"} if a.tp_far else None
        try:
            sym = sym_of(a.target)
            p = client.prices().get(sym)
            if p is None:
                print(f"{sym} 은 바이낸스 선물에 없는 종목이에요.")
                return
            o = store.add(new_order(a.target, a.side, entry, stop, tp, a.lev, a.hours, close_at=close_time(a),
                                    void_ma={"tf": a.void_ma[0], "kind": a.void_ma[1].lower(), "len": int(a.void_ma[2]),
                                             "ref": "level" if a.void_vs_entry else "price"} if a.void_ma else None))
        except ValueError as exc:
            print(f"등록 안 됨: {exc}")
            return
        print(f"등록했어요 (테스트 매매, 실제 주문 없음) [{o['id']}] {describe(o)}")
        print(f"지금 가격 {p:g}. 감시 프로그램(windows\\binance_manual.bat)이 켜져 있어야 진입·청산이 기록돼요.")
        return
    if a.action == "list":
        try:
            prices = client.prices()
        except Exception:  # noqa: BLE001
            prices = {}
        show(state_dir, prices, a.all)
        return
    if a.action == "cancel":
        if not a.target:
            print("취소할 주문 번호가 필요해요 (list 로 확인).")
            return
        print("취소 요청을 남겼어요. 감시 프로그램이 다음 확인 때 처리해요 (보유 중이면 그때 가격으로 정리)." if store.cancel(a.target)
              else "그 번호의 대기·보유 주문이 없어요.")
        return
    # run
    from kiwoombot.__main__ import acquire_lock

    store.dir.mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(str(store.dir))
    if lock is None:
        print("조건 주문 감시가 이미 켜져 있어요. 이 창은 닫아도 됩니다.")
        return
    fmt = logging.Formatter("%(asctime)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(), logging.FileHandler(Path(store.dir) / "manual.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    client.sync_time()
    ManualTrader(client, state_dir, equity).run()
