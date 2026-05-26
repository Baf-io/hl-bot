#!/usr/bin/env python3
"""
Tracker scan — gathers high-signal state from FREE keyless sources (HL API + Pyth + local
shadow state) and APPENDS a findings block to data/tracker_findings.md ONLY WHEN SOMETHING
CHANGED (78aa moves, a candidate opens/closes a coin, a shadow round-trip closes, or our
book changes) — plus a ~12h heartbeat. Keeps the log signal-dense, not a tick spam.

Run on a loop (hl-tracker-scan.service). Keyless; Etherscan/Nansen layers plug in later.
"""
import sys, json, os, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from config import settings

API="https://api.hyperliquid.xyz/info"
LOG="/root/hl-bot/data/tracker_findings.md"
PREV="/root/hl-bot/data/tracker_scan_prev.json"
POLL_S=900            # 15 min
HEARTBEAT_S=43200     # force a log line at least every 12h
ME=settings.HL_WALLET_ADDRESS
SRC="0x78aa6328eae8028a089c35d2819f79c78de2a7e5"
CANDS={"ca41":"0x0ca4109c94438dde8d7386da27180f414de80fa8",
       "2c5d":"0x2c5dc7e67fce4bc252221c46c1cdc7651838b2d8",
       "78dc":"0x78dcfb97b85b83b92c1b1a2d91b4e6cd03a4e120",
       "36f2":"0x36f26e2e5bed062968c17fc770863fd740713205",
       "05c6":"0x05c6959d997cde99d2f967367e5e9f1959ba9706"}

def post(t,**k): return requests.post(API,json={"type":t,**k},timeout=20).json()
def book(addr):
    cs=post("clearinghouseState",user=addr)
    return ({p["position"]["coin"]:round(float(p["position"]["szi"]),5) for p in cs.get("assetPositions",[])},
            float(cs["marginSummary"]["accountValue"]))
def pyth(sym):
    try:
        r=requests.get("https://hermes.pyth.network/v2/price_feeds",params={"query":sym,"asset_type":"crypto"},timeout=10).json()
        fid=next((f["id"] for f in r if f["attributes"].get("base","").upper()==sym and f["attributes"].get("quote_currency")=="USD"),None)
        if not fid: return None
        d=requests.get("https://hermes.pyth.network/v2/updates/price/latest",params=[("ids[]",fid)],timeout=10).json()
        p=d["parsed"][0]["price"]; v=int(p["price"])*10**int(p["expo"]); return v if v>0 else None
    except Exception: return None

def snapshot():
    snap={"cand":{}}
    op,oav=book(ME); snap["our"]={c:s for c,s in op.items()}; snap["oeq"]=round(oav)
    hp,_=book(SRC); snap["his_btc"]=hp.get("BTC",0)
    for lbl,a in CANDS.items():
        try: cp,cav=book(a); snap["cand"][lbl]={"coins":sorted(cp.keys()),"eq":round(cav)}
        except Exception: snap["cand"][lbl]={"coins":["ERR"],"eq":0}
    sf="/root/hl-bot/data/shadow_scan_state.json"
    snap["trips"]={}
    if os.path.exists(sf):
        s=json.load(open(sf))
        for lbl,d in s.items(): snap["trips"][lbl]={"n":d["n"],"cum":round(d["cum_ret"],1),"wins":d["wins"]}
    return snap

def diffs(prev,cur):
    out=[]
    if not prev: return ["(baseline snapshot)"]
    if prev.get("his_btc")!=cur["his_btc"]:
        out.append(f"78aa BTC {prev.get('his_btc')}→**{cur['his_btc']}** (mirror target {0.00777*cur['his_btc']:+.5f})")
    if prev.get("our")!=cur["our"]:
        out.append(f"OUR book {prev.get('our')}→**{cur['our']}** (eq ${cur['oeq']:,})")
    for lbl in CANDS:
        pc=set(prev.get("cand",{}).get(lbl,{}).get("coins",[])); cc=set(cur["cand"][lbl]["coins"])
        if pc!=cc:
            opened=cc-pc; closed=pc-cc
            d=[];
            if opened: d.append("opened "+",".join(opened))
            if closed: d.append("closed "+",".join(closed))
            out.append(f"**{lbl}** {' / '.join(d)}")
        pt=prev.get("trips",{}).get(lbl,{}).get("n",0); ct=cur["trips"].get(lbl,{}).get("n",0)
        if ct>pt:
            t=cur["trips"][lbl]; out.append(f"**{lbl}** shadow +{ct-pt} round-trip(s) → {ct} total, {t['wins']/ct*100:.0f}%WR {t['cum']:+.1f}%")
    return out

def main():
    prev=json.load(open(PREV)) if os.path.exists(PREV) else None
    last_t=prev.get("_t",0) if prev else 0
    while True:
        try:
            cur=snapshot()
        except Exception as e:
            time.sleep(POLL_S); continue
        ch=diffs(prev,cur)
        force=time.time()-last_t>HEARTBEAT_S
        if ch or force or prev is None:
            now=dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
            px={s:pyth(s) for s in ("BTC","HYPE","SOL")}
            head=f"\n## {now}" + (" · heartbeat" if (force and not ch) else "")
            lines=[head, "- prices: "+" | ".join(f"{s} ${v:,.0f}" for s,v in px.items() if v)]
            lines.append("- changes: "+("; ".join(ch) if ch else "none (heartbeat)"))
            lines.append("- candidates: "+" ".join(f"{l}(${cur['cand'][l]['eq']//1000}k:{','.join(cur['cand'][l]['coins']) or 'flat'})" for l in CANDS))
            if not os.path.exists(LOG):
                open(LOG,"w").write("# Tracker findings log\n\n_Auto-appended by tracker_scan.py — change-triggered (free HL+Pyth). Newest at bottom._\n")
            open(LOG,"a").write("\n".join(lines)+"\n")
            last_t=time.time()
        cur["_t"]=last_t; json.dump(cur,open(PREV,"w")); prev=cur
        time.sleep(POLL_S)

if __name__=="__main__": main()
