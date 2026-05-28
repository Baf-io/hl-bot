#!/usr/bin/env python3
"""
Deep scan v2 — big-pool HL trader profiler with TWO new dimensions over candidate_research.py:

  1. TRADING STYLE classifier (the fix for the 26fe trap): round_trip (copyable) vs
     hold_scale (uncopyable — edge is a locked-in old entry) vs scalp (uncopyable HFT).
  2. EQUITY-FLOW forensics (userNonFundingLedgerUpdates): deposits/withdrawals, whether this
     addr is a SUB of a master (→ who the real "main addy" is), vault links, linked wallets,
     and whether equity grew ORGANICALLY from trading (good for copy) or from inflows (not).

Goal: surface traders whose edge is a REPEATABLE entry/exit signal funded organically — not
bull-market core-holders or capital-cycling subaccounts.

Safety: rate-limited (protect live services), resumable (done.json), per-trader try/except,
incremental results.jsonl + ranked shortlist rewritten every batch. Read-only — no trading.
"""
import sys, json, os, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
OUT="/root/hl-bot/data/research2"; os.makedirs(OUT, exist_ok=True)
DONE=f"{OUT}/done.json"; RES=f"{OUT}/results.jsonl"; SHORT=f"{OUT}/shortlist.md"; LOGF=f"{OUT}/run.log"
MAIN={"BTC","ETH","SOL"}
MIN_INTERVAL=0.50          # ≥0.5s/call (~2/s) — leaves HL API headroom for the 4 live services
MAX_POOL=6000              # cap traders profiled (bounded runtime; ~2-3h at 2/s)
HL_SYS={"0x2222222222222222222222222222222222222222"}  # HL system; 0x2000…00XX also filtered below

_last=[0.0]
def _throttle():
    d=time.time()-_last[0]
    if d<MIN_INTERVAL: time.sleep(MIN_INTERVAL-d)
    _last[0]=time.time()
def log(m):
    line=f"{dt.datetime.now(dt.UTC).strftime('%H:%M:%S')} {m}"
    print(line, flush=True); open(LOGF,"a").write(line+"\n")
def post(t, **k):
    for attempt in range(5):
        try:
            _throttle()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is None or (isinstance(j,dict) and j.get("error")):   # transient null/err body → retry
                raise ValueError("empty/err body")
            return j
        except Exception:
            time.sleep(1.5*(attempt+1))
    raise RuntimeError("api fail")

def _is_sys(a):
    if not a: return True
    a=a.lower()
    return a in HL_SYS or a.startswith("0x2000000000000000000000000000000000000")

