import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from datetime import date
from pathlib import Path
import statistics as st
from tossbot.backtest import load_cache, run_envelope, load_top_universe, BacktestSettings, EnvelopeSettings, SELL_TAX_BY_YEAR
data = load_cache(Path('data/ohlcv_long'))
uni, caps = load_top_universe(Path('data/marcap/data'), '2022-01-01')
def mdd(r):
    pk, w = 0, 0
    for _, v in r.equity_curve:
        pk = max(pk, v); w = min(w, v / pk - 1)
    return w
for mode in ('near', 'below'):
    e = EnvelopeSettings(mode=mode, universe=uni, rank=caps)
    s = BacktestSettings(initial_cash=10_000_000)
    print(f'== mode {mode}')
    for y in (2023, 2024, 2025, 2026):
        r = run_envelope(data, date(y, 1, 1), date(y, 12, 31), s, e)
        tr = [t for t in r.trades if t.exit_date]
        w = sum(t.pnl > 0 for t in tr)
        rets = [t.exit_price / t.entry_price - 1 for t in tr]
        print(f'{y}: 수익률 {r.total_return*100:+.1f}% MDD {mdd(r)*100:.1f}% 매수 {len(r.trades)} 승률 {w/max(1,len(tr))*100:.0f}% 평균 {st.mean(rets)*100 if rets else 0:+.2f}% 매수일수 {len({t.entry_date for t in r.trades})}')
    r = run_envelope(data, date(2023, 1, 1), date(2026, 12, 31), s, e)
    print(f'연속: {r.total_return*100:+.1f}% MDD {mdd(r)*100:.1f}% 매수 {len(r.trades)}')
    import collections
    by = collections.Counter(t.entry_date for t in r.trades)
    print('매수 많은 날', by.most_common(8))
    print('종목 예', [ (t.name, str(t.entry_date), round((t.exit_price/t.entry_price-1)*100,1)) for t in r.trades[:12]])
