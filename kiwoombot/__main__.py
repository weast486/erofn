"""키움 단타 봇 실행.

    python -m kiwoombot check        # 토큰·잔고·등락률 순위·분봉 조회가 되는지만 확인 (주문 없음)
    python -m kiwoombot candidates   # 오늘(장 전) 대상 종목만 고르고 끝
    python -m kiwoombot update-settings  # .env.kiwoom 전략 값을 최신 추천으로 (키·모의/드라이런 유지)
    python -m kiwoombot run          # 하루 매매 (08:45 ETF 매도 → 08:50 대상 고르기 → 09:00~ 매수 → exit_time 정리 → 15:21 ETF 매수)

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


def update_settings(env_path: Path, example_path: Path) -> list[tuple[str, str, str]]:
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
            if k in KEEP_KEYS and k in old:
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
    p.add_argument("command", choices=["check", "candidates", "run", "update-settings"])
    p.add_argument("--env", default=".env.kiwoom")
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
    client = KiwoomClient(cfg.app_key, cfg.secret_key, mock=cfg.mock)
    if args.command == "check":
        c = cfg
        print(f"설정: 전일 +{c.min_prev_change:g}%↑, {c.buy_until} 까지 매수, 하루 {c.max_positions}종목 x {c.position_pct:g}%, "
              f"1주 {c.max_price:,.0f}원 이하, 손절 -{c.stop_pct:g}% / 익절 +{c.take_profit_pct:g}% / {c.exit_time} 정리, "
              + (f"ETF {c.etf_code} {c.etf_pct:g}% 오버나이트" if c.etf_enabled else "ETF 끔")
              + f" ({'모의투자' if c.mock else '실전'}{', 드라이런' if c.dry_run else ', 실제 주문'})")
        check(client)
        return
    trader = DayTrader(client, cfg)
    if args.command == "candidates":
        trader.prepare()
        return
    trader.run()


if __name__ == "__main__":
    main()
