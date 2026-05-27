#!/usr/bin/env python3
"""
SHADOW SLEEVE — generic paper copy-slippage validator (PAPER, read-only, NO capital).

Answers the only question that gates a real sleeve: does this source's edge SURVIVE our copy-lag?
It mirrors the source's directional book on paper — entering/exiting at the mid we observe when we
DETECT the move (realistic poll lag) — and scores FRESH round-trips out-of-sample (current holds are
baseline-seeded, NOT scored). Per round-trip it records our paper return % and our slippage vs his
avg entry. Unlike the live sleeve this tracks ALL coins he trades (incl. illiquid alts) so we see the
slippage the alt legs would actually cost. State persisted.

Config (env): SHADOW_NAME, SHADOW_SOURCE, SHADOW_POLL_S (default 30).
Manage: systemctl {status,stop} hl-shadow-<name> ; journalctl -u hl-shadow-<name> -f
"""
import sys, os, json, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from loguru import logger

API="https://api.hyperliquid.xyz/info"
NAME=os.getenv("SHADOW_NAME","src")
SOURCE=os.getenv("SHADOW_SOURCE","").lower()
POLL_S=int(os.getenv("SHADOW_POLL_S","30"))
EPS=1e-9
STATE=f"/root/hl-bot/data/shadow_{NAME}_state.json"

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
    if not SOURCE:
        logger.error(f"[shadow-{NAME}] SHADOW_SOURCE unset"); sys.exit(1)
    s=_load()
    logger.info(f"[shadow-{NAME}] start | src={SOURCE[:10]}… ALL coins | paper copy-slippage validator | poll {POLL_S}s")
    while True:
        try:
            his=_positions(SOURCE); mids=_post("allMids")
        except Exception as e:
            logger.warning(f"[shadow-{NAME}] read fail (skip): {e}"); time.sleep(POLL_S); continue
        coins=set(s["his"]) | set(his) | set(s["ours"])
        for c in coins:
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
            if held and (went_flat or flip):
                o=s["ours"].pop(c); d=1 if o["dir"]=="L" else -1
                ret=d*(mid-o["entry_mid"])/o["entry_mid"]*100
                s["trades"].append({"coin":c,"dir":o["dir"],"entry_mid":round(o["entry_mid"],4),
                    "exit_mid":round(mid,4),"ret_pct":round(ret,3),"his_entry":round(o["his_entry"],4),
                    "opened":o["opened"],"closed":dt.datetime.now(dt.UTC).strftime("%m-%d %H:%M")})
                w=sum(1 for t in s["trades"] if t["ret_pct"]>0); n=len(s["trades"])
                cum=sum(t["ret_pct"] for t in s["trades"])
                logger.success(f"[shadow-{NAME}] CLOSE {c} {o['dir']} ret {ret:+.2f}% (entry {o['entry_mid']:.4f}→{mid:.4f}) "
                               f"| cum {cum:+.1f}% over {n} trips, WR {w}/{n}")
            if (fresh_open or flip) and c not in s["ours"]:
                d="L" if his_szi>0 else "S"
                slip = (mid-his_entry)/his_entry*100 if his_entry>0 else 0.0
                s["ours"][c]={"dir":d,"entry_mid":mid,"his_entry":his_entry or mid,
                              "opened":dt.datetime.now(dt.UTC).strftime("%m-%d %H:%M")}
                logger.info(f"[shadow-{NAME}] OPEN {c} {d} @ mid {mid:.4f} (his avg {his_entry:.4f}, our slip {slip:+.2f}%)")
            s["his"][c]=his_szi
        if not s.get("seeded"):
            s["seeded"]=True
            logger.info(f"[shadow-{NAME}] baseline-seeded {len([c for c in s['his'] if abs(s['his'][c])>EPS])} live coins (not scored); scoring fresh trips from here")
        _save(s)
        time.sleep(POLL_S)

if __name__=="__main__": main()
