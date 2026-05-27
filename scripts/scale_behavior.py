#!/usr/bin/env python3
"""
SCALE BEHAVIOR — does he one-shot positions, or build/scale (add+trim) and hold them?

Per coin, reconstructs every episode (flat→…→flat/flip) and counts WITHIN each episode:
  adds   = fills that GROW the position in its direction (scale-in)
  trims  = fills that SHRINK it without closing (partial scale-out)
  hold   = open→close duration
Then classifies each coin: ONE-SHOT (open once, close once), SCALE-IN (adds, rare trims),
SCALE-IN/OUT (both — actively manages), HODL (very long holds, few episodes). This tells us whether
a fresh-entry direction-copy is faithful, or whether we must mirror his scaling proportionally.

Usage: SC_ADDR=0x.. SC_DAYS=60 python scripts/scale_behavior.py
FREE — HL public API, read-only.
"""
import sys, os, time, statistics as st, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
ADDR=os.getenv("SC_ADDR","").lower()
DAYS=int(os.getenv("SC_DAYS","60"))
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","NEAR","kPEPE"}
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

def fmt_hold(h):
    if h<1: return f"{h*60:.0f}m"
    if h<24: return f"{h:.1f}h"
    return f"{h/24:.1f}d"

def main():
    fl=fills(ADDR)
    print(f"\n===== SCALE BEHAVIOR  {ADDR[:12]}…  ({len(fl)} fills, last {DAYS}d) =====")
    if len(fl)<20: print("insufficient"); return
    run=defaultdict(float); EPS=1e-9
    cur_adds=defaultdict(int); cur_trims=defaultdict(int); t0=defaultdict(float)
    ep=defaultdict(list)                                          # coin -> [(hold_h, adds, trims)]
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); d=sz if f.get("side")=="B" else -sz; t=f["time"]
        prev=run[c]; new=round(prev+d,8)
        if abs(prev)<EPS and abs(new)>=EPS:                       # fresh open
            t0[c]=t; cur_adds[c]=0; cur_trims[c]=0
        elif abs(new)>abs(prev)+EPS and (prev>0)==(new>0):        # add (grow same dir)
            cur_adds[c]+=1
        elif abs(new)<abs(prev)-EPS and abs(new)>EPS and (prev>0)==(new>0):  # trim (shrink, not flat/flip)
            cur_trims[c]+=1
        if abs(prev)>=EPS and (abs(new)<EPS or (prev>0)!=(new>0)):# close/flip
            ep[c].append(((t-t0[c])/3.6e6, cur_adds[c], cur_trims[c]))
            if (prev>0)!=(new>0) and abs(new)>=EPS:
                t0[c]=t; cur_adds[c]=0; cur_trims[c]=0
        run[c]=new
    # current open: how long held so far
    cs=post("clearinghouseState",user=ADDR) or {}; open_now={}
    now=time.time()*1000
    for ap in cs.get("assetPositions",[]):
        c=ap["position"]["coin"]; open_now[c]=(now-t0[c])/3.6e6 if t0.get(c) else 0

    def classify(eps, held_h):
        n=len(eps);
        if n==0: return "open-only"
        ah=st.mean([e[0] for e in eps]); aa=st.mean([e[1] for e in eps]); at=st.mean([e[2] for e in eps])
        if ah>=24*7 or (n<=2 and held_h>24*5): return "HODL (long-hold)"
        if aa>=1.5 and at>=1.0: return "SCALE-IN/OUT (manages)"
        if aa>=1.0: return "SCALE-IN (adds, holds)"
        if aa<0.4 and at<0.4: return "ONE-SHOT"
        return "light-scale"

    print(f"\n  {'coin':9} {'liq?':5} {'eps':>4} {'med hold':>9} {'avg adds':>9} {'avg trims':>10} {'held now':>9}  class")
    coins=sorted(ep, key=lambda c:-len(ep[c]))
    for c in coins:
        eps=ep[c]
        if len(eps)<1 and c not in open_now: continue
        holds=[e[0] for e in eps]; med=sorted(holds)[len(holds)//2] if holds else 0
        aa=st.mean([e[1] for e in eps]) if eps else 0; at=st.mean([e[2] for e in eps]) if eps else 0
        held=open_now.get(c,0); thin="THIN" if (c not in LIQUID and not c.startswith("k")) else "liq"
        hn=fmt_hold(held) if c in open_now else "-"
        print(f"  {c:9} {thin:5} {len(eps):>4} {fmt_hold(med):>9} {aa:>9.1f} {at:>10.1f} {hn:>9}  {classify(eps,held)}")

    # aggregate
    all_eps=[e for c in ep for e in ep[c]]
    if all_eps:
        aa=st.mean([e[1] for e in all_eps]); at=st.mean([e[2] for e in all_eps])
        oneshot=sum(1 for e in all_eps if e[1]<1 and e[2]<1)
        print(f"\n  AGGREGATE: {len(all_eps)} episodes · avg {aa:.1f} adds + {at:.1f} trims per episode · "
              f"{oneshot}/{len(all_eps)} ({oneshot/len(all_eps)*100:.0f}%) were ONE-SHOT (no scaling)")
        print(f"  => {'He BUILDS/SCALES most positions — faithful copy needs proportional add/trim mirroring' if aa>=1.0 else 'Mostly one-shot — fresh-entry direction-copy is faithful enough'}")
        print(f"  current open basket: {', '.join(f'{c} {fmt_hold(h)}' for c,h in sorted(open_now.items(), key=lambda x:-x[1]))}")

if __name__=="__main__": main()
