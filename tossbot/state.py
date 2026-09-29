"""봇이 직접 매수한 포지션만 추적한다 (사용자가 수동으로 보유한 종목은 건드리지 않음)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Position:
    symbol: str
    name: str
    quantity: int = 0  # 체결된 보유 수량
    entry_price: float = 0.0  # 평균 체결가
    opened_at: str = ""
    buy_order_id: str | None = None
    buy_open: bool = False  # 매수 주문이 아직 진행 중인지
    status: str = "OPEN"  # OPEN | SELLING
    sell_order_id: str | None = None
    sell_reason: str | None = None
    # 증권사 서버에 걸어둔 조건주문 (SINGLE) ID
    stop_co_id: str | None = None  # 손절: 감시가 도달 시 시장가 매도
    tp_co_id: str | None = None  # 익절: 감시가 도달 시 지정가 매도
    stop_arm_failures: int = 0
    tp_arm_failures: int = 0
    # 보유 거래일 수 (매수일 = 0). 거래일이 바뀔 때마다 1씩 증가
    hold_days: int = 0
    last_day: str = ""
    kind: str = "breakout"  # 어느 전략으로 샀는지: breakout | rsi (매도 규칙이 다름)


@dataclass
class State:
    # 감시 목록에 올랐던 급등 종목 {symbol: 마지막으로 확인한 날}. 랭킹에서 빠져도 계속 추적하기 위함
    surge_seen: dict[str, str] = field(default_factory=dict)
    last_buy_date: str | None = None  # 신고가 전략: 마지막으로 매수를 실행한 날
    last_exit_date: str | None = None  # combo: RSI 매도 판정을 마친 날
    positions: dict[str, Position] = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return State(
            surge_seen=raw.get("surge_seen", {}),
            last_buy_date=raw.get("last_buy_date"),
            last_exit_date=raw.get("last_exit_date"),
            positions={k: Position(**v) for k, v in raw.get("positions", {}).items()},
            history=raw.get("history", []),
        )

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {
            "surge_seen": state.surge_seen,
            "last_buy_date": state.last_buy_date,
            "last_exit_date": state.last_exit_date,
            "positions": {k: asdict(v) for k, v in state.positions.items()},
            "history": state.history,
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
