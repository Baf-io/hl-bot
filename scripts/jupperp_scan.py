#!/usr/bin/env python3
"""
JUPITER PERPS scan — the BIGGEST Solana perps venue (JLP ~$900M), MAJORS-ONLY (SOL/ETH/BTC),
with a real ranked PnL leaderboard. The strongest copyable-trader source on Solana.

  DISCOVERY  /top-traders?marketMint&year&week  → ranked owners by weekly PnL (3 main mints ×
             recent weeks, unioned). A clean HL-style leaderboard (Drift's was empty).
  PROFILING  /trades?walletAddress  → 1.5k+ trades with action(Increase/Decrease/Close)+pnl+
             positionPubkey. Group by positionPubkey → per-position lifecycle (open→close, realized
             pnl, hold, side, market). Gives style/WR/payoff/holds/drawdown directly.
  SCORING    reuse the HL model: round_trip vs hold_scale vs scalp, drawdown, win-rate, durability
             (weekly PnL curve — positive-weeks ratio + pre-pump span). main_frac=1.0 by construction
             (majors only). SIZE excluded.

FREE — Jupiter Perps public API only. Accumulates (persistent pool + done), loop mode.
Note: /trades & /positions are decimal-USD; /top-traders & /trader-stats are micro-USD (÷1e6).
"""
import os, sys, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

B="https://perps-api.jup.ag/v1"
OUT="/root/hl-bot/data/research_sol"; os.makedirs(OUT, exist_ok=True)
RES=f"{OUT}/jupperp_results.jsonl"; DONE=f"{OUT}/jupperp_done.json"; SHORT=f"{OUT}/jupperp_shortlist.md"
MINTS={"SOL":"So11111111111111111111111111111111111111112",
       "ETH":"7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs",
       "BTC":"3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"}
MIN_INT=0.2; _last=[0.0]
def _t():
    d=time.time()-_last[0]
    if d<MIN_INT: time.sleep(MIN_INT-d)
    _last[0]=time.time()
def g(path, **p):
    for _ in range(4):
        try:
            _t(); r=requests.get(f"{B}{path}",params=p,timeout=30)
            if r.ok: return r.json()
        except Exception: time.sleep(0.6)
    return None

def discover(weeks=int(os.getenv("JUP_WEEKS","52"))):
    """ranked owners from /top-traders across SOL/ETH/BTC × the venue's history (PnL-weighted union).
    Covers ~1yr by default (works back to Jupiter Perps' 2024 launch) so durability is real, not recent-only."""
    now=dt.datetime.now(dt.UTC); pnl=defaultdict(float)
    for back in range(weeks):
        d=now-dt.timedelta(weeks=back); iso=d.isocalendar()
        for mint in MINTS.values():
            j=g("/top-traders", marketMint=mint, year=iso[0], week=iso[1]) or {}
            for x in j.get("topTradersByPnl",[]):
                o=x.get("owner")
                if o:
                    try: pnl[o]+=float(x.get("totalPnlUsd",0))/1e6
                    except Exception: pass
    return sorted(pnl, key=lambda o:-pnl[o])    # best aggregate PnL across history first

def user_trades(addr, max_pages=int(os.getenv("JUP_TRADE_PAGES","60"))):
    """FULL history via createdAtBefore cursor (default page=20). Walks back to the trader's first trade."""
    out=[]; before=None
    for _ in range(max_pages):
        p={"walletAddress":addr}
        if before: p["createdAtBefore"]=before
        j=g("/trades", **p); lst=(j or {}).get("dataList",[])
        if not lst: break
        out+=lst
        oldest=min(t.get("createdTime",0) for t in lst)
        if before is not None and oldest>=before: break     # no progress
        before=oldest
        if len(lst)<20: break                                 # reached the start
    return out

def _num(v):
    try: return float(v) if v not in (None,"") else 0.0
    except Exception: return 0.0

