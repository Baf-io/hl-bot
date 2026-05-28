#!/usr/bin/env python3
"""
Stage-2 CONTESTANT DEEP-READ — the "full read of the addy's trader" before we trust a signal.

A single leaderboard address can be ONE LEG of a hedged/market-neutral book — copying it then
copies noise (or the deliberately-losing leg). This resolves the trader's ENTITY and computes
NET economic exposure, so we only keep genuinely DIRECTIONAL traders.

Per contestant:
  HL (free):  entity = addy + linked HL wallets (subAccountTransfer/sends)
              net delta per coin across all legs   → internal-hedge?
              spot balances vs perp                → spot-perp basis / funding-farm?
              userFunding (30d)                    → funding-income farmer?
  EVM (Etherscan V2, multichain): native+stable balances on ETH/Arbitrum, and transfers to
              known CEX deposit addrs or non-HL venues → external-venue/CEX hedge signal (off-HL,
              otherwise invisible). HEURISTIC flag, not proof.

Reads contestants from data/research2/results.jsonl (copyable + clean-flow, size-free score).
Writes data/research2/CONTESTANTS_DEEPREAD.md. Read-only, rate-limited. Etherscan layer is
key-optional (activates iff ETHERSCAN_API_KEY in .env).
"""
import sys, os, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from config import settings

HL="https://api.hyperliquid.xyz/info"
OUT="/root/hl-bot/data/research2"; RES=f"{OUT}/results.jsonl"; DOC=f"{OUT}/CONTESTANTS_DEEPREAD.md"
ETHERSCAN=os.getenv("ETHERSCAN_API_KEY","") or getattr(settings,"ETHERSCAN_API_KEY","")
NANSEN=os.getenv("NANSEN_API_KEY","") or getattr(settings,"NANSEN_API_KEY","")
ES_BASE="https://api.etherscan.io/v2/api"
NANSEN_BASE="https://api.nansen.ai/api/v1"   # VERIFIED live 2026-05-26: header apiKey, POST JSON
CHAINS={"eth":1,"arbitrum":42161}                 # HL deposits bridge from Arbitrum; ETH for the rest
MAIN={"BTC","ETH","SOL"}
HL_SYS_PREFIX="0x2000000000000000000000000000000000000"
HL_SYS={"0x2222222222222222222222222222222222222222"}
HL_BRIDGE={"0x2df1c51e09aecf9cacb7bc98cb1742757f163df7"}   # Arbitrum HL bridge — deposits, NOT a hedge
# Well-known CEX deposit/hot wallets (lowercased) — transfers here = funding a CEX (possible hedge venue)
CEX={"0x28c6c06298d514db089934071355e5743bf21d60":"Binance",
     "0x21a31ee1afc51d94c2efccaa2092ad1028285549":"Binance",
     "0xdfd5293d8e347dfe59e90efd55b2956a1343963d":"Binance",
     "0xf89d7b9c864f589bbf53a82105107622b35eaa40":"Bybit",
     "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b":"OKX",
     "0x71660c4005ba85c37ccec55d0c4493e66fe775d3":"Coinbase"}

_t=[0.0]
def _throt(s=0.25):
    d=time.time()-_t[0];
    if d<s: time.sleep(s-d)
    _t[0]=time.time()
def hl(t,**k):
    for _ in range(4):
        try:
            _throt(); r=requests.post(HL,json={"type":t,**k},timeout=25).json()
            if r is not None: return r
        except Exception: time.sleep(1.0)
    return None
def es(chainid,**params):
    if not ETHERSCAN: return None
    p={"chainid":chainid,"apikey":ETHERSCAN,**params}
    for _ in range(3):
        try:
            _throt(0.22); r=requests.get(ES_BASE,params=p,timeout=20).json(); return r
        except Exception: time.sleep(0.5)
    return None
def _is_sys(a):
    a=(a or "").lower(); return a in HL_SYS or a.startswith(HL_SYS_PREFIX)

def linked_wallets(addr):
    """subAccountTransfer = proven sub; sends/spotTransfer to non-system = candidate sibling."""
    upd=hl("userNonFundingLedgerUpdates",user=addr) or []
    subs=set(); sibs=set()
    for u in upd:
        d=u.get("delta",{}) or {}; tp=d.get("type")
        if tp=="subAccountTransfer":
            for f in ("destination","user"):
                v=(d.get(f) or "").lower()
                if v and v!=addr.lower(): subs.add(v)
        elif tp in ("send","spotTransfer","internalTransfer"):
            v=(d.get("destination") or "").lower()
            if v and v!=addr.lower() and not _is_sys(v): sibs.add(v)
    return subs, sibs

