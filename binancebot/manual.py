"""조건 주문(테스트 매매 전용): 사용자가 정한 위치에 가격이 닿으면 진입한 것으로 기록하고 손절·익절까지 따라간다.

실제 주문은 보내지 않는다 (공개 시세만 조회, API 키 필요 없음). 실전 봇(bot.py)과는 별개 프로그램.
진입 위치: 고정 가격 / 이동평균선(봉이 끝날 때마다 다시 계산) / 고점·저점의 비율 지점(1/2 등, 저점·고점 자동 갱신 가능).
조건 하나 = 매매 한 번. 끝나면(손절·익절·취소·기한) 다시 들어가지 않는다."""
from __future__ import annotations

import csv
import json
import logging
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("binancebot.manual")

TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
FEE_PCT = 0.05          # 한쪽 수수료 % (테스트 손익 계산용)
MAX_LEV = 5.0
ARM_PCT = 0.05          # 위치에서 이만큼(%) 떨어져 있어야 '닿기를 기다리는 상태'로 봄


def sym_of(s: str) -> str:
    s = s.upper().strip()
    return s if s.endswith("USDT") else s + "USDT"


def moving_average(rows: list[list], kind: str, length: int) -> float | None:
    """끝난 봉들(klines 행)의 이동평균. kind = sma / ema / vwma."""
    if len(rows) < length:
        return None
    closes = [float(r[4]) for r in rows]
    if kind == "sma":
        return sum(closes[-length:]) / length
    if kind == "vwma":
        vols = [float(r[5]) for r in rows[-length:]]
        tot = sum(vols)
        return sum(c * v for c, v in zip(closes[-length:], vols)) / tot if tot > 0 else None
    if kind == "ema":
        k = 2 / (length + 1)
        e = sum(closes[:length]) / length
        for c in closes[length:]:
            e = c * k + e * (1 - k)
        return e
    raise ValueError(f"모르는 이동평균 종류: {kind}")


def new_order(symbol: str, side: str, entry: dict, stop: dict, tp: dict | None = None, lev: float = 1.0,
              hours: float = 0.0, now: datetime | None = None, note: str = "", close_at: datetime | None = None,
              void_ma: dict | None = None) -> dict:
    """주문 하나를 만든다 (검증 포함). side = long / short.
    entry: {"type": "price", "price": x} | {"type": "ma", "tf": "1h", "kind": "vwma", "len": 100}
           | {"type": "mid", "high": h, "low": l, "ratio": 0.5, "auto": True}   (auto: 숏이면 저점, 롱이면 고점을 새 값으로 갱신)
    stop:  {"type": "price", "price": x} | {"type": "pct", "pct": 3} | {"type": "anchor"} (mid 의 고점 위·저점 아래)
           | {"type": "ma_close", "tf": "15m", "kind": "sma", "len": 200}  (끝난 봉의 시가·종가가 모두 이평선 너머면: 숏은 위, 롱은 아래)
    tp:    None | {"type": "price", "price": x} | {"type": "r", "r": 2} | {"type": "far"} (mid 의 마지막 저점·고점)
           | {"type": "ma", "tf": "15m", "kind": "sma", "len": 50}  (가격이 이평선에 닿으면)
    close_at: 이 시각이 되면 대기는 취소, 보유는 그때 가격으로 정리 (None = 없음)
    void_ma: {"tf", "kind", "len"} 진입 전에 이 이평선이 가격 아래(숏)·위(롱)로 가면 폐기"""
    side_i = {"long": 1, "short": -1}[side]
    if not 0 < lev <= MAX_LEV:
        raise ValueError(f"배율은 0 초과 {MAX_LEV:g} 이하")
    if entry["type"] == "ma":
        if entry["tf"] not in TF_MIN or entry["kind"] not in ("sma", "ema", "vwma") or not 2 <= int(entry["len"]) <= 1000:
            raise ValueError("이동평균 설정을 확인해 주세요 (봉 1m/5m/15m/30m/1h/4h/1d, 종류 sma/ema/vwma, 기간 2~1000)")
    elif entry["type"] == "mid":
        if not entry["high"] > entry["low"] > 0 or not 0 < entry.get("ratio", 0.5) < 1:
            raise ValueError("고점은 저점보다 커야 하고 비율은 0~1 사이")
    elif entry["type"] != "price" or entry["price"] <= 0:
        raise ValueError("진입 위치를 확인해 주세요")
    if stop["type"] == "anchor" and entry["type"] != "mid":
        raise ValueError("'기준 고점·저점' 손절은 고점·저점 비율 진입에서만 쓸 수 있어요")
    if tp and tp["type"] == "far" and entry["type"] != "mid":
        raise ValueError("'마지막 저점·고점' 익절은 고점·저점 비율 진입에서만 쓸 수 있어요")
    if stop["type"] == "pct" and not 0 < stop["pct"] <= 30:
        raise ValueError("손절 %는 0 초과 30 이하")
    for m in [x for x in (stop, tp) if x and x["type"] in ("ma", "ma_close")] + ([void_ma] if void_ma else []):
        if m["tf"] not in TF_MIN or m["kind"] not in ("sma", "ema", "vwma") or not 2 <= int(m["len"]) <= 1000:
            raise ValueError("이동평균 설정을 확인해 주세요 (봉 1m/5m/15m/30m/1h/4h/1d, 종류 sma/ema/vwma, 기간 2~1000)")
    if stop["type"] == "ma_close" and tp and tp["type"] == "r":
        raise ValueError("이평선 손절은 손절 가격이 정해져 있지 않아 '손절폭의 몇 배' 익절과 같이 쓸 수 없어요")
    now = now or datetime.now(timezone.utc)
    if close_at and close_at <= now:
        raise ValueError("종료 시각이 이미 지났어요")
    return {"id": now.strftime("%m%d%H%M%S"), "symbol": sym_of(symbol), "side": side_i, "entry": entry, "stop": stop,
            "tp": tp, "lev": lev, "created": now.isoformat(), "expires": (now + timedelta(hours=hours)).isoformat() if hours > 0 else "",
            "close_at": close_at.astimezone(timezone.utc).isoformat() if close_at else "", "void_ma": void_ma or {},
            "status": "waiting", "armed": False, "level": 0.0, "pos": {}, "result": {}, "note": note}