def profile(addr, lb):
    cs=post("clearinghouseState",user=addr)
    av=float(cs["marginSummary"]["accountValue"])
    pos=[(p["position"]["coin"],float(p["position"]["szi"]),p["position"]["leverage"]["value"]) for p in cs.get("assetPositions",[])]
    fills=post("userFills",user=addr)
    if not isinstance(fills,list) or len(fills)<12: return None
    fills.sort(key=lambda f:f["time"])
    # ── per-coin running position → opens / adds / trims / full_closes / flips ──
    run=defaultdict(float)
    ev=defaultdict(lambda: dict(opens=0,adds=0,trims=0,full=0,flips=0))
    closes=[]; coins=defaultdict(int); longs=0; EPS=1e-6
    for f in fills:
        c=f["coin"]; sz=float(f["sz"]); pnl=float(f.get("closedPnl",0))
        delta=sz if f.get("side")=="B" else -sz          # B=buy(+), A=sell(-): authoritative
        prev=run[c]; run[c]=round(prev+delta,8); now=run[c]; e=ev[c]
        if abs(prev)<EPS and abs(now)>=EPS:
            e["opens"]+=1; coins[c]+=1
            if now>0: longs+=1
        elif abs(prev)>=EPS and abs(now)<EPS:
            e["full"]+=1; closes.append(pnl)
        elif abs(prev)>=EPS and abs(now)>=EPS and (prev>0)!=(now>0):
            e["flips"]+=1; coins[c]+=1; closes.append(pnl)
            if now>0: longs+=1
        elif abs(now)>abs(prev):
            e["adds"]+=1
        else:
            e["trims"]+=1; closes.append(pnl)
    opens=sum(e["opens"] for e in ev.values()); adds=sum(e["adds"] for e in ev.values())
    trims=sum(e["trims"] for e in ev.values()); full_closes=sum(e["full"] for e in ev.values())
    flips=sum(e["flips"] for e in ev.values())
    span=(fills[-1]["time"]-fills[0]["time"])/86400000 or 1
    nclose=full_closes+flips+trims
    if nclose<8 or span<15: return None
    nopen=opens or 1
    main_frac=sum(coins[c] for c in MAIN)/(sum(coins.values()) or 1)
    # ── PER-MAIN-COIN style (the 26fe trap: round-trips alts but HOLDS btc) ──
    m_opens=sum(ev[c]["opens"] for c in MAIN); m_adds=sum(ev[c]["adds"] for c in MAIN)
    m_close=sum(ev[c]["full"]+ev[c]["flips"] for c in MAIN)
    main_rt=round(m_close/max(m_opens,1),2)
    if m_opens<2: main_style="thin"
    elif main_rt>=0.5: main_style="round_trip"
    elif m_adds>=m_opens*3 and m_close<=m_opens: main_style="hold_scale"
    else: main_style="mixed"
    realized=sum(closes)
    wins=[p for p in closes if p>0]; losses=[p for p in closes if p<0]
    aw=sum(wins)/len(wins) if wins else 0; al=abs(sum(losses)/len(losses)) if losses else 0
    # monthly consistency
    monthly=defaultdict(float)
    for f in fills: monthly[dt.datetime.fromtimestamp(f["time"]/1000,dt.UTC).strftime("%Y-%m")]+=float(f.get("closedPnl",0))
    mvals=[v for v in monthly.values()]; pos_m=sum(1 for v in mvals if v>0); tot_m=len(mvals) or 1
    top_m=(max(mvals)/realized) if realized>0 and mvals else 1.0
    cum=peak=maxdd=0.0
    for p in closes:
        cum+=p; peak=max(peak,cum); maxdd=min(maxdd,cum-peak)
    # ── DURABILITY: was he positive BEFORE the 2026 pump? (the one test the pump can't fake) ──
    PUMP="2026-02"                                       # 2026 run started ~Feb; pre = regime-tested
    pre_months=sum(1 for m in monthly if m<PUMP)
    pre_pnl=sum(v for m,v in monthly.items() if m<PUMP)
    regime_tested=bool(pre_months>=3 and pre_pnl>0)      # net-positive across a pre-pump stretch
    pump_only=bool(pre_months==0)                        # entire track is 2026-pump-era
    # ── ARCHETYPE: copy-lag fragility (low-WR/high-payoff edge lives in a few entries we'll lag) ──
    wr_v=round(len(wins)/max(len(wins)+len(losses),1)*100); payoff_v=round(aw/al,2) if al else 0
    if wr_v<35 and payoff_v>3: archetype="trend_rider"   # fragile to copy-lag
    elif wr_v>=70: archetype="high_wr"                   # robust per trade
    else: archetype="mixed"
    # ── STYLE classifier (the 26fe-trap fix) ──
    cadence=nopen/span
    rt_ratio=(full_closes+flips)/nopen          # how often he actually completes a trade
    if cadence>8 or (closes and len([p for p in closes])>0 and span<30 and cadence>5):
        style="scalp"
    elif rt_ratio>=0.5 and (full_closes+flips)>=5:
        style="round_trip"
    elif adds>=nopen*3 and (full_closes+flips)<=nopen:
        style="hold_scale"
    else:
        style="mixed"
    return dict(addr=addr, av=round(av), turn=round(lb.get("turn",0),1), span=round(span),
        opens=opens, adds=adds, trims=trims, full_closes=full_closes, flips=flips,
        cadence=round(cadence,2), rt_ratio=round(rt_ratio,2), style=style,
        main_style=main_style, main_rt=main_rt, m_opens=m_opens, maxdd=round(maxdd),
        regime_tested=regime_tested, pump_only=pump_only, pre_months=pre_months,
        pre_pnl=round(pre_pnl), archetype=archetype,
        short_pct=round((nopen-longs)/nopen*100), wr=round(len(wins)/max(len(wins)+len(losses),1)*100),
        payoff=round(aw/al,2) if al else 0, realized=round(realized),
        main_frac=round(main_frac,2), pos_months=pos_m, total_months=tot_m,
        consistency=round(pos_m/tot_m,2), top_month_frac=round(top_m,2),
        top_coins=sorted(coins.items(),key=lambda x:-x[1])[:5],
        now=[(c,'L' if s>0 else 'S',lv) for c,s,lv in pos][:6])

