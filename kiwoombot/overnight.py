"""ETF 오버나이트 (백테스트 `etfnight`): 오늘 ETF 가 전일 종가보다 내렸으면 장 마감 동시호가에 사서
다음 날 장 시작 동시호가에 판다. 단타(9:00~12:00)와 보유 시간이 겹치지 않아 같은 돈을 쓴다.

  etf_sell_time(08:45)  보유 중인 ETF 를 시장가 매도 주문 → 9:00 시가에 체결
  etf_buy_time(15:21)   오늘 등락률 < etf_max_change 면 평가금액 etf_pct% 만큼 시장가 매수 → 15:30 종가에 체결

주문 수량은 키움 주문가능금액 안에서만 정해 돈이 모자라 주문이 거부되거나 미수가 생기지 않게 한다.
"""
from __future__ import annotations

import logging
from datetime import datetime

from .client import KiwoomClient, num
from .config import KiwoomConfig

log = logging.getLogger("kiwoombot")


class EtfOvernight:
    def __init__(self, client: KiwoomClient, cfg: KiwoomConfig):
        self.client = client
        self.cfg = cfg

    def orderable(self) -> float | None:
        try:
            return self.client.orderable_cash()
        except Exception as exc:  # noqa: BLE001
            log.warning("주문가능금액 조회 실패: %s", exc)
            return None

    def morning_sell(self) -> int:
        """보유 중인 ETF 전량 시장가 매도 주문. 주문한 수량을 돌려줌."""
        code = self.cfg.etf_code
        try:
            qty = int((self.client.holdings().get(code) or {}).get("qty", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("ETF 보유 조회 실패: %s", exc)
            return 0
        if qty <= 0:
            log.info("ETF %s 보유 없음 (어제 매수 안 함)", code)
            return 0
        log.info(f"ETF {code} {qty}주 장 시작 동시호가 시장가 매도 주문{' (드라이런)' if self.cfg.dry_run else ''}, "
                 f"주문가능금액(매도 전) {self.orderable() or 0:,.0f}원")
        if not self.cfg.dry_run:
            try:
                self.client.sell(code, qty)
            except Exception as exc:  # noqa: BLE001
                log.warning("ETF 매도 주문 실패: %s", exc)
                return 0
        return qty

    def log_cash_after_open(self) -> None:
        """9시 직후 주문가능금액을 남겨 매도 대금이 바로 반영되는지 확인한다."""
        log.info(f"주문가능금액(9시 직후) {self.orderable() or 0:,.0f}원")

    def today_change(self, ymd: str) -> tuple[float, float] | None:
        """(현재가, 오늘 등락률 %). 오늘 일봉이 없으면(휴장 등) None."""
        rows = self.client.daily_chart(self.cfg.etf_code, ymd)
        if len(rows) < 2 or str(rows[0].get("dt", "")) != ymd:
            return None
        cur, prev = num(rows[0].get("cur_prc")), num(rows[1].get("cur_prc"))
        if cur <= 0 or prev <= 0:
            return None
        return cur, (cur / prev - 1) * 100

    def evening_buy(self, now: datetime, equity: float) -> int:
        """오늘 내렸으면 평가금액 etf_pct% 만큼 시장가 매수 주문. 주문 수량을 돌려줌."""
        cfg = self.cfg
        ymd = now.strftime("%Y%m%d")
        try:
            got = self.today_change(ymd)
        except Exception as exc:  # noqa: BLE001
            log.warning("ETF 시세 조회 실패: %s", exc)
            return 0
        if got is None:
            log.info("ETF 오늘 시세 없음(휴장?) → 매수 안 함")
            return 0
        price, chg = got
        if chg >= cfg.etf_max_change:
            log.info(f"ETF {cfg.etf_code} 오늘 {chg:+.2f}% (기준 {cfg.etf_max_change:+.1f}% 미만 아님) → 매수 안 함")
            return 0
        try:
            held = int((self.client.holdings().get(cfg.etf_code) or {}).get("qty", 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("ETF 보유 조회 실패: %s", exc)
            return 0
        if held:
            log.info("ETF 이미 %d주 보유 → 추가 매수 안 함", held)
            return 0
        budget = equity * cfg.etf_pct / 100
        cash = None if cfg.dry_run else self.orderable()
        if cash is not None:
            budget = min(budget, cash * 0.98)  # 시장가는 상한가 기준으로 증거금을 잡을 수 있어 여유를 둠
        qty = int(budget // (price * 1.01))
        if qty < 1:
            log.info(f"ETF 매수 금액 부족 (예산 {budget:,.0f}원)")
            return 0
        log.info(f"ETF {cfg.etf_code} 오늘 {chg:+.2f}% → {qty}주 장 마감 동시호가 시장가 매수 주문 "
                 f"(현재가 {price:,.0f}, 예산 {budget:,.0f}원{', 드라이런' if cfg.dry_run else ''})")
        if not cfg.dry_run:
            try:
                self.client.buy(cfg.etf_code, qty)
            except Exception as exc:  # noqa: BLE001
                log.warning("ETF 매수 주문 실패: %s", exc)
                return 0
        return qty
