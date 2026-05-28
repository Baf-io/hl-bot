#!/usr/bin/env python3
"""
INTRADAY SOURCE SCANNER — finds COPYABLE intraday traders to feed a low-latency fast sleeve.

The copy-bot thesis (see [[copy-alpha-ceiling]]): a 45s reconcile can't copy a scalper, and even a
low-latency mirror can't copy sub-minute HFT (the edge IS the timing we'd lag). So this scanner
hunts the narrow COPYABLE INTRADAY band:
  - genuinely intraday (most positions closed <1h)  ... but
  - NOT HFT  (median hold >= a floor; few sub-5-min round-trips)
  - high PER-TRADE win rate  (fast trades have no long tail to ride — robustness is per-entry)
  - large sample + positive realized + regime-tested + low drawdown + liquid coins (slippage).

Method: hold-time/cadence is reconstructed from signed-fill episodes over a recent fills window
(robust to TWAP/partials/flips); EDGE stats (wr/payoff/realized/regime/dd) are reused from the
deep_scan profile in data/research2/results.jsonl (full-history). Merge → score → shortlist.

FREE — HL public API only, read-only. Output: data/research2/intraday_shortlist.md
"""
import sys, os, json, time, math, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
RES="/root/hl-bot/data/research2/results.jsonl"
OUT="/root/hl-bot/data/research2/intraday_shortlist.md"
MIN_INT=0.35; _last=[0.0]
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","PEPE","kPEPE"}

def _t():
    d=time.time()-_last[0]
    if d<MIN_INT: time.sleep(MIN_INT-d)
    _last[0]=time.time()
def post(t,**k):
    for _ in range(5):
        try:
            _t(); j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None: return j
        except Exception: time.sleep(1.0)
    return None

def fills(addr, days=int(os.getenv("INTRA_DAYS","90")), max_pages=int(os.getenv("INTRA_PAGES","10"))):
    out=[]; end=int(time.time()*1000); cur=end-days*86400*1000
    for _ in range(max_pages):
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

