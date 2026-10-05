"""키움 단타 봇 실행.

    python -m kiwoombot check        # 토큰·잔고·등락률 순위·분봉 조회가 되는지만 확인 (주문 없음)
    python -m kiwoombot candidates   # 오늘(장 전) 대상 종목만 고르고 끝
    python -m kiwoombot reserve      # 적립금·운용금 보기 (--set 금액 으로 적립금 직접 고치기)
    python -m kiwoombot update-settings  # .env.kiwoom 전략 값을 최신 추천으로 (키·모의/드라이런 유지)
    python -m kiwoombot run          # 하루 매매 (08:45 ETF 매도 → 08:50 대상 고르기 → 09:00~ 매수 → exit_time 정리 → 15:21 ETF 매수)
    python -m kiwoombot loop         # 켜 둔 채로 거래일마다 run 반복 (주말·저녁에 켜도 다음 평일 08:40 까지 기다림)

설정은 .env.kiwoom (KIWOOM_APP_KEY, KIWOOM_SECRET_KEY, KIWOOM_MOCK, KIWOOM_DRY_RUN, KW_* 전략 값).
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from tossbot.config import KST, load_dotenv

from .client import KiwoomClient, num
from .config import KiwoomConfig
from .daytrade import DayTrader
from .reserve import ReserveBook


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
    try:
        print(f"주문가능금액: {client.orderable_cash():,.0f}원")
    except Exception as exc:  # noqa: BLE001
        print(f"주문가능금액 조회 실패: {exc}")
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


def show_reserve(client: KiwoomClient, cfg: KiwoomConfig) -> None:
    book = ReserveBook(cfg.state_dir, cfg.reserve_pct, cfg.reserve_floor)
    if not book.enabled:
        print("적립금: 끔 (KW_RESERVE_PCT=0)")
        return
    st = book.load()
    equity = num(client.balance().get("prsm_dpst_aset_amt"))
    print(f"적립금: {st.reserve:,.0f}원 / 운용금(주문 기준): {book.operating(equity):,.0f}원 / 평가금액: {equity:,.0f}원 "
          f"(수익의 {cfg.reserve_pct:g}% 적립, 운용금 {cfg.reserve_floor:,.0f}원 아래면 적립금에서 보충)")
    for h in st.history[-5:]:
        print("  " + ", ".join(f"{k}={v:,}" if isinstance(v, (int, float)) else f"{k}={v}" for k, v in h.items()))


def acquire_lock(state_dir: str):
    """봇이 두 번 켜지지 않게 잠금 파일을 잡는다. 이미 다른 창이 잡고 있으면 None.
    돌려준 파일을 들고 있는 동안만 잠기고, 창이 닫히거나 PC 가 꺼지면 저절로 풀린다."""
    path = Path(state_dir) / "bot.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


LOOP_START = (8, 40)  # loop: 거래일마다 이 시각에 run 시작 (08:45 ETF 매도 전)
LOOP_END = "15:30"  # 이 시각이 지났으면 다음 평일로
LOOP_RETRIES = 20  # 하루 안에서 오류가 나면 30초 뒤 이어서 다시 (상태 파일로 이어짐)


def next_start(now: datetime, last_run: str = "") -> datetime:
    """loop 가 다음에 run 을 시작할 시각. 평일 08:40~15:30 사이이고 오늘(last_run) 아직 안 돌렸으면 지금."""
    d = now
    for _ in range(8):
        if d.weekday() < 5 and d.strftime("%Y%m%d") != last_run:
            start = d.replace(hour=LOOP_START[0], minute=LOOP_START[1], second=0, microsecond=0)
            if d.date() != now.date() or now < start:
                return start
            if now.strftime("%H:%M") < LOOP_END:
                return now
        d = (d + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise RuntimeError("unreachable")


def loop(cfg: KiwoomConfig, now_fn=None, sleep_fn=time.sleep) -> None:
    """창을 켜 둔 채로 거래일마다 run 을 되풀이한다 (주말·장 마감 뒤에 켜도 다음 평일 아침까지 기다림)."""
    log = logging.getLogger("kiwoombot")
    now_fn = now_fn or (lambda: datetime.now(KST))
    last_run = ""
    while True:
        start = next_start(now_fn(), last_run)
        if start > now_fn():
            log.info("다음 실행 %s 까지 대기 (이 창을 닫지 마세요)", start.strftime("%m-%d %H:%M"))
            while now_fn() < start:
                sleep_fn(min(60.0, max(1.0, (start - now_fn()).total_seconds())))
        last_run = now_fn().strftime("%Y%m%d")
        for attempt in range(LOOP_RETRIES + 1):
            try:
                DayTrader(KiwoomClient(cfg.app_key, cfg.secret_key, mock=cfg.mock), cfg).run()
                break
            except Exception:  # noqa: BLE001
                log.exception("실행 중 오류 (%d/%d)", attempt + 1, LOOP_RETRIES)
                if attempt >= LOOP_RETRIES or now_fn().strftime("%H:%M") >= LOOP_END:
                    log.error("오늘은 더 시도하지 않음 — 보유 종목·미체결 주문을 직접 확인하세요")
                    break
                sleep_fn(30)


KEEP_KEYS = ("KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY", "KIWOOM_MOCK", "KIWOOM_DRY_RUN", "KW_SWING_STATE_FILE", "KW_STATE_DIR")
SECRET_KEYS = ("KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY")


def _read_env(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def update_settings(env_path: Path, example_path: Path, keep: tuple = KEEP_KEYS) -> list[tuple[str, str, str]]:
    """예시 파일(최신 추천 전략)로 .env.kiwoom 을 다시 쓴다. 키·모의투자·드라이런·경로는 기존 값 유지.
    기존 파일은 .bak 으로 남긴다. (바뀐 항목, 이전 값, 새 값) 목록을 돌려줌 (키 값은 숨김)."""
    old = _read_env(env_path)
    lines = []
    changes = []
    for line in example_path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in keep and k in old:
                v = old[k]
            elif old.get(k, None) != v:
                changes.append((k, old.get(k, "(없음)"), v))
            line = f"{k}={v}"
        lines.append(line)
    for k in old:  # 예시에 없는 사용자 항목은 끝에 그대로
        if k not in {ln.split("=", 1)[0].strip() for ln in lines if "=" in ln and not ln.lstrip().startswith("#")}:
            lines.append(f"{k}={old[k]}")
    if env_path.exists():
        env_path.with_name(env_path.name + ".bak").write_text(env_path.read_text(encoding="utf-8-sig"), encoding="utf-8")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changes


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kiwoombot", description="키움 단타 봇 (전일 +20%% 종목 전일 종가 재돌파 + ETF 오버나이트)")
    p.add_argument("command", choices=["check", "candidates", "run", "loop", "reserve", "update-settings"])
    p.add_argument("--env", default=".env.kiwoom")
    p.add_argument("--set", type=float, default=None, help="reserve: 적립금을 이 금액(원)으로 고침")
    args = p.parse_args(argv)
    if args.command == "update-settings":
        env_path = Path(args.env)
        changes = update_settings(env_path, Path(".env.kiwoom.example"))
        print(f"{env_path} 를 최신 추천 설정으로 바꿨어요 (이전 파일: {env_path.name}.bak, 키·모의투자·드라이런 값은 그대로).")
        for k, before, after in changes:
            print(f"  {k}: {before} → {after}")
        if not changes:
            print("  바뀐 항목 없음 (이미 최신)")
        return
    load_dotenv(args.env)
    cfg = KiwoomConfig.from_env()
    setup_logging(cfg.state_dir)
    if not cfg.app_key or not cfg.secret_key:
        print(f"키움 API 키가 비어 있어요. 메모장으로 {args.env} 를 열어 KIWOOM_APP_KEY=, KIWOOM_SECRET_KEY= 뒤에 "
              f"키움에서 받은 {'모의투자' if cfg.mock else '실전'} 키를 붙여 넣고 저장한 뒤 다시 실행하세요.")
        return
    client = KiwoomClient(cfg.app_key, cfg.secret_key, mock=cfg.mock)
    if args.command == "check":
        c = cfg
        print(f"설정: 전일 +{c.min_prev_change:g}%↑, {c.buy_until} 까지 매수, 하루 {c.max_positions}종목 x {c.position_pct:g}%, "
              f"1주 {c.max_price:,.0f}원 이하, 손절 -{c.stop_pct:g}% / 익절 +{c.take_profit_pct:g}% / {c.exit_time} 정리, "
              + (f"ETF {c.etf_code} {c.etf_pct:g}% 오버나이트" if c.etf_enabled else "ETF 끔")
              + f" ({'모의투자' if c.mock else '실전'}{', 드라이런' if c.dry_run else ', 실제 주문'})")
        check(client)
        show_reserve(client, cfg)
        return
    if args.command == "reserve":
        if args.set is not None:
            book = ReserveBook(cfg.state_dir, cfg.reserve_pct, cfg.reserve_floor)
            before = book.load().reserve
            print(f"적립금을 {before:,.0f}원 → {book.set_reserve(args.set).reserve:,.0f}원으로 고쳤어요.")
        show_reserve(client, cfg)
        return
    trader = DayTrader(client, cfg)
    if args.command == "candidates":
        trader.prepare()
        return
    lock = acquire_lock(cfg.state_dir)  # run·loop 는 주문을 내므로 한 번에 하나만
    if lock is None:
        print("키움 단타 봇이 이미 켜져 있어요. 먼저 켠 창이 그대로 돌고 있으니 이 창은 닫아도 됩니다.")
        return
    if args.command == "loop":
        loop(cfg)
        return
    trader.run()


if __name__ == "__main__":
    main()
