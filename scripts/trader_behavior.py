#!/usr/bin/env python3
"""
TRADER BEHAVIOR — how does this account actually operate? Sizing, timing, and is-it-an-algo.

Three questions, from the full signed-fill record:
  SIZING   — per coin: peak position notional (|running net| × px), avg, leverage; flags THIN alts
             vs liquid majors so we see how big he goes on illiquid names (our copy-slippage risk).
  TIMING   — fills by hour-of-UTC + day-of-week → when he's most active; minute-of-hour clustering
             (fixed-schedule cron signature) and SIMULTANEOUS multi-coin opens (basket-rebalance).
  ALGO?    — 24/7 hour coverage, overnight (0-6 UTC) & weekend fill share, inter-fill interval
             regularity (CV), round-number sizing, taker fraction → a weighted bot/human verdict.

Usage: BEH_ADDR=0x.. BEH_DAYS=60 python scripts/trader_behavior.py
FREE — HL public API, read-only.
"""
import sys, os, time, statistics as st, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
ADDR=os.getenv("BEH_ADDR","").lower()
DAYS=int(os.getenv("BEH_DAYS","60"))
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

def bar(n, mx, w=40):
    return "█"*int(round(n/mx*w)) if mx else ""

def main():
    fl=fills(ADDR)
    print(f"\n===== TRADER BEHAVIOR  {ADDR}  ({len(fl)} fills, last {DAYS}d) =====")
    if len(fl)<20: print("insufficient fills"); return

    # ── SIZING per coin (peak notional from running net) ──
    run=defaultdict(float); peak=defaultdict(float); notion_sum=defaultdict(float)
    fillct=defaultdict(int); lev_seen=defaultdict(set); EPS=1e-9
    hours=defaultdict(int); dow=defaultdict(int); minute=defaultdict(int)
    weekend=overnight=0; gaps=[]; prev_t=None; crossed=maker=0; round_sz=0
    opens_at=defaultdict(set)                                   # minute_ts -> set(coins opened) for basket detection
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); px=float(f["px"]); t=f["time"]
        d=sz if f.get("side")=="B" else -sz
        D=dt.datetime.fromtimestamp(t/1000,dt.UTC)
        hours[D.hour]+=1; dow[D.weekday()]+=1; minute[D.minute]+=1
        if D.weekday()>=5: weekend+=1
        if 0<=D.hour<6: overnight+=1
        if prev_t is not None: gaps.append((t-prev_t)/1000)
        prev_t=t
        if f.get("crossed") is True: crossed+=1
        elif f.get("crossed") is False: maker+=1
        if abs(sz*10-round(sz*10))<1e-6 and sz>=1: round_sz+=1   # crude round-size check
        prev=run[c]; new=round(prev+d,8)
        if abs(prev)<EPS and abs(new)>=EPS: opens_at[t//60000].add(c)
        run[c]=new; peak[c]=max(peak[c], abs(new)*px)
        notion_sum[c]+=sz*px; fillct[c]+=1
        if "startPosition" in f: pass

    # current leverage per coin
    cs=post("clearinghouseState",user=ADDR) or {}
    cur_lev={}; cur_notional={}
    mids=post("allMids") or {}
    for ap in cs.get("assetPositions",[]):
        p=ap["position"]; c=p["coin"]; szi=float(p["szi"])
        cur_lev[c]=p["leverage"].get("value"); cur_notional[c]=abs(szi*float(mids.get(c,0) or p.get("entryPx") or 0))

    print("\n-- SIZING per coin (peak position notional held) --")
    print(f"  {'coin':8} {'liq?':5} {'peak $notional':>14} {'fills':>6} {'now $':>10} {'lev':>4}")
    thin_peaks=[]; liq_peaks=[]
    for c in sorted(peak, key=lambda x:-peak[x]):
        thin = c not in LIQUID and not c.startswith("k")
        (thin_peaks if thin else liq_peaks).append(peak[c])
        tag="THIN" if thin else "liq"
        nown=cur_notional.get(c,0); lev=cur_lev.get(c,"")
        if peak[c]>=500:
            pk=f"${peak[c]:,.0f}"; nw=f"${nown:,.0f}" if nown else "-"; lv=f"{lev}x" if lev else "-"
            print(f"  {c:8} {tag:5} {pk:>14} {fillct[c]:>6} {nw:>10} {lv:>4}")
    if thin_peaks:
        print(f"\n  THIN alts: {len(thin_peaks)} coins · peak notional avg ${st.mean(thin_peaks):,.0f} · max ${max(thin_peaks):,.0f}")
    if liq_peaks:
        print(f"  Liquid majors: {len(liq_peaks)} coins · peak notional avg ${st.mean(liq_peaks):,.0f} · max ${max(liq_peaks):,.0f}")

    # ── TIMING ──
    n=len(fl); mxh=max(hours.values())
    print("\n-- ACTIVITY by hour-of-day (UTC) --")
    for h in range(24):
        print(f"  {h:02d}:00  {bar(hours[h],mxh):40} {hours[h]}")
    peak_hours=sorted(hours, key=lambda x:-hours[x])[:4]
    print(f"  most active hours (UTC): {', '.join('%02d:00'%h for h in sorted(peak_hours))}")
    dn=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]; mxd=max(dow.values())
    print("\n-- by day-of-week --")
    for i in range(7): print(f"  {dn[i]}  {bar(dow[i],mxd):40} {dow[i]}")
    # minute-of-hour clustering (cron signature)
    top_min=sorted(minute, key=lambda x:-minute[x])[:5]
    print(f"\n-- minute-of-hour concentration (cron signature) --")
    print(f"  top minutes: {', '.join(':%02d (%d)'%(m,minute[m]) for m in top_min)}  | uniform≈{n/60:.0f}/min")
    baskets=[(t,cs_) for t,cs_ in opens_at.items() if len(cs_)>=3]
    print(f"  simultaneous multi-coin opens (≥3 coins same minute): {len(baskets)} events "
          f"→ {'BASKET-REBALANCE algo' if baskets else 'none'}")
    if baskets:
        for t,cs_ in sorted(baskets)[-4:]:
            print(f"     {dt.datetime.fromtimestamp(t*60,dt.UTC):%m-%d %H:%M}  {sorted(cs_)}")

    # ── ALGO verdict ──
    hour_cov=len(hours)/24; overn=overnight/n; wknd=weekend/n
    icv=(st.pstdev(gaps)/st.mean(gaps)) if len(gaps)>1 and st.mean(gaps)>0 else 0
    taker=crossed/max(crossed+maker,1)
    print("\n-- IS IT AN ALGO? --")
    print(f"  24/7 hour-coverage : {hour_cov*100:.0f}% of hours seen")
    print(f"  overnight (0-6 UTC): {overn*100:.0f}% of fills")
    print(f"  weekend            : {wknd*100:.0f}% of fills")
    print(f"  inter-fill CV      : {icv:.2f} (lower=more regular/scheduled)")
    print(f"  taker fraction     : {taker*100:.0f}% (high=crosses spread, momentum; low=maker/MM)")
    print(f"  basket rebalances  : {len(baskets)}")
    score=sum([hour_cov>=0.8, overn>=0.12, wknd>=0.15, len(baskets)>=3, n>=300])
    verdict=("ALGORITHMIC / automated (24-7, no human sleep cycle, programmatic baskets)" if score>=4
             else "likely automated" if score>=3 else "discretionary / human-paced")
    print(f"\n  => VERDICT: {verdict}  [{score}/5 algo signals]")

if __name__=="__main__": main()
