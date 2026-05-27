#!/usr/bin/env python3
"""
ACCOUNT ACTIVITY SCAN — full episode-by-episode trade history for one address.

Reconstructs every perp position episode from the signed fill record (flat→position→flat/flip),
so we can SEE whether the account actively OPENS NEW trades and turns them over, vs. just sitting in
one hodled perp. Prints: per-episode open/close time + hold + dir + realized PnL, a recency timeline
(trades per week), current open book + how long each has been held, and hold-cadence tags.

Usage: ACT_ADDR=0x... ACT_DAYS=400 python scripts/account_activity.py
FREE — HL public API, read-only.
"""
import sys, os, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
ADDR=os.getenv("ACT_ADDR","").lower()
DAYS=int(os.getenv("ACT_DAYS","400"))
_last=[0.0]
def post(t,**k):
    for _ in range(5):
        try:
            d=time.time()-_last[0]
            if d<0.35: time.sleep(0.35-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: time.sleep(1.2)
    return None

def fills(addr):
    out=[]; end=int(time.time()*1000); cur=end-DAYS*86400*1000
    for _ in range(30):
        j=post("userFillsByTime",user=addr,startTime=cur,endTime=end)
        if not isinstance(j,list) or not j: break
        out+=j
        if len(j)<2000: break
        cur=max(f["time"] for f in j)+1
    seen=set(); d=[]
    for f in out:
        k=(f["time"],f.get("oid"),f.get("tid"))
        if k in seen: continue
        seen.add(k); d.append(f)
    d.sort(key=lambda f:f["time"]); return d

def ts(ms): return dt.datetime.fromtimestamp(ms/1000,dt.UTC).strftime("%Y-%m-%d %H:%M")

def fmt_hold(h):
    if h<1: return f"{h*60:.0f}m"
    if h<24: return f"{h:.1f}h"
    return f"{h/24:.1f}d"

def main():
    fl=fills(ADDR)
    print(f"\n===== ACTIVITY SCAN  {ADDR}  ({len(fl)} fills, last {DAYS}d) =====")
    if len(fl)<5:
        print("insufficient fills"); return
    run=defaultdict(float); avg=defaultdict(float); t_open=defaultdict(float)
    epnl=defaultdict(float); EPS=1e-9; episodes=[]; nadds=defaultdict(int)
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); px=float(f["px"]); pnl=float(f.get("closedPnl",0))
        d=sz if f.get("side")=="B" else -sz; t=f["time"]
        prev=run[c]; new=round(prev+d,8); epnl[c]+=pnl
        if abs(prev)<EPS and abs(new)>=EPS:                        # fresh open
            t_open[c]=t; avg[c]=px; nadds[c]=0
        elif abs(new)>abs(prev) and (prev>0)==(new>0):             # add same dir
            avg[c]=(avg[c]*abs(prev)+px*sz)/abs(new); nadds[c]+=1
        if abs(prev)>=EPS and (abs(new)<EPS or (prev>0)!=(new>0)): # close or flip
            episodes.append(dict(coin=c, dir="L" if prev>0 else "S", opened=t_open[c], closed=t,
                hold_h=(t-t_open[c])/3.6e6, pnl=round(epnl[c]), adds=nadds[c], avg=round(avg[c],4)))
            epnl[c]=0.0
            if (prev>0)!=(new>0) and abs(new)>=EPS:                # flip → new episode opens
                t_open[c]=t; avg[c]=px; nadds[c]=0
        run[c]=new

    span=(fl[-1]["time"]-fl[0]["time"])/86400000 or 1
    now=time.time()*1000
    # recency timeline: episodes opened per ISO-week
    wk=defaultdict(int)
    for e in episodes: wk[dt.datetime.fromtimestamp(e["opened"]/1000,dt.UTC).strftime("%G-W%V")]+=1
    holds=[e["hold_h"] for e in episodes]
    intraday=sum(1 for h in holds if h<24); multiday=sum(1 for h in holds if h>=24)
    print(f"span {span:.0f}d · {len(episodes)} CLOSED episodes · "
          f"{len(episodes)/span*7:.1f} new-opens/week · last fill {(now-fl[-1]['time'])/86400000:.1f}d ago")
    if holds:
        hs=sorted(holds); med=hs[len(hs)//2]
        print(f"hold: median {fmt_hold(med)} · intraday {intraday} ({intraday/len(holds)*100:.0f}%) · "
              f"multiday {multiday} ({multiday/len(holds)*100:.0f}%) · "
              f"shortest {fmt_hold(hs[0])} · longest {fmt_hold(hs[-1])}")

    print("\n-- new-opens per week (recent 14) --")
    for w in sorted(wk)[-14:]: print(f"  {w}: {'█'*wk[w]} {wk[w]}")

    print("\n-- last 25 closed episodes (newest first) --")
    print(f"  {'opened':16} {'closed':16} {'hold':>7}  {'coin':6} {'dir':3} {'adds':>4} {'realized':>10}")
    for e in episodes[-25:][::-1]:
        pnl_s=f"${e['pnl']:+,}"
        print(f"  {ts(e['opened']):16} {ts(e['closed']):16} {fmt_hold(e['hold_h']):>7}  "
              f"{e['coin']:6} {e['dir']:3} {e['adds']:>4} {pnl_s:>10}")

    # current open book
    cs=post("clearinghouseState",user=ADDR) or {}; mids=post("allMids") or {}
    print("\n-- CURRENTLY OPEN (held now) --")
    openpos=cs.get("assetPositions",[])
    if not openpos: print("  flat")
    for ap in openpos:
        p=ap["position"]; c=p["coin"]; szi=float(p["szi"])
        mid=float(mids.get(c,0) or p.get("entryPx") or 0)
        held_h=(now-t_open[c])/3.6e6 if t_open.get(c) else 0
        print(f"  {c:6} {'L' if szi>0 else 'S'} ${abs(szi*mid):>9,.0f} @{p['leverage'].get('value')}x  "
              f"entry {p.get('entryPx')}  uPnL ${float(p['unrealizedPnl']):+,.0f}  held ~{fmt_hold(held_h)}")
    print(f"\n  => verdict: {'ACTIVE fresh-opener (turns positions over)' if len(episodes)/span*7>=0.5 and multiday/max(len(holds),1)<0.9 else 'mostly HOLDS (low turnover)'}")

if __name__=="__main__": main()
