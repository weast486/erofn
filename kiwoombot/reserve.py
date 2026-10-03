"""수익 적립금: 수익 난 날 수익금의 일부를 주문 금액에서 빼 둔다 (같은 계좌 안의 장부상 구분, 실제 이체 없음).

  운용금 = 평가금액 - 적립금  → 단타·ETF 주문 금액은 운용금 기준
  12시 정리 직후(settle)   하루 손익 = 지금 평가금액 - 기준 평가금액(전날 15:21). 수익이면 reserve_pct% 를 적립금으로
  15:21 ETF 매수 전(mark_flows)  12시 정리 뒤로 평가금액이 변했으면 입출금으로 본다 (그 사이엔 보유 종목이 없음).
                                 출금은 적립금에서 먼저 빼고, 입금은 운용금에 더한다. 이때 평가금액이 다음 날 손익 기준
  운용금이 reserve_floor 아래면 적립금에서 꺼내 reserve_floor 까지 채운다.
드라이런은 계산해서 로그만 남기고 파일(state_kiwoom/reserve.json)은 바꾸지 않는다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("kiwoombot")

FLOW_NOISE = 1000  # 원. 12시 정리 뒤 평가금액 변화가 이보다 작으면 입출금으로 보지 않음
HISTORY_MAX = 400


@dataclass
class ReserveState:
    reserve: float = 0.0  # 적립금
    base_equity: float = 0.0  # 하루 손익을 재는 기준 평가금액 (0 = 아직 없음)
    base_date: str = ""
    settle_equity: float = 0.0  # 12시 정리 직후 평가금액 (입출금 판단 기준)
    settled_date: str = ""
    history: list = field(default_factory=list)


class ReserveBook:
    def __init__(self, state_dir: str, pct: float, floor: float, dry_run: bool = False):
        self.path = Path(state_dir) / "reserve.json"
        self.pct = pct
        self.floor = floor
        self.dry_run = dry_run
        self._mem: ReserveState | None = None  # 드라이런은 파일 대신 여기에만

    @property
    def enabled(self) -> bool:
        return self.pct > 0

    # ------------------------------------------------------------------- io
    def load(self) -> ReserveState:
        if self._mem is not None:
            return self._mem
        if not self.path.exists():
            return ReserveState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return ReserveState(**{k: raw[k] for k in ReserveState.__dataclass_fields__ if k in raw})

    def save(self, st: ReserveState) -> None:
        st.reserve = float(round(max(0.0, st.reserve)))
        st.history = st.history[-HISTORY_MAX:]
        if self.dry_run:
            self._mem = st
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(st), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---------------------------------------------------------------- rules
    def operating(self, equity: float) -> float:
        """주문 금액 기준이 되는 운용금. 적립금을 빼되, floor 아래로는 적립금에서 채워 준 것으로 본다."""
        if not self.enabled:
            return equity
        return max(equity - self.load().reserve, min(equity, self.floor))

    def _top_up(self, st: ReserveState, equity: float) -> float:
        st.reserve = max(0.0, min(st.reserve, equity))
        short = self.floor - (equity - st.reserve)
        take = min(st.reserve, short) if short > 0 else 0.0
        st.reserve -= take
        return take

    def ensure_base(self, equity: float, ymd: str) -> None:
        """손익 기준이 아직 없으면(첫날) 지금 평가금액을 기준으로 삼는다."""
        if not self.enabled or equity <= 0:
            return
        st = self.load()
        if st.base_equity <= 0:
            st.base_equity, st.base_date = equity, ymd
            self.save(st)

    def settle(self, equity: float, ymd: str) -> ReserveState:
        """12시 정리 직후: 하루 손익을 재고 수익이면 일부를 적립. 하루 한 번만."""
        st = self.load()
        if not self.enabled or equity <= 0 or st.settled_date == ymd:
            return st
        pnl = equity - st.base_equity if st.base_equity > 0 else 0.0
        added = pnl * self.pct / 100 if pnl > 0 else 0.0
        st.reserve += added
        topup = self._top_up(st, equity)
        st.settle_equity, st.settled_date = equity, ymd
        st.base_equity, st.base_date = equity, ymd
        st.history.append({"date": ymd, "kind": "settle", "equity": equity, "pnl": round(pnl), "added": round(added),
                           "topup": round(topup), "reserve": round(st.reserve)})
        self.save(st)
        log.info(f"적립금 정산{' (드라이런: 계산만)' if self.dry_run else ''}: 하루 손익 {pnl:+,.0f}원 → 적립 {added:+,.0f}원"
                 + (f", 운용금 보충 {topup:,.0f}원" if topup else "")
                 + f" | 평가금액 {equity:,.0f} = 운용금 {equity - st.reserve:,.0f} + 적립금 {st.reserve:,.0f}")
        return st

    def mark_flows(self, equity: float, ymd: str) -> ReserveState:
        """15:21 ETF 매수 전: 12시 정리 뒤의 평가금액 변화를 입출금으로 처리하고 내일 손익 기준을 잡는다."""
        st = self.load()
        if not self.enabled or equity <= 0:
            return st
        if st.settled_date != ymd:  # 오늘 정산을 못 했으면 지금 한다 (입출금을 가려낼 수 없음)
            return self.settle(equity, ymd)
        flow = equity - st.settle_equity
        if abs(flow) < FLOW_NOISE:
            flow = 0.0
        used = min(st.reserve, -flow) if flow < 0 else 0.0  # 출금은 적립금에서 먼저
        st.reserve -= used
        topup = self._top_up(st, equity)
        st.base_equity, st.base_date = equity, ymd
        st.settle_equity = equity
        if flow or topup:
            st.history.append({"date": ymd, "kind": "flow", "equity": equity, "flow": round(flow), "from_reserve": round(used),
                               "topup": round(topup), "reserve": round(st.reserve)})
            log.info(f"입출금 반영{' (드라이런: 계산만)' if self.dry_run else ''}: 12시 이후 {flow:+,.0f}원"
                     + (f" (적립금에서 {used:,.0f}원 차감)" if used else "")
                     + (f", 운용금 보충 {topup:,.0f}원" if topup else "")
                     + f" | 평가금액 {equity:,.0f} = 운용금 {equity - st.reserve:,.0f} + 적립금 {st.reserve:,.0f}")
        self.save(st)
        return st

    def set_reserve(self, amount: float) -> ReserveState:
        """적립금을 직접 고친다 (정해진 시간 밖에 입출금했을 때 등)."""
        st = self.load()
        before = st.reserve
        st.reserve = max(0.0, amount)
        st.history.append({"kind": "manual", "before": round(before), "reserve": round(st.reserve)})
        self.save(st)
        return st