def flow(addr, realized):
    """Equity-flow forensics via userNonFundingLedgerUpdates."""
    try:
        upd=post("userNonFundingLedgerUpdates",user=addr)
    except Exception:
        return {}
    if not isinstance(upd,list): return {}
    dep=wd=0.0; vault=False; is_sub=False; is_master=False; master=None
    cps=set()
    for u in upd:
        d=u.get("delta",{}) or {}; tp=d.get("type"); usd=0.0
        try: usd=float(d.get("usdc",0) or 0)
        except Exception: usd=0.0
        if tp=="deposit": dep+=usd
        elif tp=="withdraw": wd+=usd
        elif tp in ("vaultDeposit","vaultWithdraw","vaultCreate","vaultDistribution"): vault=True
        elif tp=="subAccountTransfer":
            frm=(d.get("user") or "").lower(); to=(d.get("destination") or "").lower()
            if to==addr.lower(): is_sub=True; master=frm or master
            if frm==addr.lower(): is_master=True; cps.add(to)
        elif tp in ("send","spotTransfer","internalTransfer","accountClassTransfer"):
            dest=(d.get("destination") or d.get("user") or "").lower()
            if dest and dest!=addr.lower() and not _is_sys(dest): cps.add(dest)
    net=dep-wd
    # verdict
    if vault: verdict="CAUTION:vault-linked"
    elif is_sub: verdict=f"CAUTION:SUB-of-{(master or '?')[:8]}"
    elif dep>0 and wd>dep*2: verdict="CAUTION:cycles-capital-out"
    elif realized>0 and net>=0: verdict="POSITIVE:organic-retained"
    elif realized>0: verdict="OK:trading-positive"
    else: verdict="WEAK"
    return dict(dep=round(dep), wd=round(wd), net_flow=round(net), vault=vault,
                is_sub=is_sub, is_master=is_master, main_addy=(master if is_sub else addr),
                linked=sorted(cps)[:4], copy_flow=verdict)