def hold_stats(addr):
    """signed-fill episode reconstruction → hold-time distribution + liquid-coin fraction."""
    fl=fills(addr)
    if len(fl)<20: return None
    run=defaultdict(float); t0=defaultdict(float); EPS=1e-9
    closed=[]; coin_hits=defaultdict(int)
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); d=sz if f.get("side")=="B" else -sz
        prev=run[c]; run[c]=round(prev+d,8); cur=run[c]
        if abs(prev)<EPS and abs(cur)>=EPS:
            t0[c]=f["time"]; coin_hits[c]+=1
        elif abs(prev)>=EPS and abs(cur)<EPS:
            closed.append((c,(f["time"]-t0[c])/3.6e6))
        elif abs(prev)>=EPS and abs(cur)>=EPS and (prev>0)!=(cur>0):
            closed.append((c,(f["time"]-t0[c])/3.6e6)); t0[c]=f["time"]; coin_hits[c]+=1
    if len(closed)<10: return None
    hs=sorted(h for _,h in closed)
    n=len(hs); med=hs[n//2]
    sub5=sum(1 for h in hs if h<1/12)/n           # <5 min
    intr=sum(1 for h in hs if h<1)/n              # <1 h
    mid=sum(1 for h in hs if 1<=h<6)/n            # 1-6 h
    multi=sum(1 for h in hs if h>=24)/n
    liq=sum(v for c,v in coin_hits.items() if c in LIQUID or c.startswith("k"))/(sum(coin_hits.values()) or 1)
    tag="SCALPER" if intr>=0.6 else ("SWING" if multi>=0.5 else "MIXED")
    return dict(n_closed=n, med_h=round(med,2), pct_sub5=round(sub5,2), pct_intraday=round(intr,2),
                pct_1to6h=round(mid,2), pct_multiday=round(multi,2), liq_frac=round(liq,2), tag=tag,
                hot_coins=[c for c,_ in sorted(coin_hits.items(),key=lambda x:-x[1])[:4]])

def score(p):
    if p["realized"]<=0 or p["n_closed"]<20: return 0
    if p["med_h"] < 0.08: return 0                # <~5 min median = HFT, uncopyable even low-latency
    wr=p["wr"]/100
    intr=p["pct_intraday"]
    sample=min(p["n_closed"],250)/250
    s=100*wr*(0.45+0.55*intr)*(0.5+0.5*sample)*(1-0.5*p["pct_sub5"])*(0.5+0.5*p["liq_frac"])
    if p.get("regime_tested"): s*=1.2
    dd=abs(p.get("maxdd",0))/max(p["realized"],1)
    if dd>0.5: s*=0.4
    elif dd>0.3: s*=0.7
    return round(s,1)

def main():
    rows=[json.loads(l) for l in open(RES)]
    # cheap pre-filter to intraday-likely (fast cadence or scalp style, real positive edge)
    cand=[r for r in rows if r.get("realized",0)>0 and (r.get("style")=="scalp" or r.get("cadence",0)>=3)
          and r.get("wr",0)>=50]
    extra=os.getenv("INTRA_EXTRA","")
    if extra:  # allow injecting addresses to also profile
        have={r["addr"].lower() for r in cand}
        cand+=[{"addr":a} for a in extra.split(",") if a.strip() and a.lower() not in have]
    print(f"[intraday] pre-filter {len(cand)} intraday-likely; computing hold-cadence…")
    merged=[]
    for i,r in enumerate(cand):
        hs=hold_stats(r["addr"])
        if not hs:
            continue
        m=dict(r); m.update(hs); m["iscore"]=score(m); merged.append(m)
        if (i+1)%10==0: print(f"  {i+1}/{len(cand)} profiled")
    merged.sort(key=lambda m:-m["iscore"])
    with open(OUT,"w") as f:
        f.write(f"# Intraday COPYABLE-source shortlist — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(merged)} profiled\n")
        f.write("_Sweet spot = intraday but NOT sub-5-min HFT; high per-trade WR; liquid coins; regime-tested._\n\n")
        f.write("| iscore | addr | tag | med_h | sub5% | intra% | 1-6h% | WR | payoff | n_cl | dd% | liq% | regime | realized | hot coins |\n")
        f.write("|--|--|--|--|--|--|--|--|--|--|--|--|--|--|--|\n")
        for m in merged[:40]:
            dd=int(abs(m.get("maxdd",0))/max(m["realized"],1)*100)
            rg="✅" if m.get("regime_tested") else "recent"
            f.write(f"| {m['iscore']} | `{m['addr'][:10]}…` | {m['tag']} | {m['med_h']}h | {int(m['pct_sub5']*100)}% | {int(m['pct_intraday']*100)}% | {int(m['pct_1to6h']*100)}% | {m['wr']}% | {m['payoff']} | {m['n_closed']} | {dd}% | {int(m['liq_frac']*100)}% | {rg} | ${m['realized']:,} | {','.join(m['hot_coins'])} |\n")
    print(f"[intraday] done → {OUT}")
    # echo top 12 to stdout
    print(f"\n{'addr':12} {'tag':8} {'med_h':>6} {'sub5':>5} {'intra':>6} {'WR':>4} {'pay':>5} {'n':>4} {'dd':>5} {'liq':>5} {'iscore':>6}  coins")
    for m in merged[:12]:
        dd=int(abs(m.get("maxdd",0))/max(m["realized"],1)*100)
        print(f"{m['addr'][:12]} {m['tag']:8} {m['med_h']:>5}h {int(m['pct_sub5']*100):>4}% {int(m['pct_intraday']*100):>5}% {m['wr']:>3}% {m['payoff']:>5} {m['n_closed']:>4} {dd:>4}% {int(m['liq_frac']*100):>4}% {m['iscore']:>6}  {','.join(m['hot_coins'])}")

if __name__=="__main__": main()