def perp_net(addr):
    """signed notional per coin for an HL address."""
    st=hl("clearinghouseState",user=addr) or {}
    out={}
    for ap in st.get("assetPositions",[]):
        p=ap["position"]; sz=float(p.get("szi",0));
        if sz==0: continue
        px=float(p.get("entryPx") or 0); out[p["coin"]]=sz*px
    return out

def spot_holdings(addr):
    st=hl("spotClearinghouseState",user=addr) or {}
    out={}
    for b in st.get("balances",[]):
        try: out[b["coin"]]=float(b.get("total",0))
        except Exception: pass
    return out

def funding_30d(addr):
    start=int((dt.datetime.now(dt.UTC)-dt.timedelta(days=30)).timestamp()*1000)
    fu=hl("userFunding",user=addr,startTime=start) or []
    tot=0.0
    for f in fu:
        try: tot+=float(f.get("delta",{}).get("usdc",0))
        except Exception: pass
    return tot

def evm_read(addr):
    """External-venue signal via Etherscan V2. Heuristic, not proof."""
    if not ETHERSCAN: return {"evm":"(no key)"}
    flags=[]; cex_hits=set(); ext_native=0.0
    for name,cid in CHAINS.items():
        bal=es(cid,module="account",action="balance",address=addr,tag="latest")
        try:
            if bal and bal.get("status")=="1": ext_native+=int(bal["result"])/1e18
        except Exception: pass
        tx=es(cid,module="account",action="txlist",address=addr,startblock=0,endblock=99999999,page=1,offset=200,sort="desc")
        if tx and isinstance(tx.get("result"),list):
            for t in tx["result"]:
                to=(t.get("to") or "").lower()
                if to in CEX: cex_hits.add(CEX[to])
                elif to and to not in HL_BRIDGE and not _is_sys(to) and t.get("input","0x")!="0x":
                    pass  # contract interaction (other venue) — counted loosely below
    if cex_hits: flags.append("CEX:"+",".join(sorted(cex_hits)))
    if ext_native>0.05: flags.append(f"ext-ETH {ext_native:.2f}")
    return {"evm":"; ".join(flags) if flags else "none", "cex_linked":bool(cex_hits)}

def _nansen(ep, addr):
    if not NANSEN: return None
    try:
        r=requests.post(f"{NANSEN_BASE}/{ep}", json={"address":addr,"chain":"ethereum"},
                        headers={"apiKey":NANSEN,"Content-Type":"application/json"}, timeout=20)
        return r.json() if r.ok else None
    except Exception: return None

def nansen_related(addr):
    """related-wallets — CHEAP (~1 credit). Returns set of related addresses."""
    j=_nansen("profiler/address/related-wallets", addr)
    if not j: return set()
    return {(w.get("address") or "").lower() for w in (j.get("data") or []) if w.get("address")} - {addr.lower(), ""}

def nansen_labels(addr):
    """labels — EXPENSIVE (~100 credits). Gated to top survivors only. Returns dict."""
    j=_nansen("profiler/address/labels", addr)
    if not j: return {"nansen":"?", "fund_label":False, "smart_money":False}
    labels=[((d.get("label") or "").strip(),(d.get("category") or "").strip()) for d in (j.get("data") or []) if d.get("label")]
    text=" ".join(f"{l}/{c}" for l,c in labels).lower()
    HEDGER=("exchange","cex","market maker","market-maker","trading firm","desk","fund","institution")
    return {"nansen": (",".join(l for l,_ in labels[:4]) or "no-labels"),
            "fund_label": any(k in text for k in HEDGER),
            "smart_money": ("smart money" in text or "smart trader" in text)}

def verdict(net, spot, funding, evm, nan=None):
    # net per-coin across entity; spot offset; funding farm; external venue
    hedged=[c for c,v in net.items() if c in MAIN and abs(v)<max(abs(x) for x in net.values() if x)*0.15] if net else []
    basis=[c for c in MAIN if net.get(c,0)<0 and spot.get(c,0)>0]   # perp short + spot long = basis
    tags=[]
    if nan and nan.get("fund_label"): tags.append(f"⚠️Nansen-label:{nan.get('nansen')}")
    if evm.get("cex_linked"): tags.append("⚠️CEX-linked(off-HL hedge?)")
    if basis: tags.append("⚠️basis/funding-farm "+",".join(basis))
    if funding>500: tags.append(f"⚠️funding-income ${funding:.0f}/30d")
    # net direction across main coins
    netmain=sum(v for c,v in net.items() if c in MAIN)
    if not tags and abs(netmain)>50:
        tags.append(f"✅net-directional (${netmain:+,.0f} main)")
    elif not tags:
        tags.append("flat/none")
    return "; ".join(tags)

