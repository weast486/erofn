"""ETF 오버나이트 (백테스트 `etfnight`): 오늘 ETF 가 전일 종가보다 내렸으면 장 마감 동시호가에 사서
다음 날 장 시작 동시호가에 판다. 단타(9:00~12:00)와 보유 시간이 겹치지 않아 같은 돈을 쓴다.

  etf_sell_time(08:45)  보유 중인 ETF 를 시장가 매도 주문 → 9:00 시가에 체결
  etf_buy_time(15:21)   오늘 등락률 < etf_max_change 면 평가금액 etf_pct% 만큼 시장가 매수 → 15:30 종가에 체결

주문 수량은 키움 주문가능금액 안에서만 정해 돈이 모자라 주문이 거부되거나 미수가 생기지 않게 한다.
매매 내역은 state_dir/etf_trades.json 에 남긴다 (현황 페이지용): 주문할 때 한 줄을 만들고, settle() 이 나중에
매수가(매수일 종가 = 장 마감 동시호가 체결가, 보유 중이면 계좌의 평균 매입가)와 매도가(매도일 시가)를 채운다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from .client import KiwoomClient, num
from .config import KiwoomConfig

log = logging.getLogger("kiwoombot")


class EtfOvernight:
    def __init__(self, client: KiwoomClient, cfg: KiwoomConfig):
        self.client = client
        self.cfg = cfg

    # ------------------------------------------------------------ 매매 내역
    @property
    def ledger(self) -> Path:
        return Path(self.cfg.state_dir) / "etf_trades.json"

    def load_trades(self) -> list[dict]:
        try:
            return json.loads(self.ledger.read_text(encoding="utf-8")) if self.ledger.exists() else []
        except ValueError:
            log.warning("etf_trades.json 을 읽지 못해 새로 시작")
            return []

    def save_trades(self, rows: list[dict]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.ledger.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.ledger)

    def _record_buy(self, ymd: str, qty: int, price: float) -> None:
        rows = self.load_trades()
        rows.append({"code": self.cfg.etf_code, "buy_day": ymd, "qty": qty, "buy_price": 0.0, "order_price": price,
                     "sell_day": "", "sell_price": 0.0, "pnl": 0.0, "ret_pct": 0.0, "status": "ordered"})
        self.save_trades(rows)

    def _record_sell(self, ymd: str, qty: int, avg_price: float) -> None:
        rows = self.load_trades()
        row = next((r for r in reversed(rows) if r.get("code") == self.cfg.etf_code and not r.get("sell_day")
                    and r.get("status") != "unfilled"), None)
        if row is None:  # 기록 없이 들고 있던 것 (기록 기능 전에 샀거나 파일이 없어짐)
            row = {"code": self.cfg.etf_code, "buy_day": "", "qty": qty, "buy_price": avg_price, "order_price": 0.0,
                   "sell_day": "", "sell_price": 0.0, "pnl": 0.0, "ret_pct": 0.0, "status": "held"}
            rows.append(row)
        if not row.get("buy_price") and avg_price:
            row["buy_price"] = avg_price
        row.update(qty=qty, sell_day=ymd, status="selling")
        self.save_trades(rows)

    def settle(self, now: datetime) -> None:
        """가격이 비어 있는 기록을 채운다. 여러 번 불러도 됨 (실패하면 다음에 다시)."""
        if self.cfg.dry_run:
            return
        rows = self.load_trades()
        todo = [r for r in rows if (r.get("status") == "ordered") or (r.get("status") == "selling")]
        if not todo:
            return
        ymd, hm = now.strftime("%Y%m%d"), now.strftime("%H:%M")
        try:
            daily = {str(d.get("dt", "")): d for d in self.client.daily_chart(self.cfg.etf_code, ymd)}
        except Exception as exc:  # noqa: BLE001
            log.warning("ETF 내역 정리용 일봉 조회 실패: %s", exc)
            return
        changed = False
        for r in todo:
            if r["status"] == "ordered" and (ymd > r["buy_day"] or hm >= "15:31"):
                price = 0.0
                if ymd == r["buy_day"]:
                    try:
                        h = self.client.holdings().get(r["code"])
                    except Exception as exc:  # noqa: BLE001
                        log.warning("ETF 보유 조회 실패: %s", exc)
                        continue
                    if not h:
                        r["status"] = "unfilled"
                        changed = True
                        log.info("ETF 매수 주문이 체결되지 않음 → 내역에서 뺌")
                        continue
                    r["qty"], price = int(h["qty"]), float(h.get("avg_price") or 0)
                price = price or num((daily.get(r["buy_day"]) or {}).get("cur_prc"))
                if price:
                    r["buy_price"], r["status"] = price, "held"
                    changed = True
                    log.info(f"ETF 매수 체결 기록: {r['qty']}주 x {price:,.0f}원")
            if r["status"] == "selling" and (ymd > r["sell_day"] or hm >= "09:01"):
                price = num((daily.get(r["sell_day"]) or {}).get("open_pric"))
                if price and r.get("buy_price"):
                    r["sell_price"] = price
                    r["pnl"] = round((price - r["buy_price"]) * r["qty"], 0)
                    r["ret_pct"] = round((price / r["buy_price"] - 1) * 100, 3)
                    r["status"] = "closed"
                    changed = True
                    log.info(f"ETF 오버나이트 끝: {r['qty']}주 {r['buy_price']:,.0f} → {price:,.0f}원 "
                             f"({r['ret_pct']:+.2f}%, 비용 전 {r['pnl']:+,.0f}원)")
        if changed:
            self.save_trades(rows)

    def orderable(self) -> float | None:
        try:
            return self.client.orderable_cash()
        except Exception as exc:  # noqa: BLE001
            log.warning("주문가능금액 조회 실패: %s", exc)
            return None

    def morning_sell(self, now: datetime | None = None) -> int:
        """보유 중인 ETF 전량 시장가 매도 주문. 주문한 수량을 돌려줌."""
        code = self.cfg.etf_code
        try:
            held = self.client.holdings().get(code) or {}
            qty = int(held.get("qty", 0))
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
            self._record_sell((now or datetime.now()).strftime("%Y%m%d"), qty, float(held.get("avg_price") or 0))
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
                # 시장가 주문은 증권사가 상한가(+30%) 기준으로 증거금을 잡아 거부할 수 있음 → 그 기준에 맞는 수량으로 한 번 더
                safe = int(budget // (price * 1.30))
                if not 1 <= safe < qty:
                    log.warning("ETF 매수 주문 실패: %s", exc)
                    return 0
                log.warning(f"ETF {qty}주 매수 주문 거부({exc}) → 상한가 기준 증거금에 맞춰 {safe}주로 다시 주문")
                try:
                    self.client.buy(cfg.etf_code, safe)
                except Exception as exc2:  # noqa: BLE001
                    log.warning("ETF 매수 주문 실패: %s", exc2)
                    return 0
                qty = safe
            self._record_buy(ymd, qty, price)
        return qty
