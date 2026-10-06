"""전일 +20% 종목 다음 날 전일 종가 재돌파 단타 (백테스트 `surgebreak --mode prevclose` 를 실전으로).

하루 흐름
  08:50  대상 고르기: 전날 장 마감 뒤 저장해 둔 급등 목록(surge_날짜.json) + 등락률 상위(ka10027, 장 시작 전에는 비어 있음)
         → 일봉(ka10081)으로 전일 +min_prev_change%↑·직전 20일 평균 거래대금·1주 가격 확인, 토스 스윙 봇이 들고 있는 종목 제외
  09:00~buy_until  2초마다 1분봉(ka10080) 확인: 시가가 전일 종가 아래에서 시작했고 고가가 전일 종가를 넘으면 매수
         (현재가 + entry_slip_pct 지정가, 전일 종가 + max_chase_pct 를 넘으면 추격 안 함, fill_timeout_sec 뒤 잔량 취소)
  체결 뒤  익절 지정가(매수가 +take_profit_pct) 주문, 현재가가 손절가(매수가 -stop_pct) 이하면 익절 주문 취소 후 시장가 매도
  exit_time  남은 주문 취소, 보유 전량 시장가 매도
  (etf_enabled) 08:45 어제 산 ETF 매도 주문, 15:21 오늘 ETF 가 내렸으면 매수 주문 — overnight.py
  15:35  오늘 등락률 상위를 다음 거래일 대상 후보로 저장 (surge_날짜.json)
매수 수량은 키움 주문가능금액 안에서만 정한다.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from tossbot.broker import round_up_to_tick
from tossbot.config import KST

from .client import KiwoomClient, num, ord_key
from .config import KiwoomConfig
from .overnight import EtfOvernight
from .reserve import ReserveBook

log = logging.getLogger("kiwoombot")

SURGE_SAVE_TIME = "15:35"  # 장 마감 뒤 이 시각에 오늘 등락률 상위를 다음 거래일 후보로 저장


@dataclass
class Candidate:
    code: str
    name: str
    prev_close: float
    prev_change: float
    status: str = "watch"  # watch / bought / skip_gap / missed_chase / full / too_expensive / expired


@dataclass
class Trade:
    code: str
    name: str
    buy_order: str
    order_qty: int
    order_price: int
    ordered_at: str  # ISO
    status: str = "pending"  # pending / open / closing / closed / canceled
    qty: int = 0
    entry_price: float = 0.0
    stop: float = 0.0
    target: int = 0
    tp_order: str = ""
    exit_reason: str = ""
    exit_price: float = 0.0
    exit_at: str = ""


@dataclass
class DayState:
    date: str
    equity: float = 0.0
    candidates: list[Candidate] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)


def is_common_stock(code: str) -> bool:
    return len(code) == 6 and code.isalnum() and code.endswith("0")


def swing_symbols(path: str) -> set[str]:
    """토스 스윙 봇 state.json 의 보유 종목 (없거나 읽기 실패면 빈 집합)."""
    if not path or path.lower() == "none":
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("positions", {}).keys())
    except Exception as exc:  # noqa: BLE001
        log.warning("스윙 봇 상태 파일을 읽지 못함(%s): %s", path, exc)
        return set()


def today_bars(rows: list[dict], ymd: str) -> list[dict]:
    """ka10080 응답(최신순)에서 오늘 봉만 시간순으로. 각 봉 {t, open, high, low, close, volume}."""
    out = []
    for r in rows:
        tm = str(r.get("cntr_tm", ""))
        if not tm.startswith(ymd):
            continue
        out.append({"t": tm[8:12], "open": num(r.get("open_pric")), "high": num(r.get("high_pric")),
                    "low": num(r.get("low_pric")), "close": num(r.get("cur_prc")), "volume": num(r.get("trde_qty"))})
    out.sort(key=lambda b: b["t"])
    return out


class DayTrader:
    def __init__(self, client: KiwoomClient, cfg: KiwoomConfig, now_fn=None, sleep_fn=time.sleep):
        self.client = client
        self.cfg = cfg
        self.now_fn = now_fn or (lambda: datetime.now(KST))
        self.sleep_fn = sleep_fn
        self.state: DayState | None = None
        self.state_dir = Path(cfg.state_dir)
        self._sim_price: dict[str, float] = {}  # dry-run 체결 흉내용 마지막 가격
        self.reserve = ReserveBook(cfg.state_dir, cfg.reserve_pct, cfg.reserve_floor, cfg.dry_run)

    # ------------------------------------------------------------- state io
    def _state_path(self, ymd: str) -> Path:
        return self.state_dir / f"daytrade_{ymd}.json"

    def save(self) -> None:
        if not self.state:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        p = self._state_path(self.state.date)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)

    def load(self, ymd: str) -> bool:
        p = self._state_path(ymd)
        if not p.exists():
            return False
        raw = json.loads(p.read_text(encoding="utf-8"))
        self.state = DayState(date=raw["date"], equity=raw.get("equity", 0.0),
                              candidates=[Candidate(**c) for c in raw.get("candidates", [])],
                              trades=[Trade(**t) for t in raw.get("trades", [])])
        log.info("오늘 상태 이어서 실행: 대상 %d, 거래 %d", len(self.state.candidates), len(self.state.trades))
        return True

    # ------------------------------------------------------------ candidates
    def _surge_rows(self, ranks: list[dict]) -> list[dict]:
        """등락률 순위에서 급등 후보(보통주, 기준의 90% 이상 상승)만 {code, name, flu_rt} 로."""
        seen, out = set(), []
        for r in ranks:
            code = str(r.get("stk_cd", "")).split("_")[0].lstrip("A")
            if not is_common_stock(code) or code in seen:
                continue
            seen.add(code)
            if num(r.get("flu_rt")) < self.cfg.min_prev_change * 0.9:  # 여유 있게 거르고 일봉으로 확인
                continue
            out.append({"code": code, "name": str(r.get("stk_nm", "")), "flu_rt": num(r.get("flu_rt"))})
        return out

    def save_surge_list(self, ymd: str) -> int:
        """장 마감 뒤: 오늘 등락률 상위를 다음 거래일 대상 후보로 저장한다. 저장한 종목 수를 돌려줌.
        다음 날 장 시작 전에는 등락률 순위(ka10027)가 비어 있어서 전날 저장해 둔 이 목록으로 대상을 고른다."""
        try:
            ranks = self.client.change_rate_ranking()
        except Exception as exc:  # noqa: BLE001
            log.warning("내일 대상 후보 저장 실패(등락률 순위 조회): %s", exc)
            return 0
        if not ranks:
            log.warning("등락률 순위가 비어 있어 내일 대상 후보를 저장하지 못함")
            return 0
        rows = self._surge_rows(ranks)
        trade_day = ymd
        try:  # 휴장일에 돌면 순위는 마지막 거래일 것 → 그 날짜로 저장
            code = rows[0]["code"] if rows else str(ranks[0].get("stk_cd", "")).split("_")[0].lstrip("A")
            trade_day = str(self.client.daily_chart(code, ymd)[0].get("dt", "")) or ymd
        except Exception as exc:  # noqa: BLE001
            log.warning("마지막 거래일 확인 실패(오늘 날짜로 저장): %s", exc)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        p = self.state_dir / f"surge_{trade_day}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"date": trade_day, "saved_at": self.now_fn().isoformat(), "rows": rows},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
        log.info("다음 거래일 대상 후보 %d종목 저장 (%s 등락률 +%g%%↑)", len(rows), trade_day,
                 self.cfg.min_prev_change * 0.9)
        return len(rows)

    def load_surge_list(self, ymd: str) -> tuple[str, list[dict]]:
        """오늘(ymd) 이전에 저장한 가장 최근 급등 목록. (날짜, 목록), 없으면 ("", [])."""
        for p in sorted(self.state_dir.glob("surge_*.json"), reverse=True):
            day = p.stem.split("_", 1)[1]
            if not (len(day) == 8 and day.isdigit() and day < ymd):
                continue
            try:
                return day, list(json.loads(p.read_text(encoding="utf-8")).get("rows", []))
            except Exception as exc:  # noqa: BLE001
                log.warning("저장된 급등 목록을 읽지 못함(%s): %s", p.name, exc)
        return "", []

    def select_candidates(self, ymd: str) -> list[Candidate]:
        cfg = self.cfg
        swing = swing_symbols(cfg.swing_state_file)
        saved_day, saved = self.load_surge_list(ymd)
        ranks = []
        for attempt in range(1 if saved else 3):  # 장 시작 전에는 순위가 비어 있음 — 저장 목록이 없을 때만 기다렸다 다시
            ranks = self.client.change_rate_ranking()
            if ranks:
                break
            if not saved:
                log.warning("등락률 순위가 비어 있음, 60초 뒤 다시 (%d/3)", attempt + 1)
                self.sleep_fn(60)
        rows = self._surge_rows(ranks)
        have = {r["code"] for r in rows}
        rows += [r for r in saved if r.get("code") not in have]
        log.info("대상 후보 %d종목 (전날 저장 %s %d종목 + 지금 등락률 순위 %d종목)", len(rows),
                 saved_day or "없음", len(saved), len(have))
        if not rows:
            log.warning("등락률 순위가 비어 있고 전날 저장한 목록도 없어 오늘 대상을 고를 수 없음 "
                        "(봇을 전날 %s 까지 켜 두면 목록이 저장됩니다)", SURGE_SAVE_TIME)
        out, last_day = [], ""
        for r in rows:
            code = r["code"]
            if code in swing:
                log.info("  %s %s: 토스 스윙 봇 보유 종목이라 제외", code, r.get("name", ""))
                continue
            try:
                daily = [d for d in self.client.daily_chart(code, ymd) if str(d.get("dt", "")) < ymd]
            except Exception as exc:  # noqa: BLE001
                log.warning("  %s 일봉 조회 실패: %s", code, exc)
                continue
            if len(daily) < 21:
                continue
            last, prior = daily[0], daily[1]
            last_day = max(last_day, str(last.get("dt", "")))
            close, prev = num(last.get("cur_prc")), num(prior.get("cur_prc"))
            if prev <= 0:
                continue
            change = (close / prev - 1) * 100
            avg_amt = sum(num(d.get("cur_prc")) * num(d.get("trde_qty")) for d in daily[1:21]) / 20
            if change < cfg.min_prev_change or avg_amt < cfg.min_avg_amount or close > cfg.max_price:
                continue
            out.append(Candidate(code, str(r.get("name", "")), close, round(change, 2)))
            log.info(f"  대상 {code} {out[-1].name}: 전일 {change:+.1f}%, 종가 {close:,.0f}, "
                     f"20일 평균 거래대금 {avg_amt / 1e8:.0f}억")
        if saved and not have and last_day and saved_day != last_day:
            log.warning("저장된 급등 목록(%s)이 마지막 거래일(%s) 것이 아니라 대상이 빠졌을 수 있음 "
                        "(마지막 거래일 %s 에 봇이 꺼져 있었음)", saved_day, last_day, SURGE_SAVE_TIME)
        return out

    def prepare(self) -> None:
        now = self.now_fn()
        ymd = now.strftime("%Y%m%d")
        if self.load(ymd):
            return
        equity = 0.0
        if self.cfg.dry_run:
            try:
                equity = num(self.client.balance().get("prsm_dpst_aset_amt"))
            except Exception as exc:  # noqa: BLE001
                log.warning("평가금액 조회 실패(드라이런은 100만원으로 가정): %s", exc)
            equity = equity or 1_000_000
        else:
            equity = num(self.client.balance().get("prsm_dpst_aset_amt"))
        self.reserve.ensure_base(equity, ymd)
        total, equity = equity, self.reserve.operating(equity)  # 주문 금액은 적립금을 뺀 운용금 기준
        log.info(f"대상 고르기 ({ymd}, 평가금액 {total:,.0f}원"
                 + (f" = 운용금 {equity:,.0f} + 적립금 {total - equity:,.0f}" if self.reserve.enabled else "")
                 + f", {'모의투자' if self.cfg.mock else '실전'}{' · 드라이런' if self.cfg.dry_run else ''})")
        c = self.cfg
        log.info(f"설정: 전일 +{c.min_prev_change:g}%↑, {c.buy_until} 까지 매수, 하루 {c.max_positions}종목 x {c.position_pct:g}%, "
                 f"손절 -{c.stop_pct:g}% / 익절 +{c.take_profit_pct:g}% / {c.exit_time} 정리"
                 + (f", ETF {c.etf_code} {c.etf_pct:g}% 오버나이트" if c.etf_enabled else ", ETF 끔")
                 + (f", 수익의 {c.reserve_pct:g}% 적립(운용금 {c.reserve_floor:,.0f}원 아래면 보충)" if self.reserve.enabled
                    else ", 적립 끔"))
        self.state = DayState(date=ymd, equity=equity, candidates=self.select_candidates(ymd))
        log.info("오늘 대상 %d종목", len(self.state.candidates))
        self.save()

    # ----------------------------------------------------------------- poll
    def _active_trades(self) -> list[Trade]:
        return [t for t in self.state.trades if t.status not in ("canceled",)]

    def step(self) -> bool:
        """한 번 확인. 계속 돌아야 하면 True."""
        now = self.now_fn()
        hm = now.strftime("%H:%M")
        ymd = self.state.date
        if hm >= self.cfg.exit_time:
            self.exit_all(now)
            return any(t.status in ("pending", "open", "closing") for t in self.state.trades)

        bars_cache: dict[str, list[dict]] = {}

        def bars(code: str) -> list[dict]:
            if code not in bars_cache:
                try:
                    bars_cache[code] = today_bars(self.client.minute_chart(code), ymd)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s 분봉 조회 실패: %s", code, exc)
                    bars_cache[code] = []
                if bars_cache[code]:
                    self._sim_price[code] = bars_cache[code][-1]["close"]
            return bars_cache[code]

        self.manage_trades(now, bars)
        if "09:00" <= hm <= self.cfg.buy_until:
            self.check_entries(now, bars)
        elif hm > self.cfg.buy_until:
            for c in self.state.candidates:
                if c.status == "watch":
                    c.status = "expired"
        self.save()
        return True

    def check_entries(self, now: datetime, bars) -> None:
        cfg = self.cfg
        for c in self.state.candidates:
            if c.status != "watch":
                continue
            b = bars(c.code)
            if not b:
                continue
            if b[0]["open"] >= c.prev_close:
                c.status = "skip_gap"
                log.info(f"{c.code} {c.name}: 시가 {b[0]['open']:,.0f} 가 전일 종가 {c.prev_close:,.0f} 이상(갭상승) → 제외")
                continue
            if max(x["high"] for x in b) <= c.prev_close:
                continue
            cur = b[-1]["close"]
            if len(self._active_trades()) >= cfg.max_positions:
                c.status = "full"
                log.info("%s %s: 전일 종가 돌파했지만 오늘 %d종목 다 참", c.code, c.name, cfg.max_positions)
                continue
            cap = c.prev_close * (1 + cfg.max_chase_pct / 100)
            if cur > cap:
                c.status = "missed_chase"
                log.info(f"{c.code} {c.name}: 돌파 확인 시 현재가 {cur:,.0f} 가 전일 종가 +{cfg.max_chase_pct}% 초과 → 추격 안 함")
                continue
            price = round_up_to_tick(min(cur * (1 + cfg.entry_slip_pct / 100), cap))
            qty = int(self.state.equity * cfg.position_pct / 100 // price)
            cash = self._orderable()
            if cash is not None and qty * price > cash:
                log.info(f"{c.code}: 주문가능금액 {cash:,.0f}원이 모자라 {qty}주 → {int(cash // price)}주")
                qty = int(cash // price)
            if qty < 1:
                c.status = "too_expensive"
                continue
            order_no = "DRY" if cfg.dry_run else self.client.buy(c.code, qty, price)
            c.status = "bought"
            self.state.trades.append(Trade(c.code, c.name, order_no, qty, price, now.isoformat()))
            log.info(f"매수 주문 {c.code} {c.name}: {qty}주 x {price:,}원 (전일 종가 {c.prev_close:,.0f} 돌파, "
                     f"현재가 {cur:,.0f}) 주문번호 {order_no}")

    def _orderable(self) -> float | None:
        """실주문 모드에서 키움 주문가능금액 (조회 실패·드라이런이면 None → 평가금액 기준 그대로)."""
        if self.cfg.dry_run:
            return None
        try:
            return self.client.orderable_cash()
        except Exception as exc:  # noqa: BLE001
            log.warning("주문가능금액 조회 실패(평가금액 기준으로 주문): %s", exc)
            return None

    # ------------------------------------------------------------- manage
    def _open_orders(self) -> dict[str, dict]:
        if self.cfg.dry_run:
            return {}
        return {ord_key(o.get("ord_no")): o for o in self.client.unfilled()}

    def _holdings(self) -> dict[str, dict]:
        if self.cfg.dry_run:
            return {t.code: {"qty": t.qty, "avg_price": t.entry_price} for t in self.state.trades
                    if t.status in ("open", "closing") and t.qty}
        return self.client.holdings()

    def manage_trades(self, now: datetime, bars) -> None:
        cfg = self.cfg
        live = [t for t in self.state.trades if t.status in ("pending", "open", "closing")]
        if not live:
            return
        orders = self._open_orders()
        held = self._holdings()
        for t in live:
            if t.status == "pending":
                if cfg.dry_run:
                    t.qty, t.entry_price = t.order_qty, float(t.order_price)
                    self._opened(t)
                    continue
                o = orders.get(ord_key(t.buy_order))
                age = (now - datetime.fromisoformat(t.ordered_at)).total_seconds()
                if o is not None:
                    if age >= cfg.fill_timeout_sec:
                        log.info("%s 매수 %d초 지나 잔량 %s주 취소", t.code, int(age), o.get("oso_qty"))
                        self.client.cancel(t.code, t.buy_order)
                    continue
                h = held.get(t.code)
                if not h:
                    if age >= cfg.fill_timeout_sec + 10:
                        t.status = "canceled"
                        log.info("%s 매수 체결 없음 → 취소 처리", t.code)
                    continue
                t.qty, t.entry_price = h["qty"], h["avg_price"] or float(t.order_price)
                self._opened(t)
            elif t.status == "open":
                h = held.get(t.code)
                if not h and not cfg.dry_run:
                    self._closed(t, now, "TAKE_PROFIT", float(t.target))
                    continue
                b = bars(t.code)
                cur = b[-1]["close"] if b else 0
                low = b[-1]["low"] if b else 0
                if cfg.dry_run and cur and b[-1]["high"] >= t.target:
                    self._closed(t, now, "TAKE_PROFIT", float(t.target))
                    continue
                if cur and min(cur, low or cur) <= t.stop:
                    log.info(f"{t.code} 손절: 현재가 {cur:,.0f} (분봉 저가 {low:,.0f}) ≤ 손절가 {t.stop:,.0f}")
                    self._sell_all(t, held, orders, now, "STOP_LOSS", cur)
            elif t.status == "closing":
                if cfg.dry_run or not held.get(t.code):
                    self._closed(t, now, t.exit_reason or "CLOSED", t.exit_price)

    def _opened(self, t: Trade) -> None:
        cfg = self.cfg
        t.status = "open"
        t.stop = t.entry_price * (1 - cfg.stop_pct / 100)
        t.target = round_up_to_tick(t.entry_price * (1 + cfg.take_profit_pct / 100))
        t.tp_order = "DRY" if cfg.dry_run else self.client.sell(t.code, t.qty, t.target)
        log.info(f"체결 {t.code} {t.name}: {t.qty}주 평균 {t.entry_price:,.0f}원 → 익절 지정가 {t.target:,}원 "
                 f"주문({t.tp_order}), 손절가 {t.stop:,.0f}원")

    def _sell_all(self, t: Trade, held: dict, orders: dict, now: datetime, reason: str, price: float) -> None:
        if not self.cfg.dry_run:
            if t.tp_order and ord_key(t.tp_order) in orders:
                self.client.cancel(t.code, t.tp_order)
            qty = (held.get(t.code) or {}).get("qty", 0)
            if qty:
                self.client.sell(t.code, qty)
        t.status = "closing"
        t.exit_reason, t.exit_price = reason, price
        if self.cfg.dry_run:
            self._closed(t, now, reason, price)

    def _closed(self, t: Trade, now: datetime, reason: str, price: float) -> None:
        t.status = "closed"
        t.exit_reason, t.exit_price, t.exit_at = reason, price, now.isoformat()
        ret = (price / t.entry_price - 1) * 100 if t.entry_price and price else 0.0
        log.info(f"청산 {t.code} {t.name}: {reason} {price:,.0f}원 (매수가 대비 {ret:+.2f}%, 비용 전)")

    def exit_all(self, now: datetime) -> None:
        live = [t for t in self.state.trades if t.status in ("pending", "open")]
        if not live:
            closing = [t for t in self.state.trades if t.status == "closing"]
            if closing:
                held = self._holdings()
                for t in closing:
                    if self.cfg.dry_run or not held.get(t.code):
                        self._closed(t, now, t.exit_reason, t.exit_price)
                self.save()
            return
        orders = self._open_orders()
        if not self.cfg.dry_run:
            for t in live:  # 미체결 매수·익절 주문 모두 취소
                for no in (t.buy_order, t.tp_order):
                    if no and ord_key(no) in orders:
                        self.client.cancel(t.code, no)
            self.sleep_fn(1)
        held = self._holdings()
        for t in live:
            price = self._sim_price.get(t.code, t.entry_price)
            if t.status == "pending" and not (held.get(t.code) or {}).get("qty") and not self.cfg.dry_run:
                t.status = "canceled"
                continue
            log.info("%s 정리 시각 %s → 시장가 매도", t.code, self.cfg.exit_time)
            if not self.cfg.dry_run and t.status == "pending":
                h = held.get(t.code)
                t.qty, t.entry_price = h["qty"], h["avg_price"]
            self._sell_all(t, held, {}, now, "TIME_EXIT", price)
        self.save()

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        now = self.now_fn()
        if now.weekday() >= 5:
            log.info("주말이라 종료")
            return
        etf = EtfOvernight(self.client, self.cfg) if self.cfg.etf_enabled else None
        hm = self.now_fn().strftime("%H:%M")
        if etf and hm < "09:00":
            while self.now_fn().strftime("%H:%M") < self.cfg.etf_sell_time:
                self.sleep_fn(30)
            etf.morning_sell()
        while self.now_fn().strftime("%H:%M") < "08:50":
            self.sleep_fn(30)
        self.prepare()
        if self.state.candidates or self.state.trades:
            self.day_loop(etf)
        else:
            log.info("오늘 단타 대상 없음")
        self.summary()
        self.settle_reserve()
        if etf:
            while self.now_fn().strftime("%H:%M") < self.cfg.etf_buy_time:
                self.sleep_fn(30)
            if self.now_fn().strftime("%H:%M") < "15:30":
                try:
                    equity = num(self.client.balance().get("prsm_dpst_aset_amt"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("평가금액 조회 실패: %s", exc)
                    equity = 0.0
                if equity:  # 12시 정리 뒤의 평가금액 변화 = 입출금, ETF 는 운용금만큼만
                    self.reserve.mark_flows(equity, self.state.date)
                    equity = self.reserve.operating(equity)
                if self.cfg.dry_run:
                    equity = equity or self.state.equity
                etf.evening_buy(self.now_fn(), equity)
            else:
                log.info("장 마감 동시호가가 지나 ETF 매수 안 함")
        while self.now_fn().strftime("%H:%M") < SURGE_SAVE_TIME:
            self.sleep_fn(30)
        self.save_surge_list(self.now_fn().strftime("%Y%m%d"))

    def settle_reserve(self) -> None:
        """정리 시각(exit_time)이 지나 보유 종목이 없을 때 하루 손익을 재고 수익의 일부를 적립한다."""
        if not self.reserve.enabled:
            return
        while self.now_fn().strftime("%H:%M") < self.cfg.exit_time:
            self.sleep_fn(30)
        try:
            equity = num(self.client.balance().get("prsm_dpst_aset_amt"))
        except Exception as exc:  # noqa: BLE001
            log.warning("평가금액 조회 실패로 적립금 정산을 건너뜀: %s", exc)
            return
        self.reserve.settle(equity, self.state.date)

    def day_loop(self, etf: "EtfOvernight | None" = None) -> None:
        logged_cash = False
        while True:
            hm = self.now_fn().strftime("%H:%M")
            if hm < "09:00":
                self.sleep_fn(5)
                continue
            if etf and not logged_cash:
                etf.log_cash_after_open()
                logged_cash = True
            keep = self.step()
            if hm >= self.cfg.exit_time and not keep:
                break
            if hm > self.cfg.buy_until and not any(
                    t.status in ("pending", "open", "closing") for t in self.state.trades):
                log.info("매수 시간이 끝났고 보유 종목이 없어 단타 끝")
                break
            self.sleep_fn(self.cfg.poll_seconds)

    def summary(self) -> None:
        closed = [t for t in self.state.trades if t.status == "closed" and t.entry_price]
        if not closed:
            log.info("오늘 거래 없음")
            return
        pnl = sum((t.exit_price - t.entry_price) * t.qty for t in closed)
        log.info(f"오늘 {len(closed)}건, 손익(비용 전) {pnl:+,.0f}원")
        for t in closed:
            log.info(f"  {t.code} {t.name} {t.qty}주 {t.entry_price:,.0f} → {t.exit_price:,.0f} ({t.exit_reason})")
