#!/usr/bin/env python3
"""
DRIFT (Solana perps) scan — the COPYABLE Solana analog to the HL deep-scan.

Unlike spot/memecoin swaps (snipe/MEV, uncopyable), Drift perp traders take DIRECTIONAL swing
positions on main coins (SOL/ETH/BTC-PERP) — exactly what we copy. And Drift's Data API is as
clean as HL's: trade feed for discovery, per-user trades + PnL metrics + snapshots for profiling.

  DISCOVERY  /market/{SYM}-PERP/trades (paginated) → unique taker/maker accounts = active perp traders
  PROFILING  /user/{id}/trades → per-market position reconstruction (opens/adds/trims/full/flips,
             holds, style, WR/payoff like HL); /user/{id}/snapshots/trading → cumulativeRealizedPnl,
             volume, fees (headline) + time-series snapshots (durability).
  SCORING    reuses the HL lessons: per-MAIN-market round_trip vs hold_scale vs scalp, drawdown,
             win-rate, durability. SIZE excluded (copyability, not whale-ness).

FREE — Drift Data API only (no Nansen credits, no HL API). Rate-limited; resumable.
"""
import os, sys, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

B="https://data.api.drift.trade"
OUT="/root/hl-bot/data/research_sol"; os.makedirs(OUT, exist_ok=True)
RES=f"{OUT}/drift_results.jsonl"; SHORT=f"{OUT}/drift_shortlist.md"
MAIN={"SOL-PERP","ETH-PERP","BTC-PERP"}
MIN_INT=0.25; _last=[0.0]
def _t():
    d=time.time()-_last[0]
    if d<MIN_INT: time.sleep(MIN_INT-d)
    _last[0]=time.time()
def g(path):
    for _ in range(4):
        try:
            _t(); r=requests.get(f"{B}{path}",timeout=25)
            if r.ok: return r.json()
        except Exception: time.sleep(0.8)
    return None

def discover(markets=("SOL-PERP","ETH-PERP","BTC-PERP","HYPE-PERP","DRIFT-PERP","JTO-PERP","WIF-PERP","DOGE-PERP")):
    """unique taker/maker accounts from the perp trade feed (active directional traders).
    Profiling still scores MAIN={SOL,ETH,BTC}-PERP focus; extra markets just widen discovery."""
    # Drift exposes only the recent live feed (limit<=50; page/dated endpoints unavailable).
    # Harvest unique taker/maker across the main perp markets (+ a few more for breadth).
    wallets=set()
    for mk in markets:
        j=g(f"/market/{mk}/trades?limit=50"); recs=(j or {}).get("records",[])
        for t in recs:
            for k in ("taker","maker"):
                if t.get(k): wallets.add(t[k])
    return list(wallets)

def user_trades(acct, pages=5):
    j=g(f"/user/{acct}/trades?limit=50"); recs=(j or {}).get("records",[])
    return recs if isinstance(recs,list) else []

def metrics(acct):
    j=g(f"/user/{acct}/snapshots/trading") or {}
    m=j.get("metrics") or {}
    def f(k):
        try: return float(m.get(k,0) or 0)
        except Exception: return 0.0
    return dict(realized=f("cumulativeRealizedPnl"),
                vol=f("cumulativeTakerVolume")+f("cumulativeMakerVolume"),
                fees=f("cumulativeFeePaid"), snaps=len(j.get("snapshots") or []))

