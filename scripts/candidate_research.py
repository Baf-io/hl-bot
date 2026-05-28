#!/usr/bin/env python3
"""
Overnight candidate research — big-pool profiler of HL leaderboard traders.

Goal: as large a pool as possible of CONSISTENT traders (not one-month-wonders), with a
tilt toward our main tokens (BTC/ETH/SOL). Heavy API work lives HERE (cheap, no Claude),
so the candidate list is delivered even if the evaluation loop stops.

Safety:
- RATE-LIMITED (min interval between calls) to protect the live services that share this IP.
- RESUMABLE: checkpoints processed addresses; re-run continues where it left off.
- ERROR-TOLERANT: per-trader try/except, backoff on failures — never crashes the run.
- Writes results.jsonl incrementally + rewrites a ranked shortlist.md every batch.
"""
import sys, json, os, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
OUT="/root/hl-bot/data/research"; os.makedirs(OUT, exist_ok=True)
DONE=f"{OUT}/done.json"; RES=f"{OUT}/results.jsonl"; SHORT=f"{OUT}/shortlist.md"; LOGF=f"{OUT}/run.log"
MAIN={"BTC","ETH","SOL"}
MIN_INTERVAL=0.40          # ≥0.4s/call (~2.5/s) — leaves HL API headroom for live services
MAX_POOL=1500              # cap traders profiled (bounded runtime)

_last=[0.0]
def _throttle():
    dtm=time.time()-_last[0]
    if dtm<MIN_INTERVAL: time.sleep(MIN_INTERVAL-dtm)
    _last[0]=time.time()
def log(m):
    line=f"{dt.datetime.now(dt.UTC).strftime('%H:%M:%S')} {m}"
    print(line, flush=True); open(LOGF,"a").write(line+"\n")
def post(t, **k):
    for attempt in range(4):
        try:
            _throttle()
            return requests.post(API,json={"type":t,**k},timeout=25).json()
        except Exception as e:
            time.sleep(1.5*(attempt+1))
    raise RuntimeError("api fail")

