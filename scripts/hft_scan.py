#!/usr/bin/env python3
"""
HFT / FAST-TRADER SCAN — templated research tool, broadened to the HIGH-TURNOVER tail.

The swing/intraday scans ranked the leaderboard by monthly ROI and filtered turnover to <=200,
which SILENTLY EXCLUDED the highest-frequency wallets. This sweeps that gap: discovery is ranked
by TURNOVER (vlm/accountValue) descending, so the busiest traders on the venue get profiled — then
each is classified on the SAME copyability axes and scored for the COPYABLE-FAST band.

Self-contained profiling from fills (no dependency on the prior results.jsonl):
  edge      — wr / payoff / realized (from closedPnl), monthly consistency, pre-pump regime test
  speed     — signed-fill episode hold-time distribution (med / sub-5min / intraday / multiday)
  direction — flips/day + extend-ratio + current net $ exposure → directional vs market-maker
  liquidity — fraction of activity on liquid coins (slippage proxy)

Verdict mirrors candidate_dossier: ❌HFT/MM (uncopyable), ⚠️illiquid/fragile, ✅copyable-intraday.
Resumable (done.json), incremental (hft_results.jsonl), ranked shortlist each batch. Read-only.

Env: HFT_MAX_PROFILE (default 800), HFT_MIN_TURN (default 5), HFT_LOOP=1 + HFT_INTERVAL.
"""
import sys, os, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
OUT="/root/hl-bot/data/research2"; os.makedirs(OUT, exist_ok=True)
RES=f"{OUT}/hft_results.jsonl"; DONE=f"{OUT}/hft_done.json"; SHORT=f"{OUT}/hft_shortlist.md"
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","PEPE","kPEPE"}
MIN_INT=float(os.getenv("HFT_MIN_INT","0.5")); _last=[0.0]   # ↑ to coexist with live services
def post(t,**k):
    for _ in range(5):
        try:
            d=time.time()-_last[0]
            if d<MIN_INT: time.sleep(MIN_INT-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: time.sleep(1.2)
    return None

def discover(min_turn=float(os.getenv("HFT_MIN_TURN","5"))):
    r=requests.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",timeout=90)
    rows=r.json()["leaderboardRows"]; pool=[]
    for row in rows:
        try:
            av=float(row["accountValue"]); w=dict(row["windowPerformances"])
            mv=float(w["month"]["vlm"]); ap=float(w["allTime"]["pnl"])
        except Exception: continue
        if av<5_000 or ap<=0: continue
        turn=mv/av if av else 0
        if turn<min_turn: continue
        pool.append((turn, row["ethAddress"]))
    pool.sort(reverse=True)                      # busiest first — the HFT tail ROI-sort missed
    return [a for _,a in pool]

def fills(addr, days=150, pages=int(os.getenv("HFT_PAGES","15"))):
    out=[]; end=int(time.time()*1000); cur=end-days*86400*1000
    for _ in range(pages):
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

def profile(addr):
    fl=fills(addr)
    if len(fl)<30: return None
    run=defaultdict(float); t0=defaultdict(float); EPS=1e-9
    closed=[]; pnls=[]; coin_hits=defaultdict(int); flips=0; extend=0; reduce_=0
    monthly=defaultdict(float)
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); pnl=float(f.get("closedPnl",0)); d=sz if f.get("side")=="B" else -sz
        monthly[dt.datetime.fromtimestamp(f["time"]/1000,dt.UTC).strftime("%Y-%m")]+=pnl
        prev=run[c]; run[c]=round(prev+d,8); cur=run[c]
        if abs(cur)>abs(prev): extend+=1
        else: reduce_+=1
        if abs(prev)<EPS and abs(cur)>=EPS:
            t0[c]=f["time"]; coin_hits[c]+=1
        elif abs(prev)>=EPS and abs(cur)<EPS:
            closed.append((f["time"]-t0[c])/3.6e6); pnls.append(pnl)
        elif abs(prev)>=EPS and abs(cur)>=EPS and (prev>0)!=(cur>0):
            closed.append((f["time"]-t0[c])/3.6e6); pnls.append(pnl); t0[c]=f["time"]; coin_hits[c]+=1; flips+=1
        elif (abs(prev)>=EPS and abs(cur)<abs(prev)):
            pnls.append(pnl)
    if len(closed)<10: return None
    span=(fl[-1]["time"]-fl[0]["time"])/86400000 or 1
    hs=sorted(closed); n=len(hs); med=hs[n//2]
    wins=[p for p in pnls if p>0]; losses=[p for p in pnls if p<0]
    aw=sum(wins)/len(wins) if wins else 0; al=abs(sum(losses)/len(losses)) if losses else 0
    cum=peak=maxdd=0.0
    for p in pnls: cum+=p; peak=max(peak,cum); maxdd=min(maxdd,cum-peak)
    pre=sum(v for m,v in monthly.items() if m<"2026-02"); pre_n=sum(1 for m in monthly if m<"2026-02")
    cs=post("clearinghouseState",user=addr) or {}; mids=post("allMids") or {}
    netusd=0.0
    for ap in cs.get("assetPositions",[]):
        p=ap["position"]; szi=float(p["szi"]); c=p["coin"]
        netusd+=szi*float(mids.get(c,0) or p.get("entryPx") or 0)
    return dict(addr=addr, n_closed=n, span=round(span), realized=round(sum(pnls)),
        med_h=round(med,2), pct_sub5=round(sum(1 for h in hs if h<1/12)/n,2),
        pct_intraday=round(sum(1 for h in hs if h<1)/n,2), pct_multiday=round(sum(1 for h in hs if h>=24)/n,2),
        wr=round(len(wins)/max(len(wins)+len(losses),1)*100), payoff=round(aw/al,2) if al else 0,
        maxdd=round(maxdd), flips_per_day=round(flips/span,2), extend_ratio=round(extend/max(extend+reduce_,1),2),
        liq_frac=round(sum(v for c,v in coin_hits.items() if c in LIQUID or c.startswith("k"))/(sum(coin_hits.values()) or 1),2),
        net_usd_now=round(netusd), regime_tested=bool(pre_n>=3 and pre>0),
        hot_coins=[c for c,_ in sorted(coin_hits.items(),key=lambda x:-x[1])[:4]])

def verdict(p):
    dd=abs(p["maxdd"])/max(p["realized"],1)
    if p["med_h"]<0.08 or p["pct_sub5"]>=0.6: return "HFT/MM-uncopyable"
    if abs(p["net_usd_now"])<50 and p["flips_per_day"]>4 and p["med_h"]<0.5: return "market-maker"
    if p["liq_frac"]<0.3: return "illiquid"
    if dd>0.5: return "fragile"
    if 0.1<=p["med_h"]<=12 and p["pct_intraday"]>=0.4: return "COPYABLE-INTRADAY"
    return "review"

def score(p):
    if p["realized"]<=0 or p["n_closed"]<20 or p["med_h"]<0.08: return 0
    wr=p["wr"]/100; sample=min(p["n_closed"],250)/250
    s=100*wr*(0.45+0.55*p["pct_intraday"])*(0.5+0.5*sample)*(1-0.5*p["pct_sub5"])*(0.5+0.5*p["liq_frac"])
    if p.get("regime_tested"): s*=1.2
    dd=abs(p["maxdd"])/max(p["realized"],1)
    if dd>0.5: s*=0.4
    elif dd>0.3: s*=0.7
    if abs(p["net_usd_now"])<50 and p["flips_per_day"]>4: s*=0.3   # MM penalty
    return round(s,1)

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    best={}
    for r in rows: best[r["addr"]]=r
    rows=sorted(best.values(), key=lambda r:-r.get("score",0))
    with open(SHORT,"w") as f:
        f.write(f"# HFT/fast-trader scan (turnover-ranked) — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(rows)} profiled\n")
        f.write("_Broadened to the high-turnover tail. ✅COPYABLE-INTRADAY = directional+liquid+holds long enough; rest flagged why-not._\n\n")
        f.write("| score | addr | verdict | med_h | sub5% | intra% | WR | payoff | n | dd% | liq% | flips/d | net$now | regime | realized | coins |\n")
        f.write("|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|\n")
        for r in rows[:50]:
            dd=int(abs(r["maxdd"])/max(r["realized"],1)*100)
            f.write(f"| {r.get('score',0)} | `{r['addr'][:10]}…` | {r.get('verdict','?')} | {r['med_h']}h | {int(r['pct_sub5']*100)}% | {int(r['pct_intraday']*100)}% | {r['wr']}% | {r['payoff']} | {r['n_closed']} | {dd}% | {int(r['liq_frac']*100)}% | {r['flips_per_day']} | ${r['net_usd_now']:,} | {'✅' if r.get('regime_tested') else '-'} | ${r['realized']:,} | {','.join(r['hot_coins'])} |\n")

def run_once():
    pool=discover()[:int(os.getenv("HFT_MAX_PROFILE","800"))]
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    new=[a for a in pool if a not in done]
    print(f"[hft] turnover pool={len(pool)}, {len(new)} new to profile", flush=True)
    kept=0
    for i,a in enumerate(new):
        try:
            pr=profile(a)
            if pr:
                pr["verdict"]=verdict(pr); pr["score"]=score(pr); kept+=1
                open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e: print(f"  skip {a[:10]}: {e}", flush=True)
        done.add(a)
        if (i+1)%20==0:
            json.dump(list(done),open(DONE,"w")); write_shortlist()
            print(f"  {i+1}/{len(new)} done, {kept} kept", flush=True)
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    print(f"[hft] run done: +{kept} profiled, {len(done)} seen", flush=True)

def main():
    if os.getenv("HFT_LOOP")=="1":
        iv=int(os.getenv("HFT_INTERVAL","21600"))
        while True:
            try: run_once()
            except Exception as e: print("run err:", e, flush=True)
            time.sleep(iv)
    else: run_once()

if __name__=="__main__": main()
