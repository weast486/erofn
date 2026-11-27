"""키움 단타 A+B 거래에 '전일 종가가 5분봉 50선 아래면 제외' 조건 붙여 비교."""
import csv,sys
from pathlib import Path
from datetime import date
from collections import defaultdict
sys.path.insert(0,r"E:\erofn")
from tossbot.backtest import load_cache, load_minute
S=Path(sys.argv[1]); N=int(sys.argv[2]) if len(sys.argv)>2 else 50
cache=load_cache(Path("data/ohlcv_long")); md=Path("data/minute")
def five(bars):
    b5={}
    for b in bars:
        hh,mm=map(int,b.t.split(":")); m=hh*60+mm-540-1
        if m<0 or m>=390: continue
        b5[m//5]=b.close  # 끝 시각 기준, 마지막 종가
    return [b5[k] for k in sorted(b5)]
rows=[]; miss=0
for kind in ("A","B"):
    for r in csv.DictReader(open(S/f"{kind}.csv",encoding="utf-8")):
        bars=cache[r["code"]][1]; td=date.fromisoformat(r["trade_day"])
        i=sum(1 for b in bars if b.day<td)
        if i>=len(bars) or bars[i].day!=td: miss+=1; continue
        pd=bars[i-1].day; pclose=bars[i-1].close
        mb=load_minute(md,r["code"],pd)
        st=None
        if mb:
            c=five(mb)
            if len(c)<N:  # 앞날 분봉으로 채움
                mb2=load_minute(md,r["code"],bars[i-2].day)
                if mb2: c=five(mb2)+c
            if len(c)>=N: st= pclose < sum(c[-N:])/N
        rows.append(dict(kind=kind,day=td,code=r["code"],t=r["entry_time"],ret=float(r["ret_pct"]),below=st))
print("거래",len(rows),"판정 못함",sum(r["below"] is None for r in rows),"일봉없음",miss)
def stat(xs,lab):
    if not xs: print(f"{lab}: 0건"); return
    n=len(xs); w=sum(x["ret"]>0 for x in xs); a=sum(x["ret"] for x in xs)/n
    s=sorted(x["ret"] for x in xs); a5=sum(s[:-5])/(n-5) if n>5 else float("nan")
    yr=defaultdict(list)
    for x in xs: yr[x["day"].year].append(x["ret"])
    ys=" / ".join(f"{y} {len(v)}건 {sum(v)/len(v):+.2f}" for y,v in sorted(yr.items()))
    print(f"{lab}: {n}건 승률 {w/n*100:.0f}% 거래당 {a:+.2f}% (5건 빼면 {a5:+.2f}%) | {ys}")
def acct(xs,lab):
    byd=defaultdict(list)
    for x in xs: byd[x["day"]].append(x)
    eq=1.0; peak=1.0; mdd=0; yeq=defaultdict(lambda:[None,None])
    for d in sorted(byd):
        seen=set(); pick=[]
        for x in sorted(byd[d],key=lambda x:(x["t"],x["kind"])):
            if x["code"] in seen: continue
            seen.add(x["code"]); pick.append(x)
        pick=pick[:3]
        if yeq[d.year][0] is None: yeq[d.year][0]=eq
        eq*=1+sum(p["ret"] for p in pick)/100/3
        yeq[d.year][1]=eq; peak=max(peak,eq); mdd=min(mdd,eq/peak-1)
    ys=" / ".join(f"{y} {v[1]/v[0]*100-100:+.0f}%" for y,v in sorted(yeq.items()))
    print(f"  계좌(3종목 x 1/3): {eq*100-100:+.0f}% MDD {mdd*100:.1f}% | {ys}")
ok=[r for r in rows if r["below"] is not None]
for kind in ("A","B","A+B"):
    sub=[r for r in ok if kind=="A+B" or r["kind"]==kind]
    print(f"== {kind} (판정 가능한 거래만)")
    stat(sub,"  지금(조건 없음)"); 
    if kind=="A+B": acct(sub,"")
    keep=[r for r in sub if not r["below"]]
    stat(keep,f"  전일 종가 ≥ 5분 {N}선만"); 
    if kind=="A+B": acct(keep,"")
    stat([r for r in sub if r["below"]],f"  빠지는 거래(종가 < {N}선)")