def profile(addr):
    tr=user_trades(addr)
    if len(tr)<20: return None
    # segment real trade lifecycles per positionPubkey by tracking running notional (size deltas)
    bypk=defaultdict(list)
    for t in tr:
        if t.get("positionPubkey"): bypk[t["positionPubkey"]].append(t)
    lifecycles=[]
    for pk,seq in bypk.items():
        seq.sort(key=lambda t:t.get("createdTime",0))
        run=0.0; peak=0.0; cur=None
        for t in seq:
            a=(t.get("action") or "").lower(); sz=_num(t.get("size")); ct=t.get("createdTime",0); pnl=_num(t.get("pnl"))
            if "increase" in a:
                if run<=1e-6 and sz>0:
                    cur=dict(pnl=0.0,t0=ct,t1=ct,mkt=t.get("positionName"),side=t.get("side"),adds=0,closed=False); lifecycles.append(cur); peak=0.0
                elif cur and sz>0: cur["adds"]+=1
                run+=sz; peak=max(peak,run)
                if cur: cur["t1"]=ct
            elif "decrease" in a or "close" in a:
                run-=sz
                if cur:
                    cur["pnl"]+=pnl; cur["t1"]=ct
                    if run<=max(10.0,0.02*peak):   # ~flat → lifecycle closed
                        cur["closed"]=True; run=0.0; cur=None
    positions=lifecycles; closed=[p for p in positions if p["closed"]]
    if len(positions)<3 or len(closed)<5: return None
    times=[p["t0"] for p in positions if p["t0"]]
    span=(max(times)-min(times))/86400 if times else 1
    if span<10: return None
    opens=len(positions); adds=sum(p["adds"] for p in positions); trims=0
    realized=sum(p["pnl"] for p in positions)
    wins=[p["pnl"] for p in closed if p["pnl"]>0]; losses=[p["pnl"] for p in closed if p["pnl"]<0]
    aw=sum(wins)/len(wins) if wins else 0; al=abs(sum(losses)/len(losses)) if losses else 0
    holds=sorted((p["t1"]-p["t0"])/3600 for p in closed if p["t1"] and p["t0"]); med=holds[len(holds)//2] if holds else 0
    cad=opens/span
    if cad>10 or (med<1 and cad>5): style="scalp"
    elif adds>=opens*3 and len(closed)<=opens*0.5: style="hold_scale"
    else: style="round_trip"
    # drawdown + durability from closed-position pnl ordered by close time
    cl=sorted(closed, key=lambda p:p["t1"] or 0); cum=peak=maxdd=0.0; weekly=defaultdict(float)
    for p in cl:
        cum+=p["pnl"]; peak=max(peak,cum); maxdd=min(maxdd,cum-peak)
        wk=dt.datetime.fromtimestamp(p["t1"],dt.UTC).strftime("%Y-%W"); weekly[wk]+=p["pnl"]
    pos_wk=sum(1 for v in weekly.values() if v>0); tot_wk=len(weekly) or 1
    PUMP="2026-06"  # ISO %Y-%W; pre-pump if weeks predate the 2026 run (~week 06+)
    pre_wk=sum(1 for w in weekly if w<PUMP); pre_pnl=sum(v for w,v in weekly.items() if w<PUMP)
    mkts=defaultdict(int)
    for p in positions: mkts[p["mkt"]]+=1
    return dict(addr=addr, trades=len(tr), positions=opens, closed=len(closed), span=round(span),
        adds=adds, style=style, main_style=style, cadence=round(cad,2),
        wr=round(len(wins)/max(len(wins)+len(losses),1)*100), payoff=round(aw/al,2) if al else 0,
        realized=round(realized), maxdd=round(maxdd), med_hold_h=round(med,1),
        consistency=round(pos_wk/tot_wk,2), weeks=tot_wk, regime_tested=bool(pre_wk>=3 and pre_pnl>0),
        markets=sorted(mkts.items(),key=lambda x:-x[1]))

def score(r):
    if not r or r["realized"]<=0: return 0
    sm={"round_trip":1.0,"hold_scale":0.2,"scalp":0.15}.get(r["main_style"],0.5)
    pay=min(r["payoff"],4)/4; cons=0.4+0.6*r["consistency"]
    s=cons*(0.4+0.6*pay)*sm*100
    if r.get("regime_tested"): s*=1.30
    dd=abs(r["maxdd"])/max(r["realized"],1)
    if dd>0.5: s*=0.45
    elif dd>0.3: s*=0.75
    return round(s,1)

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    best={}
    for r in rows: best[r["addr"]]=r
    rows=sorted(best.values(), key=lambda r:-r.get("score",0))
    with open(SHORT,"w") as f:
        f.write(f"# Jupiter Perps shortlist — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(rows)} profiled (majors-only, biggest SOL venue)\n\n")
        f.write("| score | owner | style | regime | WR | payoff | dd% | cons | wks | realized | hold_h | markets |\n|--|--|--|--|--|--|--|--|--|--|--|--|\n")
        for r in rows[:40]:
            dd=int(abs(r["maxdd"])/max(r["realized"],1)*100)
            rg="✅" if r.get("regime_tested") else "recent"
            f.write(f"| {r.get('score',0)} | `{r['addr'][:8]}…` | {r['main_style']} | {rg} | {r['wr']}% | {r['payoff']} | {dd}% | {r['consistency']} | {r['weeks']} | ${r['realized']:,} | {r['med_hold_h']} | {','.join(m for m,_ in r['markets'])} |\n")

def run_once():
    pool=discover()[:int(os.getenv("JUP_MAX_PROFILE","400"))]   # top-N by aggregate PnL (quality-first)
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    new=[a for a in pool if a not in done]
    print(f"[jupperp] leaderboard pool={len(pool)}, {len(new)} new to profile")
    kept=0
    for i,a in enumerate(new):
        try:
            pr=profile(a)
            if pr: pr["score"]=score(pr); kept+=1; open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e: print(f"  skip {a[:8]}: {e}")
        done.add(a)
        if (i+1)%20==0: json.dump(list(done),open(DONE,"w")); write_shortlist(); print(f"  {i+1}/{len(new)}, {kept} kept")
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    print(f"[jupperp] run done: +{kept} profiled, {len(done)} total seen")

def main():
    if os.getenv("JUP_LOOP")=="1":
        iv=int(os.getenv("JUP_INTERVAL","21600"))
        while True:
            try: run_once()
            except Exception as e: print("run err:", e)
            time.sleep(iv)
    else:
        run_once()

if __name__=="__main__": main()
