"""손절 금액 기준 진입 수량 정하기 (백테스트·봇 공용).

수량 = (평가금액 x 위험%) / (1개당 손절 폭 + 1개당 예상 비용)
단 포지션 금액 합계는 평가금액 x 최대 레버리지 이하, 수량은 거래소 단위(stepSize)로 내림.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizeRule:
    risk_pct: float = 2.0         # 거래 1건 손절 시 잃는 돈 = 평가금액의 몇 %
    max_leverage: float = 5.0     # 열린 포지션 금액 합계 / 평가금액 상한
    fee_pct: float = 0.05         # 한쪽 수수료 % (테이커)
    slippage_pct: float = 0.05    # 손절(시장가) 한쪽 슬리피지 %


def position_size(equity: float, entry: float, stop: float, rule: SizeRule,
                  open_notional: float = 0.0, step: float = 0.0, min_notional: float = 0.0) -> float:
    """진입 수량. 조건이 안 맞으면 0."""
    dist = abs(entry - stop)
    if equity <= 0 or entry <= 0 or dist <= 0:
        return 0.0
    cost = entry * (2 * rule.fee_pct + rule.slippage_pct) / 100  # 진입·청산 수수료 + 손절 슬리피지
    qty = equity * rule.risk_pct / 100 / (dist + cost)
    room = equity * rule.max_leverage - open_notional
    qty = min(qty, max(room, 0.0) / entry)
    if step > 0:
        qty = math.floor(qty / step + 1e-9) * step
    if qty <= 0 or qty * entry < min_notional:
        return 0.0
    return qty