class Store:
    def __init__(self, state_dir: str):
        self.dir = Path(state_dir) / "manual"
        self.path = self.dir / "orders.json"

    def load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, orders: list[dict]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(orders, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, order: dict) -> dict:
        orders = self.load()
        while any(o["id"] == order["id"] for o in orders):
            order["id"] += "a"
        orders.append(order)
        self.save(orders)
        return order

    def cancel(self, oid: str) -> bool:
        orders = self.load()
        hit = [o for o in orders if o["id"] == oid and o["status"] in ("waiting", "position")]
        for o in hit:
            o["cancel"] = True      # 감시 프로그램이 다음 확인 때 처리 (꺼져 있으면 list 가 '취소 요청'으로 보여 줌)
        self.save(orders)
        return bool(hit)


def describe(o: dict) -> str:
    e, s, t = o["entry"], o["stop"], o["tp"]
    side = "롱" if o["side"] == 1 else "숏"
    if e["type"] == "price":
        ent = f"{e['price']:g} 에 닿으면"
    elif e["type"] == "ma":
        ent = f"{e['tf']} {e['kind'].upper()}{e['len']} 선에 닿으면"
    else:
        ent = f"고점 {e['high']:g}·저점 {e['low']:g} 의 {e.get('ratio', 0.5):g} 지점" + (" (자동 갱신)" if e.get("auto") else "")
    ma = lambda m: f"{m['tf']} {m['kind'].upper()}{m['len']} 선"
    st = {"price": lambda: f"{s['price']:g}", "pct": lambda: f"진입가 {s['pct']:g}% 반대", "anchor": lambda: "기준 고점·저점 밖",
          "ma_close": lambda: f"봉 시가·종가가 모두 {ma(s)} {'위' if o['side'] == -1 else '아래'}"}[s["type"]]()
    tp = "없음" if not t else {"price": lambda: f"{t['price']:g}", "r": lambda: f"손절폭의 {t['r']:g}배", "far": lambda: "마지막 저점·고점",
                             "ma": lambda: f"{ma(t)}에 닿으면"}[t["type"]]()
    end = ""
    if o.get("close_at"):
        end = " · " + datetime.fromisoformat(o["close_at"]).astimezone(timezone(timedelta(hours=9))).strftime("%m-%d %H:%M") + "(한국) 종료"
    if o.get("void_ma"):
        end += (f" · 진입 전 {ma(o['void_ma'])}이 {'진입 위치' if o['void_ma'].get('ref') == 'level' else '가격'} "
                f"{'아래' if o['side'] == -1 else '위'}로 가면 폐기")
    return f"{o['symbol'][:-4]} {side} · {ent} · 손절 {st} · 익절 {tp} · {o['lev']:g}배{end}"


