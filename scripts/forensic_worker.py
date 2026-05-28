#!/usr/bin/env python3
"""
FORENSIC WORKER — sharded, resumable, SELF-CONTAINED trust-gate over a big address pool.

Runs the consistency-first trust gate (martingale / drawdown / concentration / one-trade /
sporadic / stale) on a SHARD of an address list. Designed to fan out across MULTIPLE MACHINES
(each its own IP) so the full pool gets deep-screened without 429-ing any single IP.

  *** FREE — HL public API ONLY. ZERO Nansen / Etherscan calls (those are reserved for manual
      deep-vet of the final survivors). ***

Portable: needs only python3 + `requests`. Copy this file + the address list to any machine.

Run (one shard per machine):
  POOL_FILE=green_pool.txt SHARD_N=4 SHARD_IDX=0 MIN_INTERVAL=0.6 python3 forensic_worker.py
  (machine 2 → SHARD_IDX=1, machine 3 → SHARD_IDX=2, ...; all share the same SHARD_N)

Writes fullscan/shard_{IDX}.jsonl (one json/account) + done_{IDX}.json (resume). Merge separately.
"""
import os, sys, json, time, math, statistics as st, datetime as dt
from collections import defaultdict
import requests

API="https://api.hyperliquid.xyz/info"
POOL=os.getenv("POOL_FILE","green_pool.txt")
N=int(os.getenv("SHARD_N","1")); IDX=int(os.getenv("SHARD_IDX","0"))
MIN_INT=float(os.getenv("MIN_INTERVAL","0.6"))
PAGES=int(os.getenv("TRIAGE_PAGES","6"))      # fewer than the 30-page full pass — enough for the gates
OUTDIR=os.getenv("OUTDIR","fullscan")
os.makedirs(OUTDIR, exist_ok=True)
RES=f"{OUTDIR}/shard_{IDX}.jsonl"; DONE=f"{OUTDIR}/done_{IDX}.json"
_last=[0.0]

def post(t,**k):
    for _ in range(5):
        try:
            d=time.time()-_last[0]
            if d<MIN_INT: time.sleep(MIN_INT-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: time.sleep(1.5)
    return None

def fills(addr, days=400):
    out=[]; end=int(time.time()*1000); cur=end-days*86400*1000
    for _ in range(PAGES):
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

def forensic(addr):
    fl=fills(addr)
    if len(fl)<30: return None
    now=time.time()*1000; span=(fl[-1]["time"]-fl[0]["time"])/86400000 or 1
    run=defaultdict(float); avg=defaultdict(float); seg=defaultdict(float)
    EPS=1e-9; closes=[]; adds=0; adds_down=0; daily=defaultdict(float)
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); px=float(f["px"]); pnl=float(f.get("closedPnl",0))
        d=sz if f.get("side")=="B" else -sz
        D=dt.datetime.fromtimestamp(f["time"]/1000,dt.UTC); daily[D.strftime("%Y-%m-%d")]+=pnl
        prev=run[c]; new=round(prev+d,8); seg[c]+=pnl
        if abs(new)>abs(prev) and abs(prev)>EPS and (prev>0)==(new>0):
            adds+=1
            if (prev>0 and px<avg[c]) or (prev<0 and px>avg[c]): adds_down+=1
        if abs(new)>abs(prev):
            avg[c]=(avg[c]*abs(prev)+px*sz)/abs(new) if abs(new)>0 else px
        if abs(prev)>=EPS and (abs(new)<EPS or (prev>0)!=(new>0)):
            closes.append(round(seg[c])); seg[c]=0.0
        if abs(new)<EPS: avg[c]=0.0
        run[c]=new
    if len(closes)<8: return None
    realized=sum(closes); tot=realized if realized>0 else 1
    # LOSS-HIDER check: current open unrealized PnL (closed-WR hides traders sitting on buried losers)
    cs=post("clearinghouseState",user=addr) or {}
    open_upnl=sum(float(ap["position"]["unrealizedPnl"]) for ap in cs.get("assetPositions",[]))
    wins=[x for x in closes if x>0]; losses=[x for x in closes if x<0]
    biggest=max(closes,key=abs) if closes else 0
    cum=peak=mdd=0.0
    for x in closes: cum+=x; peak=max(peak,cum); mdd=min(mdd,cum-peak)
    dvals=list(daily.values()); worst_day=min(dvals) if dvals else 0
    pos_day=sum(1 for v in dvals if v>0)/max(len(dvals),1)
    sharpe=(st.mean(dvals)/st.pstdev(dvals)) if len(dvals)>1 and st.pstdev(dvals)>0 else 0
    th=len(closes)//3 or 1
    wr_thirds=[round(sum(1 for x in g if x>0)/max(len(g),1)*100) for g in (closes[:th],closes[th:2*th],closes[2*th:]) if g]
    return dict(addr=addr, span=round(span,1), active_days=len(daily), recency_d=round((now-fl[-1]["time"])/86400000,1),
        realized=round(realized), n_closed=len(closes), wr=round(len(wins)/max(len(wins)+len(losses),1)*100),
        concentration=round(abs(biggest)/max(abs(realized),1),2), maxdd_ratio=round(abs(mdd)/tot,2),
        worst_day_ratio=round(abs(worst_day)/tot,2), adds=adds, avg_down_ratio=round(adds_down/max(adds,1),2),
        sharpe=round(sharpe,2), pos_day_ratio=round(pos_day,2), wr_thirds=wr_thirds, biggest=biggest,
        open_upnl=round(open_upnl), open_loss_ratio=round(-open_upnl/tot,2) if open_upnl<0 else 0.0)

