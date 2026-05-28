#!/usr/bin/env python3
"""
SHADOW — 0xb65dd7c5 copy-slippage validator (PAPER, read-only, NO capital).

Answers the only question that gates a fast sleeve: does this intraday source's edge SURVIVE our
copy-lag? It mirrors his directional book on LIQUID coins on paper — entering/exiting at the mid we
observe when we DETECT his move (i.e. with realistic poll lag) — and scores the FRESH round-trips
out-of-sample (his current holds are baseline-seeded, NOT scored). For each round-trip it records:
  our paper return % (mirroring at detection-time prices) — the realized copyable edge
  our entry mid vs his avg entry — the slippage we eat vs him
Cumulative paper return + win-rate tell us if a real sleeve would have made money. State persisted.

Manage: systemctl {status,stop} hl-shadow-b65d ; journalctl -u hl-shadow-b65d -f
"""
import sys, os, json, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from loguru import logger

API="https://api.hyperliquid.xyz/info"
SOURCE="0xb65dd7c56afbf3b272ab5fc49be44b47dca18003"
POLL_S=20
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","kPEPE"}
EPS=1e-9
STATE="/root/hl-bot/data/shadow_b65d_state.json"

def _load():
    try: return json.load(open(STATE))
    except Exception: return {"his":{}, "ours":{}, "trades":[], "seeded":False}
def _save(d): json.dump(d, open(STATE,"w"))

def _post(t,**k):
    for _ in range(5):
        try:
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: pass
        time.sleep(1.5)
    raise RuntimeError("api fail")

def _positions(addr):
    st=_post("clearinghouseState",user=addr); out={}
    for ap in st.get("assetPositions",[]):
        p=ap["position"]; out[p["coin"]]=(float(p["szi"]), float(p.get("entryPx") or 0))
    return out

def main():
    s=_load()
    logger.info(f"[shadow-b65d] start | src={SOURCE[:10]}… liquid-only | paper copy-slippage validator | poll {POLL_S}s")
    while True:
        try:
            his=_positions(SOURCE); mids=_post("allMids")
        except Exception as e:
            logger.warning(f"[shadow-b65d] read fail (skip): {e}"); time.sleep(POLL_S); continue
        coins=set(s["his"]) | set(his) | set(s["ours"])
        for c in coins:
            if c not in LIQUID: continue
            try: mid=float(mids.get(c,0) or 0)
            except Exception: mid=0.0
            if mid<=0: continue
            his_szi=his.get(c,(0.0,0.0))[0]; his_entry=his.get(c,(0.0,0.0))[1]
            prev=s["his"].get(c,0.0)
            if not s.get("seeded"):
                s["his"][c]=his_szi; continue
            fresh_open = abs(prev)<EPS and abs(his_szi)>=EPS
            flip       = abs(prev)>=EPS and abs(his_szi)>=EPS and (prev>0)!=(his_szi>0)
            went_flat  = abs(prev)>=EPS and abs(his_szi)<EPS
            held = c in s["ours"]
            # close our paper leg on his flat/flip
            if held and (went_flat or flip):
                o=s["ours"].pop(c); d=1 if o["dir"]=="L" else -1
                ret=d*(mid-o["entry_mid"])/o["entry_mid"]*100
                s["trades"].append({"coin":c,"dir":o["dir"],"entry_mid":round(o["entry_mid"],4),
                    "exit_mid":round(mid,4),"ret_pct":round(ret,3),"his_entry":round(o["his_entry"],4),
                    "opened":o["opened"],"closed":dt.datetime.now(dt.UTC).strftime("%m-%d %H:%M")})
                w=sum(1 for t in s["trades"] if t["ret_pct"]>0); n=len(s["trades"])
                cum=sum(t["ret_pct"] for t in s["trades"])
                logger.success(f"[shadow-b65d] CLOSE {c} {o['dir']} ret {ret:+.2f}% (entry {o['entry_mid']:.4f}→{mid:.4f}) "
                               f"| cum {cum:+.1f}% over {n} trips, WR {w}/{n}")
            # open a fresh paper leg in his direction (mirror at detection mid)
            if (fresh_open or flip) and c not in s["ours"]:
                d="L" if his_szi>0 else "S"
                slip = (mid-his_entry)/his_entry*100 if his_entry>0 else 0.0
                s["ours"][c]={"dir":d,"entry_mid":mid,"his_entry":his_entry or mid,
                              "opened":dt.datetime.now(dt.UTC).strftime("%m-%d %H:%M")}
                logger.info(f"[shadow-b65d] OPEN {c} {d} @ mid {mid:.4f} (his avg {his_entry:.4f}, our slip {slip:+.2f}%)")
            s["his"][c]=his_szi
        if not s.get("seeded"):
            s["seeded"]=True
            logger.info(f"[shadow-b65d] baseline-seeded {len([c for c in s['his'] if abs(s['his'][c])>EPS])} live coins (not scored); scoring fresh trips from here")
        _save(s)
        time.sleep(POLL_S)

if __name__=="__main__": main()
