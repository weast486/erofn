"""15:10 에는 20일 최고 종가를 넘었는데 종가는 못 넘은 날(엘앤씨바이오 사례) 분석."""
import sys, os, csv, json; sys.path.insert(0, '.')
from pathlib import Path
from datetime import date
from collections import defaultdict
from tossbot import backtest as bt
SL, TP, N = 4.7, 20.0, 20
data = bt.load_cache(Path('data/ohlcv_long'))
full = {f[:-4] for f in os.listdir('data/minute')}
tail = {f[:-5] for f in os.listdir('data/minute_tail')}

def at1510(sym, b):
    """(15:10 가격, 15:10 까지 고가, 15:10 까지 거래대금) 또는 None"""
    k = f"{sym}_{b.day}"
    if k in full:
        rows = list(csv.DictReader(open(f'data/minute/{k}.csv', encoding='utf-8')))
        pre = [r for r in rows if r['time'] <= '15:10']
        if len(pre) < 200 or not any('15:0' in r['time'] or r['time'] == '15:10' for r in pre): return None
        return (float(pre[-1]['close']), max(float(r['high']) for r in pre),
                sum(float(r['close']) * float(r['volume']) for r in pre))
    if k in tail:
        rows = json.load(open(f'data/minute_tail/{k}.json'))
        pre = [r for r in rows if r['t'] <= '15:10']
        post = [r for r in rows if '15:10' < r['t']]
        if not pre or pre[-1]['t'] < '15:05': return None
        return (pre[-1]['c'], b.high, b.close * b.volume - sum(r['c'] * r['v'] for r in post))
    return None

def outcome(B, i, entry):
    e = entry * 1.001
    stop, tp = entry * (1 - SL / 100), entry * (1 + TP / 100)
    for j in range(i + 1, len(B)):
        x = B[j]
        if x.low <= stop:
            px = min(stop, x.open) * 0.999; return (px * (1 - 0.0020 - 0.00015) / (e * 1.00015) - 1) * 100, 'stop', j - i
        if x.high >= tp:
            px = max(tp, x.open); return (px * (1 - 0.0020 - 0.00015) / (e * 1.00015) - 1) * 100, 'tp', j - i
    return (B[-1].close * (1 - 0.00215) / (e * 1.00015) - 1) * 100, 'open', len(B) - 1 - i

rows = []
for sym, (name, B) in data.items():
    ch = bt.new_high_flags([x.close for x in B], N)
    for i in range(40, len(B)):
        b = B[i]
        if b.day < date(2023, 10, 1): continue
        ref = max(x.close for x in B[i - N:i])
        if b.high <= ref or any(ch[i - N:i]): continue
        if sum(x.close * x.volume for x in B[i - 20:i]) / 20 < 3e9: continue
        pc = B[i - 1].close
        m = at1510(sym, b)
        if m is None: continue
        p, hi, amt = m
        live = p > ref and p <= 100000 and amt >= 2e10 and p < pc * 1.295 and hi < pc * 1.295
        bk = b.close > ref and b.close <= 100000 and b.close * b.volume >= 2e10 and b.close < pc * 1.295 and b.high < pc * 1.295
        if not (live or bk): continue
        grp = 'both' if live and bk else ('fake' if live and b.close <= ref else ('live_only' if live else 'missed'))
        r, why, days = outcome(B, i, p if live else b.close)
        rc = outcome(B, i, b.close)[0]
        rows.append(dict(sym=sym, name=name, day=str(b.day), grp=grp, ref=ref, p1510=p, close=b.close,
                         over=round((p / ref - 1) * 100, 2), slip=round((b.close / p - 1) * 100, 2), ret=round(r, 2), retc=round(rc, 2), why=why, days=days))
json.dump(rows, open(sys.argv[1], 'w', encoding='utf-8'), ensure_ascii=False)
def show(title, rs):
    if not rs: print(title, 0); return
    n = len(rs); w = sum(r['why'] == 'tp' for r in rs); s = sum(r['why'] == 'stop' for r in rs)
    print(f"{title:28s} {n:5d}건  거래당 {sum(r['ret'] for r in rs)/n:+.2f}%  익절 {w/n*100:4.1f}%  손절 {s/n*100:4.1f}%  당일종가까지 {sum(r['slip'] for r in rs)/n:+.2f}%")
for g in ('both', 'fake', 'live_only', 'missed'):
    show(g, [r for r in rows if r['grp'] == g])
lv = [r for r in rows if r['grp'] in ('both', 'fake', 'live_only')]
show('실전 전체(15:10 기준)', lv)

print('--- 연도별')
for y in ('2023', '2024', '2025', '2026'):
    for g in ('both', 'fake', 'live_only', 'missed'):
        show(f'{y} {g}', [r for r in rows if r['grp'] == g and r['day'][:4] == y])
print('--- 실전(15:10 기준 매수) vs 백테스트(종가 기준 매수), 종목 수 제한 없이 신호 전부')
for y in ('2023', '2024', '2025', '2026', '20'):
    L = [r['ret'] for r in rows if r['grp'] != 'missed' and r['day'].startswith(y)]
    K = [r['retc'] for r in rows if r['grp'] in ('both', 'missed') and r['day'].startswith(y)]
    print(y, f"실전 {len(L)}건 {sum(L)/len(L):+.2f}%   백테스트 {len(K)}건 {sum(K)/len(K):+.2f}%")
print('--- fake: 15:10 돌파 폭별')
fk = [r for r in rows if r['grp'] == 'fake']
for lo, hi in ((0, 0.5), (0.5, 1), (1, 2), (2, 99)):
    show(f'fake {lo}~{hi}%', [r for r in fk if lo < r['over'] <= hi])
print('--- 실전 매수 전체: 15:10 돌파 폭별 (가짜 비율)')
lv = [r for r in rows if r['grp'] != 'missed']
for lo, hi in ((0, 0.3), (0.3, 0.5), (0.5, 1), (1, 2), (2, 3), (3, 5), (5, 10), (10, 99)):
    rs = [r for r in lv if lo < r['over'] <= hi]
    f = sum(r['grp'] == 'fake' for r in rs)
    show(f'{lo}~{hi}% (가짜 {f/max(len(rs),1)*100:.0f}%)', rs)
print('--- missed: 종가 돌파 폭별')
for lo, hi in ((0, 1), (1, 2), (2, 5), (5, 99)):
    show(f'missed {lo}~{hi}%', [r for r in rows if r['grp'] == 'missed' and lo < (r['close']/r['ref']-1)*100 <= hi])
import collections
print(collections.Counter(r['grp'] for r in rows))