def gates(p):
    f=[]
    if p["concentration"]>0.30: f.append(f"one-trade {int(p['concentration']*100)}%")
    if p["maxdd_ratio"]>0.40: f.append(f"DD {int(p['maxdd_ratio']*100)}%")
    if p["worst_day_ratio"]>0.40: f.append("blowup-day")
    if p["avg_down_ratio"]>0.30 and p["adds"]>=5: f.append(f"MARTINGALE {int(p['avg_down_ratio']*100)}%")
    if p["open_loss_ratio"]>0.30: f.append(f"LOSS-HIDER (open -${abs(p['open_upnl']):,} = {int(p['open_loss_ratio']*100)}% of realized buried)")
    if p["active_days"]/max(p["span"],1)<0.15: f.append("sporadic")
    if p["recency_d"]>14: f.append("stale")
    return f

def cscore(p):
    if p["realized"]<=0: return 0
    c_conc=max(0,1-p["concentration"]/0.5); c_dd=max(0,1-p["maxdd_ratio"]/0.6)
    c_shp=min(max(p["sharpe"],0)/0.5,1); c_pos=p["pos_day_ratio"]
    c_act=min(p["active_days"]/max(p["span"],1)/0.5,1)
    consistency=0.28*c_conc+0.24*c_dd+0.18*c_shp+0.15*c_pos+0.15*c_act
    trust=1.0-min(p["avg_down_ratio"],1.0)*0.7
    fresh=1.0 if p["recency_d"]<=7 else (0.6 if p["recency_d"]<=14 else 0.2)
    return round(consistency*trust*fresh*100,1)

def main():
    addrs=[a.strip() for a in open(POOL) if a.strip()]
    mine=[a for i,a in enumerate(addrs) if i%N==IDX]
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    todo=[a for a in mine if a not in done]
    print(f"[shard {IDX}/{N}] {len(mine)} addrs, {len(todo)} to do, {MIN_INT}s throttle, FREE HL-only", flush=True)
    clean=0
    for i,a in enumerate(todo):
        try:
            p=forensic(a)
            if p:
                p["fails"]=gates(p); p["score"]=0 if p["fails"] else cscore(p)
                if not p["fails"]: clean+=1
                open(RES,"a").write(json.dumps(p)+"\n")
        except Exception as e: print(f"  skip {a[:10]}: {e}", flush=True)
        done.add(a)
        if (i+1)%25==0:
            json.dump(list(done),open(DONE,"w"))
            print(f"  [{IDX}] {i+1}/{len(todo)} done, {clean} clean so far", flush=True)
    json.dump(list(done),open(DONE,"w"))
    print(f"[shard {IDX}] DONE: {len(done)} processed, {clean} clean → {RES}", flush=True)

if __name__=="__main__": main()
