#!/usr/bin/env python3
"""
CANDIDATE DOSSIER — enrich each trader with everything we know, in a readable report.

For each address: merges the deep_scan profile (full-history edge: wr/payoff/realized/regime/dd)
with freshly-computed hold-time + a NET-DIRECTIONAL-vs-MARKET-MAKER read + current live positions,
then prints a plain-English copyability verdict. This is the "let me see the candidates" view.

Directional vs MM read (the key copyability gate beyond hold-time):
  - flips_per_day: a market-maker flips constantly around flat; a directional trader holds a side.
  - extend_ratio: fraction of fills that GROW net exposure vs offset it (directional builds & holds).
  - net_usd_now: current signed $ exposure (directional traders carry it; MMs sit ~flat).

Usage: DOSSIER_ADDRS="0xaaa,0xbbb" python scripts/candidate_dossier.py
       (defaults to the top intraday candidates if unset)
FREE — HL public API, read-only. Output: data/research2/DOSSIER.md
"""
import sys, os, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
RES="/root/hl-bot/data/research2/results.jsonl"
OUT="/root/hl-bot/data/research2/DOSSIER.md"
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","PEPE","kPEPE"}
_last=[0.0]
def post(t,**k):
    for _ in range(5):
        try:
            d=time.time()-_last[0]
            if d<0.3: time.sleep(0.3-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None: return j
        except Exception: time.sleep(1.0)
    return None

def fills(addr, days=120, pages=12):
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

def analyze(addr):
    fl=fills(addr)
    if len(fl)<20: return None
    run=defaultdict(float); t0=defaultdict(float); EPS=1e-9
    closed=[]; coin_hits=defaultdict(int); flips=0; extend=0; reduce_=0
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); d=sz if f.get("side")=="B" else -sz
        prev=run[c]; run[c]=round(prev+d,8); cur=run[c]
        if abs(cur)>abs(prev): extend+=1
        else: reduce_+=1
        if abs(prev)<EPS and abs(cur)>=EPS:
            t0[c]=f["time"]; coin_hits[c]+=1
        elif abs(prev)>=EPS and abs(cur)<EPS:
            closed.append((f["time"]-t0[c])/3.6e6)
        elif abs(prev)>=EPS and abs(cur)>=EPS and (prev>0)!=(cur>0):
            closed.append((f["time"]-t0[c])/3.6e6); t0[c]=f["time"]; coin_hits[c]+=1; flips+=1
    if len(closed)<5: return None
    span=(fl[-1]["time"]-fl[0]["time"])/86400000 or 1
    hs=sorted(closed); n=len(hs); med=hs[n//2]
    # current live positions → net $ exposure now
    cs=post("clearinghouseState",user=addr) or {}
    mids=post("allMids") or {}
    netusd=0.0; poslist=[]
    for ap in cs.get("assetPositions",[]):
        p=ap["position"]; szi=float(p["szi"]); c=p["coin"]
        mid=float(mids.get(c,0) or p.get("entryPx") or 0); val=szi*mid
        netusd+=val; poslist.append(f"{c} {'L' if szi>0 else 'S'} ${abs(val):,.0f}")
    return dict(
        n_closed=n, med_h=round(med,2),
        pct_sub5=round(sum(1 for h in hs if h<1/12)/n,2),
        pct_intraday=round(sum(1 for h in hs if h<1)/n,2),
        pct_multiday=round(sum(1 for h in hs if h>=24)/n,2),
        flips_per_day=round(flips/span,2),
        extend_ratio=round(extend/max(extend+reduce_,1),2),
        liq_frac=round(sum(v for c,v in coin_hits.items() if c in LIQUID or c.startswith("k"))/(sum(coin_hits.values()) or 1),2),
        hot_coins=[c for c,_ in sorted(coin_hits.items(),key=lambda x:-x[1])[:5]],
        net_usd_now=round(netusd), positions=poslist[:6])

def verdict(p, prof):
    dd=abs(prof.get("maxdd",0))/max(prof.get("realized",1),1)
    if p["med_h"]<0.08 or p["pct_sub5"]>=0.6:
        return "❌ HFT/MM — UNCOPYABLE (latency/spread edge; lagged copy = exit liquidity)"
    if abs(p["net_usd_now"])<50 and p["flips_per_day"]>4 and p["med_h"]<0.5:
        return "❌ likely MARKET-MAKER — sits ~flat, flips fast, no net direction to mirror"
    if p["liq_frac"]<0.3:
        return "⚠️ ILLIQUID coins — edge real but slippage would eat a copy"
    if dd>0.5:
        return "⚠️ FRAGILE — drawdown > 50% of realized; tail risk"
    if 0.1<=p["med_h"]<=12 and p["pct_intraday"]>=0.4:
        return "✅ COPYABLE INTRADAY — directional, liquid enough, holds long enough to mirror"
    return "➖ REVIEW — doesn't cleanly fit copyable-intraday"

def main():
    profs={json.loads(l)["addr"].lower():json.loads(l) for l in open(RES)}
    byprefix={a[:12]:a for a in profs}
    default=["0xb65dd7c5","0x3354143a","0xdc7bf4d1","0x618d6943","0x1666b6d0","0x12203316"]
    addrs=[a.strip() for a in os.getenv("DOSSIER_ADDRS","").split(",") if a.strip()] or default
    full=[]
    for a in addrs:
        m=[v for k,v in profs.items() if k.startswith(a.lower())]
        full.append(m[0] if m else a)
    lines=[f"# Candidate dossier — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC}\n"]
    for item in full:
        addr=item if isinstance(item,str) else item["addr"]
        prof=item if isinstance(item,dict) else {}
        p=analyze(addr)
        if not p:
            lines.append(f"\n## `{addr}`\n_insufficient fills to profile_\n"); continue
        v=verdict(p,prof)
        dd=int(abs(prof.get("maxdd",0))/max(prof.get("realized",1),1)*100) if prof else 0
        lines.append(f"\n## `{addr}`\n**{v}**\n")
        lines.append(f"- **Edge** (full history): WR {prof.get('wr','?')}% · payoff {prof.get('payoff','?')} · "
                     f"realized ${prof.get('realized',0):,} · drawdown {dd}% · regime {'✅tested' if prof.get('regime_tested') else 'recent'} · "
                     f"style {prof.get('main_style','?')}")
        lines.append(f"- **Speed**: median hold **{p['med_h']}h** · sub-5min {int(p['pct_sub5']*100)}% · intraday {int(p['pct_intraday']*100)}% · "
                     f"multiday {int(p['pct_multiday']*100)}% · {p['n_closed']} closed")
        lines.append(f"- **Directional vs MM**: net exposure now **${p['net_usd_now']:,}** ({', '.join(p['positions']) or 'flat'}) · "
                     f"flips/day {p['flips_per_day']} · extend-ratio {p['extend_ratio']} (>0.5 = builds & holds)")
        lines.append(f"- **Liquidity**: {int(p['liq_frac']*100)}% on liquid coins · trades: {', '.join(p['hot_coins'])}")
    open(OUT,"w").write("\n".join(lines)+"\n")
    print("\n".join(lines))
    print(f"\n[dossier] → {OUT}")

if __name__=="__main__": main()