class ManualTrader:
    """테스트 매매 감시. client 는 prices()·klines() 만 쓴다."""

    def __init__(self, client, state_dir: str, equity: float = 1000.0, now_fn=None, sleep_fn=_time.sleep, poll: float = 2.0):
        self.c, self.store, self.equity0 = client, Store(state_dir), equity
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self.sleep, self.poll = sleep_fn, poll
        self.ma_cache: dict[tuple, tuple[datetime, float | None]] = {}

    # ------------------------------------------------------------ 위치 계산
    def ma_value(self, sym: str, tf: str, kind: str, length: int) -> float | None:
        return self.ma_bar(sym, tf, kind, length)[0]

    def ma_bar(self, sym: str, tf: str, kind: str, length: int) -> tuple:
        """(이평 값, 마지막으로 끝난 봉의 시가, 종가, 그 봉이 끝난 시각 ms). 20초마다 다시 계산."""
        now = self.now()
        key = (sym, tf, kind, length)
        hit = self.ma_cache.get(key)
        if hit and (now - hit[0]).total_seconds() < 20:
            return hit[1]
        step = TF_MIN[tf] * 60_000
        need = length * (3 if kind == "ema" else 1) + 3
        now_ms = int(now.timestamp() * 1000)
        rows = self.c.klines(sym, now_ms - need * step, limit=min(need + 2, 1500), interval=tf)
        rows = [r for r in rows if r[0] + step <= now_ms]          # 끝난 봉만
        val = (moving_average(rows, kind, length),) + ((float(rows[-1][1]), float(rows[-1][4]), rows[-1][0] + step) if rows else (0.0, 0.0, 0))
        self.ma_cache[key] = (now, val)
        return val

    def level(self, o: dict, p: float) -> float | None:
        e = o["entry"]
        if e["type"] == "price":
            return e["price"]
        if e["type"] == "ma":
            return self.ma_value(o["symbol"], e["tf"], e["kind"], int(e["len"]))
        if e.get("auto"):                                           # 숏: 저점이 더 내려가면 따라감 / 롱: 고점이 더 올라가면 따라감
            if o["side"] == -1 and p < e["low"]:
                e["low"] = p
            elif o["side"] == 1 and p > e["high"]:
                e["high"] = p
        r = e.get("ratio", 0.5)
        return e["low"] + (e["high"] - e["low"]) * r if o["side"] == -1 else e["high"] - (e["high"] - e["low"]) * r

    # ------------------------------------------------------------ 한 번 확인
    def tick(self) -> None:
        orders = self.store.load()
        live = [o for o in orders if o["status"] in ("waiting", "position")]
        if not live:
            return
        prices = self.c.prices()
        now = self.now()
        for o in live:
            p = prices.get(o["symbol"])
            if o.get("cancel"):
                if o["status"] == "position" and p:
                    self.finish(o, "cancel", p)
                else:
                    o["status"], o["result"] = "cancelled", {"reason": "cancel", "time": now.isoformat()}
                    log.info("[테스트] 취소: %s", describe(o))
                continue
            if p is None:
                continue
            if o.get("close_at") and now >= datetime.fromisoformat(o["close_at"]):
                if o["status"] == "position":
                    self.finish(o, "time", p)
                else:
                    o["status"], o["result"] = "expired", {"reason": "expired", "time": now.isoformat()}
                    log.info("[테스트] 종료 시각이 돼 취소: %s", describe(o))
            elif o["status"] == "waiting":
                self.check_entry(o, p, now)
            else:
                self.check_exit(o, p)
        self.store.save(orders)

    def check_entry(self, o: dict, p: float, now: datetime) -> None:
        if o["expires"] and now >= datetime.fromisoformat(o["expires"]):
            o["status"], o["result"] = "expired", {"reason": "expired", "time": now.isoformat()}
            log.info("[테스트] 기한 지남: %s", describe(o))
            return
        e, s = o["entry"], o["side"]
        if e["type"] == "mid" and ((s == -1 and p >= e["high"]) or (s == 1 and p <= e["low"])):
            o["status"], o["result"] = "cancelled", {"reason": "broken", "time": now.isoformat()}
            log.info("[테스트] 기준 %s을 넘어서 취소: %s", "고점" if s == -1 else "저점", describe(o))
            return
        v = o.get("void_ma")
        if v:
            mv = self.ma_value(o["symbol"], v["tf"], v["kind"], int(v["len"]))
            ref = (self.level(o, p) if v.get("ref") == "level" else p) or p   # 비교 기준: 진입 위치 또는 지금 가격
            hit = (mv - ref) * s >= 0 if v.get("ref") == "level" else (mv - ref) * s > 0
            if mv and hit:                                          # 숏: 이평선이 기준 아래 / 롱: 이평선이 기준 위
                o["status"], o["result"] = "cancelled", {"reason": "void", "time": now.isoformat()}
                log.info("[테스트] 진입 전에 이평선(%g)이 %s(%g) %s로 가서 폐기: %s", mv, "진입 위치" if v.get("ref") == "level" else "가격",
                         ref, "이하" if s == -1 else "이상", describe(o))
                return
        lvl = self.level(o, p)
        if not lvl:
            return
        o["level"] = lvl
        # 롱은 위에서 내려와 닿을 때, 숏은 아래에서 올라와 닿을 때
        away = (p - lvl) * s / lvl * 100
        if not o["armed"]:
            if away >= ARM_PCT:
                o["armed"] = True
                log.info("[테스트] 대기 시작: %s (지금 %g, 위치 %g)", describe(o), p, lvl)
            return
        if away > 0:
            return
        entry = p
        st = o["stop"]
        if st["type"] == "price":
            stop = st["price"]
        elif st["type"] == "pct":
            stop = entry * (1 - s * st["pct"] / 100)
        elif st["type"] == "ma_close":
            stop = 0.0                                              # 가격이 아니라 끝난 봉으로 판단 (check_exit)
        else:
            stop = e["high"] * 1.001 if s == -1 else e["low"] * 0.999
        if stop and (stop - entry) * s >= 0:
            o["status"], o["result"] = "cancelled", {"reason": "bad_stop", "time": now.isoformat()}
            log.info("[테스트] 손절가가 진입가의 반대편이 아니라 취소: %s (진입 %g, 손절 %g)", describe(o), entry, stop)
            return
        t = o["tp"]
        tp = 0.0
        tp_side = 0
        if t and t["type"] == "ma":
            mv = self.ma_value(o["symbol"], t["tf"], t["kind"], int(t["len"]))
            tp_side = (1 if entry > mv else -1) if mv else 0        # 진입 때 가격이 이평선의 어느 쪽인지 (그쪽에서 닿으면 청산)
        elif t:
            tp = t["price"] if t["type"] == "price" else entry + s * abs(entry - stop) * t["r"] if t["type"] == "r" else (e["low"] if s == -1 else e["high"])
            if (tp - entry) * s <= 0:
                tp = 0.0
        qty = self.equity() * o["lev"] / entry
        o["status"] = "position"
        o["pos"] = {"entry": entry, "stop": stop, "tp": tp, "qty": qty, "time": now.isoformat(), "level": lvl, "tp_side": tp_side}
        log.info("[테스트] 진입 %s %s @ %g (위치 %g) 손절 %g 익절 %s, %.4f개", o["symbol"], "롱" if s == 1 else "숏", entry, lvl, stop,
                 f"{tp:g}" if tp else "없음", qty)

    def check_exit(self, o: dict, p: float) -> None:
        pos, s = o["pos"], o["side"]
        st, t = o["stop"], o["tp"]
        if st["type"] == "ma_close":
            mv, bo, bc, end_ms = self.ma_bar(o["symbol"], st["tf"], st["kind"], int(st["len"]))
            entered = datetime.fromisoformat(pos["time"]).timestamp() * 1000
            if mv and end_ms > entered and (bo - mv) * s < 0 and (bc - mv) * s < 0:   # 진입 뒤에 끝난 봉만
                log.info("[테스트] %s 봉 시가 %g·종가 %g 가 모두 이평선 %g %s", o["symbol"], bo, bc, mv, "위" if s == -1 else "아래")
                self.finish(o, "stop", p)
                return
        elif (p - pos["stop"]) * s <= 0:
            self.finish(o, "stop", p)
            return
        if t and t["type"] == "ma":
            mv = self.ma_value(o["symbol"], t["tf"], t["kind"], int(t["len"]))
            if mv:
                pos["tp"] = mv                                      # 화면 표시용 (지금 이평선 값)
                if not pos.get("tp_side"):
                    pos["tp_side"] = 1 if p > mv else -1
                elif (p - mv) * pos["tp_side"] <= 0:
                    self.finish(o, "target", p)
        elif pos["tp"] and (p - pos["tp"]) * s >= 0:
            self.finish(o, "target", pos["tp"])

    def equity(self) -> float:
        """테스트 평가금액 = 시작 금액 + 지금까지 끝난 테스트 매매 손익."""
        return self.equity0 + sum(o["result"].get("pnl", 0.0) for o in self.store.load() if o["status"] == "done")

    def finish(self, o: dict, reason: str, px: float) -> None:
        pos, s = o["pos"], o["side"]
        gross = s * (px / pos["entry"] - 1) * 100
        ret = gross - 2 * FEE_PCT
        pnl = pos["qty"] * pos["entry"] * ret / 100
        now = self.now()
        o["status"] = "done"
        o["result"] = {"reason": reason, "exit": px, "ret_pct": round(ret, 3), "pnl": round(pnl, 4), "time": now.isoformat()}
        log.info("[테스트] 끝 %s: %s @ %g, %+.2f%% (%+.2f USDT, 수수료 포함)", o["symbol"],
                 {"stop": "손절", "target": "익절", "cancel": "직접 정리", "time": "종료 시각"}[reason], px, ret, pnl)
        path = self.store.dir / "trades.csv"
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["id", "symbol", "side", "entry_time", "entry", "stop", "tp", "exit_time", "exit", "reason", "lev", "ret_pct", "pnl", "note"])
            w.writerow([o["id"], o["symbol"], s, pos["time"], pos["entry"], pos["stop"], pos["tp"], now.isoformat(), px, reason,
                        o["lev"], round(ret, 3), round(pnl, 4), describe(o)])

    def run(self, until: datetime | None = None) -> None:
        log.info("조건 주문 감시 시작 (테스트 매매만, 실제 주문 없음). 대기·보유 %d건",
                 sum(o["status"] in ("waiting", "position") for o in self.store.load()))
        while until is None or self.now() < until:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 시세 조회 오류는 다음 번에 다시
                log.warning("확인 중 오류 (%s), 다시 시도", exc)
            self.sleep(self.poll)