def profile(acct):
    trades=user_trades(acct)
    if len(trades)<12: return None
    trades.sort(key=lambda t:t.get("ts",0))
    run=defaultdict(float); ev=defaultdict(lambda: dict(opens=0,adds=0,trims=0,full=0,flips=0))
    entry=defaultdict(float); closes=[]; holds=[]; topen={}; mk_count=defaultdict(int)
    for t in trades:
        sym=t.get("symbol"); base=float(t.get("baseAssetAmountFilled",0) or 0)
        if base<=0 or not sym: continue
        px=float(t.get("oraclePrice",0) or 0)
        q=float(t.get("quoteAssetAmountFilled",0) or 0)
        if px<=0 and base>0: px=q/base
        # the user's side on this fill
        if t.get("taker")==acct: d=t.get("takerOrderDirection")
        elif t.get("maker")==acct: d=t.get("makerOrderDirection")
        else: d=t.get("takerOrderDirection")
        delta=base if d=="long" else -base
        prev=run[sym]; run[sym]=round(prev+delta,9); now=run[sym]; e=ev[sym]
        if abs(prev)<1e-9 and abs(now)>=1e-9:
            e["opens"]+=1; mk_count[sym]+=1; entry[sym]=px; topen[sym]=t.get("ts")
        elif abs(prev)>=1e-9 and abs(now)<1e-9:
            e["full"]+=1; closes.append((px-entry[sym])*prev)  # prev sign carries direction
            if sym in topen: holds.append((t.get("ts",0)-topen.pop(sym))/3600)
        elif abs(prev)>=1e-9 and (prev>0)!=(now>0):
            e["flips"]+=1; closes.append((px-entry[sym])*prev); entry[sym]=px; topen[sym]=t.get("ts")
            mk_count[sym]+=1
        elif abs(now)>abs(prev):
            entry[sym]=(entry[sym]*abs(prev)+px*abs(delta))/abs(now); e["adds"]+=1  # wtd avg entry
        else:
            e["trims"]+=1; closes.append((px-entry[sym])*(prev if prev>0 else prev))
    span=(trades[-1]["ts"]-trades[0]["ts"])/86400 or 1
    opens=sum(e["opens"] for e in ev.values()); adds=sum(e["adds"] for e in ev.values())
    full=sum(e["full"] for e in ev.values()); trims=sum(e["trims"] for e in ev.values()); flips=sum(e["flips"] for e in ev.values())
    if opens<3 or (full+flips+trims)<5 or span<10: return None
    m_opens=sum(ev[s]["opens"] for s in MAIN); m_adds=sum(ev[s]["adds"] for s in MAIN)
    m_close=sum(ev[s]["full"]+ev[s]["flips"] for s in MAIN)
    main_rt=round(m_close/max(m_opens,1),2)
    cad=opens/span
    if cad>10: style="scalp"
    elif main_rt>=0.5 and m_close>=4: style="round_trip"
    elif m_adds>=m_opens*3 and m_close<=m_opens: style="hold_scale"
    else: style="mixed"
    wins=[c for c in closes if c>0]; losses=[c for c in closes if c<0]
    aw=sum(wins)/len(wins) if wins else 0; al=abs(sum(losses)/len(losses)) if losses else 0
    cum=peak=maxdd=0.0
    for c in closes: cum+=c; peak=max(peak,cum); maxdd=min(maxdd,cum-peak)
    holds.sort(); med=holds[len(holds)//2] if holds else 0
    mm=metrics(acct)
    realized=mm["realized"] if mm["realized"] else round(sum(closes))
    main_frac=sum(mk_count[s] for s in MAIN)/(sum(mk_count.values()) or 1)
    return dict(addr=acct, trades=len(trades), span=round(span), opens=opens, adds=adds, full=full,
        flips=flips, style=style, main_style=style, main_rt=main_rt, cadence=round(cad,2),
        wr=round(len(wins)/max(len(wins)+len(losses),1)*100), payoff=round(aw/al,2) if al else 0,
        realized=round(realized), maxdd=round(maxdd), med_hold_h=round(med,1),
        main_frac=round(main_frac,2), vol=round(mm["vol"]),
        markets=sorted(mk_count.items(),key=lambda x:-x[1])[:5])

def score(r):
    if not r or r["realized"]<=0: return 0
    sm={"round_trip":1.0,"mixed":0.55,"hold_scale":0.2,"scalp":0.15}.get(r["main_style"],0.5)
    base=0.4+0.6*r["main_frac"]; pay=min(r["payoff"],4)/4
    s=base*(0.4+0.6*pay)*sm*100
    dd=abs(r["maxdd"])/max(r["realized"],1)
    if dd>0.5: s*=0.45
    elif dd>0.3: s*=0.75
    return round(s,1)

DONE=f"{OUT}/drift_done.json"

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    best={}
    for r in rows: best[r["addr"]]=r
    rows=sorted(best.values(), key=lambda r:-r.get("score",0))
    with open(SHORT,"w") as f:
        f.write(f"# Drift (Solana perp) shortlist — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(rows)} profiled\n\n")
        f.write("| score | acct | style | main_rt | WR | payoff | dd% | main% | cad | realized | vol | markets |\n|--|--|--|--|--|--|--|--|--|--|--|--|\n")
        for r in rows[:40]:
            dd=int(abs(r["maxdd"])/max(r["realized"],1)*100)
            f.write(f"| {r.get('score',0)} | `{r['addr'][:8]}…` | {r['main_style']} | {r['main_rt']} | {r['wr']}% | {r['payoff']} | {dd}% | {int(r['main_frac']*100)}% | {r['cadence']}/d | ${r['realized']:,} | ${r['vol']:,} | {','.join(s for s,_ in r['markets'])} |\n")

def run_once():
    pool=discover()
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    new=[a for a in pool if a not in done]
    print(f"[drift] pool={len(pool)} active, {len(new)} new to profile")
    kept=0
    for i,a in enumerate(new):
        try:
            pr=profile(a)
            if pr: pr["score"]=score(pr); kept+=1; open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e: print(f"  skip {a[:8]}: {e}")
        done.add(a)
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    print(f"[drift] run done: +{kept} profiled, {len(done)} total seen")

def main():
    if os.getenv("DRIFT_LOOP")=="1":
        iv=int(os.getenv("DRIFT_INTERVAL","3600"))      # accumulate live perp traders hourly
        while True:
            try: run_once()
            except Exception as e: print("run err:", e)
            time.sleep(iv)
    else:
        run_once()

if __name__=="__main__": main()
