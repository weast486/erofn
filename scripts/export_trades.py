"""봇 매매 현황을 JSON 한 파일로 내보낸다 (매매 결과 화면에 끌어다 놓는 용도).

    python scripts/export_trades.py            # → state/매매현황.json
    python scripts/export_trades.py --no-price # 현재가 조회 없이 (API 호출 없음)

읽기만 한다: state/state.json(또는 DRY_RUN 이면 state.dry.json) + 보유 종목 현재가 조회. 주문·상태 변경 없음.
API 키·계좌번호는 파일에 넣지 않는다.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tossbot.config import KST, Config, load_dotenv  # noqa: E402
from tossbot.state import StateStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default=".env")
    ap.add_argument("--out", type=Path, default=Path("state/매매현황.json"))
    ap.add_argument("--no-price", action="store_true", help="현재가 조회 안 함 (보유 종목은 매수가로 평가)")
    args = ap.parse_args()

    load_dotenv(args.env)
    cfg = Config.from_env()
    state = StateStore(cfg.state_file).load()
    held = [p for p in state.positions.values() if p.quantity > 0]

    prices: dict[str, float] = {}
    price_error = None
    if held and not args.no_price:
        try:
            from tossbot.client import TossClient

            client = TossClient(cfg.client_id, cfg.client_secret, cfg.base_url, cfg.account_seq)
            prices = {r["symbol"]: float(r["lastPrice"]) for r in client.get_prices([p.symbol for p in held])}
        except Exception as exc:  # 조회 실패해도 내보내기는 계속 (매수가로 평가)
            price_error = str(exc)[:200]

    keep = ("symbol", "name", "kind", "quantity", "entry_price", "opened_at", "hold_days", "status")
    data = {
        "format": "tossbot-trades/1",
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "mode": "DRY_RUN" if cfg.dry_run else "LIVE",
        "strategy": cfg.strategy,
        "total_budget": cfg.total_budget,
        "num_stocks": cfg.num_stocks,
        "stop_loss_pct": cfg.stop_loss_pct,
        "take_profit_pct": cfg.take_profit_pct,
        "positions": [{**{k: v for k, v in asdict(p).items() if k in keep}, "last_price": prices.get(p.symbol)}
                      for p in held],
        "history": state.history,
        "price_error": price_error,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    realized = sum((h["exit_price"] - h["entry_price"]) * h["quantity"] for h in state.history
                   if h.get("exit_price") and h.get("entry_price") and h.get("quantity"))
    print(f"저장: {args.out.resolve()}")
    print(f"청산 {len(state.history)}건, 실현손익 {realized:+,.0f}원, 보유 {len(held)}종목"
          + (f" (현재가 조회 실패: {price_error})" if price_error else ""))


if __name__ == "__main__":
    main()