def show(state_dir: str, prices: dict[str, float] | None = None, all_: bool = False) -> None:
    orders = Store(state_dir).load()
    rows = orders if all_ else [o for o in orders if o["status"] in ("waiting", "position")] + [o for o in orders if o["status"] not in ("waiting", "position")][-5:]
    if not rows:
        print("등록된 조건 주문이 없어요.")
        return
    name = {"waiting": "대기", "position": "보유", "done": "끝", "cancelled": "취소", "expired": "기한 지남"}
    for o in rows:
        line = f"[{o['id']}] {name[o['status']]}{' (취소 요청)' if o.get('cancel') and o['status'] in ('waiting', 'position') else ''} | {describe(o)}"
        p = (prices or {}).get(o["symbol"])
        if o["status"] == "waiting":
            line += f" | 위치 {o['level']:g}" if o["level"] else " | 위치 계산 전"
            line += f", 지금 {p:g}" if p else ""
            line += "" if o["armed"] else " (아직 위치 반대편 — 지나갔다가 다시 닿아야 진입)"
        elif o["status"] == "position":
            pos = o["pos"]
            line += f" | 진입 {pos['entry']:g} 손절 {pos['stop']:g if pos['stop'] else '이평선 기준'} 익절 {pos['tp']:g}" + (f", 지금 {p:g} ({o['side'] * (p / pos['entry'] - 1) * 100:+.2f}%)" if p else "")
        elif o["status"] == "done":
            r = o["result"]
            line += f" | {o['pos']['entry']:g} → {r['exit']:g} {r['ret_pct']:+.2f}% ({r['pnl']:+.2f} USDT)"
        print(line)
    done = [o for o in orders if o["status"] == "done"]
    if done:
        print(f"테스트 매매 {len(done)}건, 이긴 것 {sum(o['result']['pnl'] > 0 for o in done)}건, 손익 합계 {sum(o['result']['pnl'] for o in done):+.2f} USDT")