def load_contestants():
    if not os.path.exists(RES): return []
    rows=[json.loads(l) for l in open(RES)]
    import importlib.util
    spec=importlib.util.spec_from_file_location("ds","/root/hl-bot/scripts/deep_scan.py")
    ds=importlib.util.module_from_spec(spec); spec.loader.exec_module(ds)
    for r in rows: r["score"]=ds.score(r)
    c=[r for r in rows if r.get("main_style")=="round_trip" and not (r.get("copy_flow") or "").startswith("CAUTION") and r["realized"]>0]
    c.sort(key=lambda r:(-int(bool(r.get("regime_tested"))), -r["score"]))   # durable first
    return c[:40]

MAX_LABELS=int(os.getenv("NANSEN_MAX_LABELS","10"))   # 100cr each — gate to top net-directional survivors

def main():
    cons=load_contestants()
    labels_left=MAX_LABELS if NANSEN else 0; cr_related=0; cr_labels=0
    print(f"deep-reading {len(cons)} contestants | etherscan={'ON' if ETHERSCAN else 'OFF'} | "
          f"nansen={'ON' if NANSEN else 'OFF'} | label budget={MAX_LABELS} (×100cr)")
    rowsout=[]; survivors=0
    for r in cons:
        a=r["addr"]; subs,sibs=linked_wallets(a)
        related=set()
        if NANSEN: related=nansen_related(a); cr_related+=1    # 1cr — entity expansion for all
        entity=[a]+sorted(subs|sibs|related)
        net=defaultdict(float)
        for w in entity:
            for c,v in perp_net(w).items(): net[c]+=v
        spot=spot_holdings(a); fund=funding_30d(a); evm=evm_read(a)
        netmain=sum(val for c,val in net.items() if c in MAIN)
        net_directional = abs(netmain)>50 and not evm.get("cex_linked") and not [c for c in MAIN if net.get(c,0)<0 and spot.get(c,0)>0]
        if net_directional: survivors+=1
        # LABELS (100cr) only for the top net-directional survivors, under budget
        nan={"nansen":"(not-labeled)","fund_label":False,"smart_money":False}
        if NANSEN and net_directional and labels_left>0:
            nan=nansen_labels(a); cr_labels+=100; labels_left-=1
        v=verdict(net,spot,fund,evm,nan)
        rg="✅" if r.get("regime_tested") else ("pump" if r.get("pump_only") else "part")
        sh=",".join(c for c in MAIN if net.get(c,0)<0 and spot.get(c,0)>0) or "-"
        sm="⭐" if nan.get("smart_money") else ""
        rowsout.append(f"| `{a[:10]}…` | {rg} | {len(subs)}s/{len(sibs)}x/{len(related)}n | ${netmain:+,.0f} | {sh} | ${fund:+.0f} | {evm.get('evm')} | {nan.get('nansen')}{sm}({len(related)}) | {v} |")
        print(f"  {a[:10]} → {v}")
    est=cr_related+cr_labels
    lines=[f"# Contestant deep-read — entity net-delta + hedge/basis + cross-chain",
           f"_generated {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(cons)} contestants | EVM {'ON' if ETHERSCAN else 'OFF'} | Nansen {'ON' if NANSEN else 'OFF'} | ~{est} credits this run (related {cr_related}×1 + labels {cr_labels//100}×100)_",
           "\n| addr | regime | linked(s/x/nansen) | net-main$ | spot-hedge | funding30d | EVM | Nansen-labels | VERDICT |",
           "|--|--|--|--|--|--|--|--|--|"]+rowsout
    open(DOC,"w").write("\n".join(lines)+"\n")
    print(f"wrote {DOC} | est ~{est} Nansen credits used (related {cr_related} + labels {cr_labels//100})")
    # phone push — pipeline (scan + deep-read) is fully done
    topic=os.getenv("NTFY_TOPIC") or getattr(settings,"NTFY_TOPIC","")
    if topic:
        try:
            requests.post(f"https://ntfy.sh/{topic}",
                data=f"Deep-read done: {len(cons)} contestants, {survivors} net-directional survivors. ~{est} Nansen credits used. CONTESTANTS_DEEPREAD.md ready.".encode(),
                headers={"Title":"HL research pipeline complete","Priority":"default","Tags":"white_check_mark"}, timeout=10)
        except Exception as e: print("ntfy push failed:", e)

if __name__=="__main__": main()
