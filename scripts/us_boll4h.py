"""볼린저 4시간봉 '큰 움직임 뒤 횡보 → 상단 돌파 롱, N봉 이내 정리' 를 실제 미국 주식(10달러 이하)에 적용.

    python scripts/us_boll4h.py download   # 키움 REST 로 대상 목록 + 4시간봉 받기 (조회만) → data/us_4h
    python scripts/us_boll4h.py            # 계산

규칙 (바이낸스에서 테스트한 것과 같음, 완성된 4시간봉 기준):
  직전 L봉(횡보 박스)의 고가-저가 = box, 그 앞 L봉 동안의 종가 변화 = move, |move| >= K x box
  → 종가가 볼린저(20, 2) 상단을 처음 넘은 봉 다음 봉 시가에 매수
  → 종가가 중심선 아래로 끝나면 다음 봉 시가 매도, 아니면 매수 뒤 N봉째 봉 시가에 매도 (N = 3 / 5 / 없음)
대상: 키움 '당일 거래대금 상위' 중 10달러 미만 주식(오늘 기준 목록 — 지금 싼 종목만 모은 것이라 과거 성과가 왜곡될 수 있음),
      매수 시점 가격 1~10달러. 키움 4시간봉은 프리·애프터마켓 포함(하루 6봉, 봉 끝 시각 표시·미국 동부)이고 약 6개월치만 줌.
비용: 수수료 한쪽 0.1% + 슬리피지 한쪽 0.1% (토스 미국 주식, 소형주 기준 가정)
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "us_4h"
FEE, SLIP = 0.001, 0.001


# ---------------------------------------------------------------- 받기
def download(top: int = 300) -> None:
    from tossbot.config import load_dotenv
    from kiwoombot.client import KiwoomClient
    from kiwoombot.config import KiwoomConfig

    load_dotenv(str(ROOT / ".env.kiwoom"))
    cfg = KiwoomConfig.from_env()
    c = KiwoomClient(cfg.app_key, cfg.secret_key, mock=False)
    OUT.mkdir(parents=True, exist_ok=True)
    up = OUT / "universe.json"
    if up.exists():
        uni = json.loads(up.read_text(encoding="utf-8"))
    else:
        uni, key = [], ""
        while len(uni) < top:
            data, key = c.request("usa20540", "/api/us/rkinfo", {"stex_tp": "0", "inds_cd": "000", "stk_tp": "1", "pric_cnd": "2"}, key)
            rows = data.get("result_list") or []
            uni += [{"code": r["stk_cd"], "ex": r["stex_tp"], "name": r.get("stk_enm", ""), "price": abs(float(r["cur_prc"])),
                     "amount_k": abs(float(r["trde_prica"]))} for r in rows]
            if not rows or not key:
                break
        up.write_text(json.dumps(uni, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"대상 {len(uni)}종목", flush=True)
    for n, u in enumerate(uni, 1):
        path = OUT / f"{u['code']}_4h.csv"
        if path.exists():
            continue
        rows, key = [], ""
        try:
            for _ in range(40):
                data, key = c.request("usa06011", "/api/us/chart", {"stex_tp": u["ex"], "stk_cd": u["code"], "tic_scope": "240",
                                                                    "upd_stkpc_tp": "1", "exrt_appl_tp": "0"}, key)
                page = data.get("result_list") or []
                rows += page
                if not page or not key:
                    break
        except Exception as exc:  # noqa: BLE001
            print("ERR", u["code"], str(exc)[:80], flush=True)
            continue
        seen = {}
        for r in rows:
            seen[r["cntr_tm"]] = [r["cntr_tm"], abs(float(r["open_pric"])), abs(float(r["high_pric"])), abs(float(r["low_pric"])),
                                  abs(float(r["cur_prc"])), float(r["trde_qty"])]
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t_end_et", "open", "high", "low", "close", "volume"])
            w.writerows(seen[k] for k in sorted(seen))
        if n % 25 == 0:
            print(f"{n}/{len(uni)} {u['code']} {len(seen)}봉", flush=True)
    print("끝", flush=True)


# ---------------------------------------------------------------- 계산
def load(sym: str):
    out = []
    with open(OUT / f"{sym}_4h.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = r["t_end_et"]
            out.append((t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r["volume"])))
    return out


def roll(a, n, fn):
    w = np.lib.stride_tricks.sliding_window_view(a, n)
    r = np.full(len(a), np.nan)
    r[n - 1:] = fn(w, axis=1)
    return r


def signals(bars, L=20, K=0.5, direction="both", hold=5):
    """[(진입 봉 번호, 청산 봉 번호)] — 신호 봉 i → i+1 시가 매수, 종가 < 중심선인 봉 j → j+1 시가 매도, 늦어도 진입 뒤 hold 봉째 시가."""
    o = np.array([b[1] for b in bars]); h = np.array([b[2] for b in bars]); l = np.array([b[3] for b in bars]); c = np.array([b[4] for b in bars])
    n = len(c)
    if n < 2 * L + 30:
        return []
    mid = roll(c, 20, np.mean); sd = roll(c, 20, np.std)
    up = mid + 2 * sd
    cross = np.zeros(n, bool)
    cross[1:] = (c[1:] > up[1:]) & (c[:-1] <= up[:-1])
    below = np.flatnonzero(c < mid)
    hh = roll(h, L, np.max); ll = roll(l, L, np.min)
    res, free = [], 0
    for i in np.flatnonzero(cross):
        if i < 2 * L + 20 or i + 1 >= n or i < free:
            continue
        box = hh[i - 1] - ll[i - 1]
        mv = c[i - L - 1] - c[i - 2 * L - 1]
        if abs(mv) < K * box or (direction == "up" and mv < 0) or (direction == "down" and mv > 0):
            continue
        a = int(i) + 1
        k = np.searchsorted(below, a)
        z = int(below[k]) + 1 if k < len(below) else n
        if hold:
            z = min(z, a + hold)
        if z >= n:
            continue  # 데이터 끝까지 들고 있음 → 버림
        res.append((a, z))
        free = z
    return res


def main() -> None:
    uni = json.loads((OUT / "universe.json").read_text(encoding="utf-8"))
    data = {}
    for u in uni:
        if (OUT / f"{u['code']}_4h.csv").exists():
            b = load(u["code"])
            if len(b) >= 100:
                data[u["code"]] = b
    span = sorted(b[0][0] for b in data.values())
    print(f"종목 {len(data)}개 (봉 100개 이상), 가장 이른 봉 {span[0][:8]} ~ 중간 {span[len(span) // 2][:8]}, 비용 한쪽 {FEE * 100:g}% + {SLIP * 100:g}%\n")
    # 정규장 거래대금 (끝 시각 12·16시 봉) → 전날 순위
    dayqv = {}
    for s, bars in data.items():
        q = defaultdict(float)
        for b in bars:
            if b[0][8:10] in ("12", "16"):
                q[b[0][:8]] += b[4] * b[5]
        dayqv[s] = q
    prev = defaultdict(dict)
    for s, q in dayqv.items():
        ds = sorted(q)
        for a, b in zip(ds, ds[1:]):
            prev[b][s] = q[a]
    rank = {(d, s): i + 1 for d, m in prev.items() for i, (s, _) in enumerate(sorted(m.items(), key=lambda x: -x[1]))}
    liq = {s for s, q in dayqv.items() if q and statistics.median(q.values()) >= 5e6}

    def trades(L=20, K=0.5, direction="both", hold=5, pmax=10.0, pmin=1.0, rth_only=False):
        out = []
        for s, bars in data.items():
            for a, z in signals(bars, L, K, direction, hold):
                e, x = bars[a][1], bars[z][1]
                if not pmin <= e <= pmax:
                    continue
                if rth_only and bars[a][0][8:10] not in ("12", "16"):
                    continue  # 정규장 안에서 시작하는 봉(8~12시 봉은 프리마켓 8시 시가라 뺌, 12~16시 봉만)… 끝 시각 12 = 8시 시작
                ret = (x * (1 - SLIP) * (1 - FEE) / (e * (1 + SLIP) * (1 + FEE)) - 1) * 100
                out.append({"sym": s, "in": bars[a][0], "out": bars[z][0], "entry": e, "exit": x, "ret": ret, "bars": z - a,
                            "rank": rank.get((bars[a][0][:8], s), 999), "liq": s in liq})
        return out

    def stat(rs):
        if not rs:
            return "0건"
        r = sorted(x["ret"] for x in rs)
        ex5 = sum(r[:-5]) / (len(r) - 5) if len(r) > 5 else float("nan")
        return (f"{len(r)}건 거래당 {sum(r) / len(r):+.2f}% 승률 {sum(x > 0 for x in r) / len(r) * 100:.0f}% "
                f"(좋은 5건 빼면 {ex5:+.2f}%, 최악 {r[0]:+.1f}%)")

    def account(rs, capital=100.0, slots=3):
        """100달러, 최대 3종목, 종목당 평가금액/3 (정수 주), 먼저 들어간 순."""
        cash, pos, eq_curve = capital, [], [capital]
        events = sorted(rs, key=lambda x: x["in"])
        done = skipped = 0
        open_pos = []
        for t in events:
            for p in [p for p in open_pos if p["out"] <= t["in"]]:
                cash += p["qty"] * p["exit"] * (1 - SLIP) * (1 - FEE)
                open_pos.remove(p)
            if len(open_pos) >= slots:
                continue
            eq = cash + sum(p["qty"] * p["entry"] for p in open_pos)
            cost = t["entry"] * (1 + SLIP) * (1 + FEE)
            qty = int(min(eq / slots, cash) // cost)
            if qty < 1:
                skipped += 1
                continue
            cash -= qty * cost
            open_pos.append(dict(t, qty=qty))
            done += 1
            eq_curve.append(cash + sum(p["qty"] * p["entry"] for p in open_pos))
        for p in open_pos:
            cash += p["qty"] * p["exit"] * (1 - SLIP) * (1 - FEE)
        eq_curve.append(cash)
        peak, mdd = eq_curve[0], 0.0
        for v in eq_curve:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        return f"100달러 → {cash:.1f}달러 ({(cash / capital - 1) * 100:+.0f}%), MDD {mdd * 100:.0f}%, 거래 {done}건" + (f" (돈 모자라 못 산 {skipped}건)" if skipped else "")

    def months(rs):
        ms = sorted({x["in"][:6] for x in rs})
        return " / ".join(f"{m[4:]}월 {len(v)}건 {sum(x['ret'] for x in v) / len(v):+.2f}%" for m in ms for v in [[x for x in rs if x['in'][:6] == m]])

    UNI = {"전체": lambda x: True, "유동(정규장 거래대금 중간 500만$↑)": lambda x: x["liq"], "전날 거래대금 상위 20": lambda x: x["rank"] <= 20,
           "상위 10": lambda x: x["rank"] <= 10, "상위 5": lambda x: x["rank"] <= 5, "상위 3": lambda x: x["rank"] <= 3}
    for hold in (5, 3, 10, 0):
        rs = trades(hold=hold)
        print(f"== {'매수 뒤 ' + str(hold) + '봉째 정리' if hold else '봉 수 제한 없음(중심선 이탈만)'} (횡보 20봉·K 0.5·앞선 방향 둘 다, 1~10달러)")
        for name, f in UNI.items():
            v = [x for x in rs if f(x)]
            print(f"  {name}: {stat(v)}")
            if hold == 5 and name in ("전체", "상위 10", "상위 5", "상위 3"):
                print(f"      {account(v)}\n      월별 {months(v)}")
    print("\n== 5봉 정리, 조건별 (전체 / 상위 10)")
    for label, kw in (("앞선 움직임이 하락", {"direction": "down"}), ("앞선 움직임이 상승", {"direction": "up"}), ("K 1.0", {"K": 1.0}),
                      ("K 1.5", {"K": 1.5}), ("횡보 30봉", {"L": 30}), ("5달러 이하", {"pmax": 5.0}), ("5~10달러", {"pmin": 5.0}),
                      ("가격 제한 없음(목록 전체)", {"pmax": 1e9, "pmin": 0})):
        rs = trades(hold=5, **kw)
        print(f"  {label}: {stat(rs)} | 상위 10: {stat([x for x in rs if x['rank'] <= 10]).split(' (')[0]}")
    print("\n== 5봉 정리, 매수 시각별 (매수하는 봉이 끝나는 시각, 미국 동부) — 전체")
    rs = trades(hold=5)
    for hh, nm in (("04", "0~4시 (밤, 주간거래)"), ("08", "4~8시 (프리마켓)"), ("12", "8~12시 (프리마켓 8시 시가 매수)"),
                   ("16", "12~16시 (정규장)"), ("20", "16~20시 (애프터마켓)"), ("24", "20~24시 (애프터마켓 뒤)")):
        print(f"  {nm}: {stat([x for x in rs if x['in'][8:10] == hh]).split(' (')[0]}")
    reg = [x for x in rs if x["in"][8:10] in ("12", "16")]
    print(f"  정규장 근처에서 매수한 것만(8~12시·12~16시 봉): {stat(reg)}\n      {account(reg)}")
    print("\n== 비용 민감도 (5봉 정리, 전체): 슬리피지 한쪽 0.1 → 0.3 / 0.5%")
    for extra in (0.4, 0.8):
        v = [dict(x, ret=x["ret"] - extra) for x in rs]
        print(f"  왕복 비용 +{extra}%p: {stat(v).split(' (')[0]}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "download":
        download()
    else:
        main()