def profile(addr, lb):
    cs=post("clearinghouseState",user=addr)
    av=float(cs["marginSummary"]["accountValue"])
    pos=[(p["position"]["coin"],float(p["position"]["szi"]),p["position"]["leverage"]["value"]) for p in cs.get("assetPositions",[])]
    fills=post("userFills",user=addr)
    if not isinstance(fills,list) or len(fills)<10: return None
    fills.sort(key=lambda f:f["time"])
    ev=[]
    for f in fills:
        d=f["dir"];c=f["coin"];t=f["time"];sz=float(f["sz"]);px=float(f["px"]);p=float(f.get("closedPnl",0))
        if ev and ev[-1]["c"]==c and ev[-1]["d"]==d and t-ev[-1]["t"]<=120000:
            ev[-1]["pnl"]+=p; ev[-1]["t"]=t
        else: ev.append(dict(c=c,d=d,pnl=p,t0=t,t=t))
    span=(ev[-1]["t"]-ev[0]["t"])/86400000 or 1
    opens=[e for e in ev if "Open" in e["d"] or e["d"]=="B"]
    closes=[e for e in ev if "Close" in e["d"]]
    if len(closes)<8 or span<15: return None
    longs=sum(1 for e in opens if "Long" in e["d"] or e["d"]=="B"); shorts=len(opens)-longs
    coins=defaultdict(int)
    for e in opens: coins[e["c"]]+=1
    nopen=len(opens) or 1
    main_frac=sum(coins[c] for c in MAIN)/nopen
    wins=[e for e in closes if e["pnl"]>0]; losses=[e for e in closes if e["pnl"]<0]
    realized=sum(e["pnl"] for e in closes)
    aw=sum(e["pnl"] for e in wins)/len(wins) if wins else 0
    al=abs(sum(e["pnl"] for e in losses)/len(losses)) if losses else 0
    # monthly consistency
    monthly=defaultdict(float)
    for e in closes:
        monthly[dt.datetime.fromtimestamp(e["t"]/1000,dt.UTC).strftime("%Y-%m")]+=e["pnl"]
    mvals=list(monthly.values())
    pos_months=sum(1 for v in mvals if v>0)
    total_months=len(mvals) or 1
    top_month_frac=(max(mvals)/realized) if realized>0 and mvals else 1.0   # 1-month-wonder if →1
    cum=0;peak=0;maxdd=0
    for e in closes:
        cum+=e["pnl"];peak=max(peak,cum);maxdd=min(maxdd,cum-peak)
    holds=[];lo={}
    for e in ev:
        if "Open" in e["d"] or e["d"]=="B": lo[e["c"]]=e["t0"]
        elif "Close" in e["d"] and e["c"] in lo: holds.append((e["t0"]-lo.pop(e["c"]))/3600000)
    holds.sort(); med=holds[len(holds)//2] if holds else 0
    main_pnl=sum(e["pnl"] for e in closes if e["c"] in MAIN)
    return dict(addr=addr, av=round(av), turn=round(lb.get("turn",0),1), span=round(span),
        nopen=len(opens), cadence=round(len(opens)/span,2), longs=longs, shorts=shorts,
        short_pct=round(shorts/nopen*100), wr=round(len(wins)/len(closes)*100),
        payoff=round(aw/al,2) if al else 0, realized=round(realized), main_pnl=round(main_pnl),
        main_frac=round(main_frac,2), pos_months=pos_months, total_months=total_months,
        consistency=round(pos_months/total_months,2), top_month_frac=round(top_month_frac,2),
        maxdd=round(maxdd), med_hold_h=round(med,1),
        top_coins=sorted(coins.items(),key=lambda x:-x[1])[:5],
        now=[(c,'L' if s>0 else 'S',lv) for c,s,lv in pos][:6])

def score(r):
    # CONSISTENCY-first composite; tilt to main tokens; exclude HFT & 1-month-wonders elsewhere
    if r["realized"]<=0: return 0
    base=r["consistency"]*(0.4+0.6*r["main_frac"])
    payoff=min(r["payoff"],4)/4
    size=min(r["realized"],300000)/300000
    months=min(r["total_months"],6)/6
    return round(base*(0.4+0.6*payoff)*(0.3+0.7*size)*(0.4+0.6*months)*100,1)

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    for r in rows: r["score"]=score(r)
    # qualified pool: consistent, real sample, not HFT, not pure 1-month-wonder
    qual=[r for r in rows if r["realized"]>0 and r["cadence"]<15 and r["consistency"]>=0.4 and r["top_month_frac"]<0.85]
    qual.sort(key=lambda r:-r["score"])
    main=[r for r in qual if r["main_frac"]>=0.5]
    with open(SHORT,"w") as f:
        f.write(f"# Candidate research shortlist\n_updated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | profiled {len(rows)} | qualified {len(qual)} | BTC/ETH/SOL-focused {len(main)}_\n\n")
        def tbl(lst,title):
            f.write(f"\n## {title} (top {min(40,len(lst))})\n\n| score | addr | WR | payoff | cons | mon+ | 1mo% | main% | cadence | hold | realized | now |\n|--|--|--|--|--|--|--|--|--|--|--|--|\n")
            for r in lst[:40]:
                f.write(f"| {r['score']} | `{r['addr'][:10]}…` | {r['wr']}% | {r['payoff']} | {r['consistency']} | {r['pos_months']}/{r['total_months']} | {int(r['top_month_frac']*100)}% | {int(r['main_frac']*100)}% | {r['cadence']}/d | {r['med_hold_h']}h | ${r['realized']:,} | {','.join(c for c,_,_ in r['now']) or 'flat'} |\n")
        tbl(main,"🎯 BTC/ETH/SOL-focused & consistent")
        tbl(qual,"📊 All consistent (any coins)")

def main():
    log("=== candidate research start ===")
    r=requests.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",timeout=90)
    rows=r.json()["leaderboardRows"]
    pool=[]
    for row in rows:
        try:
            av=float(row["accountValue"]); w=dict(row["windowPerformances"])
            m=w["month"]; mr=float(m["roi"]); mv=float(m["vlm"]); mp=float(m["pnl"]); ap=float(w["allTime"]["pnl"])
        except Exception: continue
        if not(25_000<=av<=50_000_000): continue
        if mp<=0 or ap<=0: continue                 # durable-ish: positive month + all-time
        if mr<0.08: continue                        # broad: >=8% month
        turn=mv/av if av else 0
        if not(1.5<=turn<=130): continue            # active, not HFT, not hodl
        pool.append((mr, {"addr":row["ethAddress"],"turn":turn}))
    pool.sort(reverse=True)
    pool=[p[1] for p in pool[:MAX_POOL]]
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    log(f"pool={len(pool)} | already done={len(done)}")
    n=0
    for item in pool:
        a=item["addr"]
        if a in done: continue
        try:
            pr=profile(a,item)
            if pr: open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e:
            log(f"skip {a[:10]}: {e}")
        done.add(a); n+=1
        if n%25==0:
            json.dump(list(done),open(DONE,"w")); write_shortlist()
            log(f"progress {n}/{len(pool)-0} processed (results so far)")
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    log(f"=== DONE: processed {n} this run, total {len(done)} ===")

if __name__=="__main__": main()