def score(r):
    if r["realized"]<=0: return 0
    # per-MAIN-coin style is what matters for copy (26fe round-trips alts but HOLDS btc → uncopyable on btc)
    ms=r.get("main_style","mixed")
    style_mult={"round_trip":1.0,"mixed":0.55,"hold_scale":0.15,"thin":0.4}.get(ms,0.5)
    if r.get("style")=="scalp": style_mult=min(style_mult,0.15)
    base=r["consistency"]*(0.4+0.6*r["main_frac"])
    payoff=min(r["payoff"],4)/4; months=min(r["total_months"],6)/6
    # SIZE INTENTIONALLY EXCLUDED — a $5k clean round-tripper is as copyable as a $5M one for our
    # small book; ranking by realized just smuggled whale-ness back in (user directive 2026-05-26).
    s=base*(0.4+0.6*payoff)*(0.4+0.6*months)*style_mult*100
    fv=r.get("copy_flow","")
    if fv.startswith("POSITIVE"): s*=1.15
    elif fv.startswith("CAUTION"): s*=0.6
    # DURABILITY — the test the pump can't fake (regime-tested >> pump-only)
    if r.get("regime_tested"): s*=1.30
    elif r.get("pump_only"): s*=0.45
    else: s*=0.9
    # ARCHETYPE — copy-lag fragility
    s*={"high_wr":1.05,"trend_rider":0.85,"mixed":1.0}.get(r.get("archetype","mixed"),1.0)
    # DRAWDOWN — blow-up risk (maxdd as a fraction of realized): kills 0x230963(-62%)/739c(-89k)
    dd_ratio=abs(r.get("maxdd",0))/max(r["realized"],1)
    if dd_ratio>0.5: s*=0.45        # catastrophic — has nuked most of its gains at least once
    elif dd_ratio>0.3: s*=0.75      # heavy
    return round(s,1)

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    for r in rows: r["score"]=score(r)
    qual=[r for r in rows if r["realized"]>0 and r["consistency"]>=0.4 and r["top_month_frac"]<0.85]
    qual.sort(key=lambda r:-r["score"])
    # COPYABLE = round-trips THE MAIN COINS (not just alts) + clean fund-flow
    copyable=[r for r in qual if r.get("main_style")=="round_trip" and not r.get("copy_flow","").startswith("CAUTION")]
    mainc=[r for r in copyable if r["main_frac"]>=0.5]
    # 🏆 DURABLE = copyable + regime-tested (positive BEFORE the 2026 pump) — the gold tier
    durable=[r for r in copyable if r.get("regime_tested")]
    with open(SHORT,"w") as f:
        f.write(f"# Deep-scan v2 shortlist\n_updated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | profiled {len(rows)} | qualified {len(qual)} | COPYABLE {len(copyable)} (+BTC/ETH/SOL {len(mainc)}) | 🏆 DURABLE/regime-tested {len(durable)}_\n\n")
        def tbl(lst,title):
            f.write(f"\n## {title} (top {min(40,len(lst))})\n\n| score | addr | main_style | archetype | regime | pre_pnl | WR | payoff | dd% | cons | 1mo% | main% | cad | realized | flow | main_addy | now |\n|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|\n")
            for r in lst[:40]:
                ma=r.get("main_addy",r["addr"]); ma=ma[:8] if ma and ma.lower()!=r["addr"].lower() else "self"
                rg="✅tested" if r.get("regime_tested") else ("⚠️pump-only" if r.get("pump_only") else "partial")
                dd=int(abs(r.get("maxdd",0))/max(r["realized"],1)*100)
                f.write(f"| {r['score']} | `{r['addr'][:10]}…` | {r.get('main_style','?')} | {r.get('archetype','?')} | {rg} | ${r.get('pre_pnl',0):,} | {r['wr']}% | {r['payoff']} | {dd}% | {r['consistency']} | {int(r['top_month_frac']*100)}% | {int(r['main_frac']*100)}% | {r['cadence']}/d | ${r['realized']:,} | {r.get('copy_flow','?')} | {ma} | {','.join(c for c,_,_ in r['now']) or 'flat'} |\n")
        tbl(durable,"🏆 DURABLE — copyable + positive BEFORE the 2026 pump (gold tier)")
        tbl(mainc,"🎯 COPYABLE round-trippers, clean flow, BTC/ETH/SOL")
        tbl(copyable,"✅ COPYABLE round-trippers, clean flow (any coin)")
        tbl(qual,"📊 All qualified (incl. hold_scale/caution — for reference)")

def main():
    log("=== deep scan v2 start ===")
    r=requests.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",timeout=90)
    rows=r.json()["leaderboardRows"]; pool=[]
    for row in rows:
        try:
            av=float(row["accountValue"]); w=dict(row["windowPerformances"])
            m=w["month"]; mr=float(m["roi"]); mv=float(m["vlm"]); mp=float(m["pnl"]); ap=float(w["allTime"]["pnl"])
        except Exception: continue
        if not(10_000<=av<=100_000_000): continue
        if mp<=0 or ap<=0: continue
        turn=mv/av if av else 0
        if not(0.5<=turn<=200): continue
        pool.append((mr, {"addr":row["ethAddress"],"turn":turn}))
    pool.sort(reverse=True); pool=[p[1] for p in pool[:MAX_POOL]]
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    log(f"pool={len(pool)} | already done={len(done)}")
    n=0
    for item in pool:
        a=item["addr"]
        if a in done: continue
        try:
            pr=profile(a,item)
            if pr:
                if pr["realized"]>0 and pr["consistency"]>=0.4:    # only spend a flow-call on qualifiers
                    pr.update(flow(a, pr["realized"]))
                open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e:
            log(f"skip {a[:10]}: {e}")
        done.add(a); n+=1
        if n%25==0:
            json.dump(list(done),open(DONE,"w")); write_shortlist()
            log(f"progress {n}/{len(pool)} processed")
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    log(f"=== DONE: processed {n} this run, total {len(done)} ===")

if __name__=="__main__": main()
