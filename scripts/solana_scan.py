#!/usr/bin/env python3
"""
SOLANA trader scan — same profile-shaping playbook as the HL deep-scan, adapted to Solana.

KEY DIFFERENCE FROM HL: there is no single Solana "leaderboard + fills" API. Trading is spread
across Jupiter/Raydium (spot) and Drift/Jupiter-Perps/Zeta (perps). So this framework splits into:

  DISCOVERY  — get a pool of candidate wallets:
     • helius_recent_swappers()  — free: pull recent Jupiter swap feePayers (active spot traders)
     • nansen_smart_money()      — DEFERRED (costs credits): Nansen Smart Money lists for Solana
     • (perp path) drift_leaderboard() — TODO: Drift/Jup-Perps protocol leaderboards (copyable analog)

  PROFILING  — reconstruct each wallet's behavior from Helius parsed transactions (FREE):
     each SWAP exposes tokenTransfers/nativeTransfers → net flows → buy/sell events per token,
     priced in the swap's own quote leg (USDC/USDT=$1, SOL via Pyth). Self-pricing, no extra calls.

  SCORING    — reuse the HL lessons: per-token round_trip vs hold_scale vs scalp style, cadence,
     hold time, realized PnL (quote-denominated), win-rate, drawdown. (Durability/pre-pump applies
     once we have enough history.)

⚠️ COPYABILITY NOTE: most Solana SPOT/memecoin activity is snipe/MEV/insider = UNCOPYABLE (the
copy-alpha ceiling, harder than on HL). The copyable analog is PERP directional traders (Drift etc.).
This scaffold profiles the universal swap primitive; steer DISCOVERY toward perps for copy targets.

Credit-safe: uses ONLY free Helius/Pyth by default. Nansen discovery is opt-in (NANSEN_DISCOVERY=1).
"""
import os, sys, json, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from config import settings

HELIUS=os.getenv("HELIUS_API_KEY","")
NANSEN=os.getenv("NANSEN_API_KEY","")
RPC=f"https://mainnet.helius-rpc.com/?api-key={HELIUS}"
ENH="https://api.helius.xyz/v0"
OUT="/root/hl-bot/data/research_sol"; os.makedirs(OUT, exist_ok=True)

USDC="EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT="Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
WSOL="So11111111111111111111111111111111111111112"
STABLES={USDC,USDT}
MAJORS={WSOL,"SOL"}                       # Solana "main" = SOL + stables as quote
JUP_PROG="JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

def _sol_price():
    try:
        from tracker.prices import price
        return price("SOL") or 150.0
    except Exception:
        return 150.0
SOLPX=_sol_price()

# ── DISCOVERY ────────────────────────────────────────────────────────────────
def helius_recent_swappers(n=100):
    """FREE seed pool: feePayers of recent Jupiter swaps = active spot traders."""
    wallets=set(); before=None
    while len(wallets)<n:
        u=f"{ENH}/addresses/{JUP_PROG}/transactions?api-key={HELIUS}&limit=100"
        if before: u+=f"&before={before}"
        try: txs=requests.get(u,timeout=25).json()
        except Exception: break
        if not isinstance(txs,list) or not txs: break
        for t in txs:
            fp=t.get("feePayer")
            if fp: wallets.add(fp)
        before=txs[-1].get("signature")
        if not before: break
    return list(wallets)[:n]

def nansen_smart_money(pages=int(os.getenv("NANSEN_PAGES","8"))):
    """Nansen Smart-Money DEX traders on Solana → high-quality labeled wallet pool (~5cr/page).
    Returns {address: label}. Gated behind NANSEN_DISCOVERY=1 to avoid surprise credit spend."""
    if not (NANSEN and os.getenv("NANSEN_DISCOVERY")=="1"): return {}
    H={"apiKey":NANSEN,"Content-Type":"application/json"}; out={}
    for pg in range(1,pages+1):
        try:
            r=requests.post("https://api.nansen.ai/api/v1/smart-money/dex-trades",
                json={"chains":["solana"],"pagination":{"page":pg,"recordsPerPage":100}}, headers=H, timeout=30)
            recs=r.json().get("data") or [] if r.ok else []
        except Exception: break
        if not recs: break
        before=len(out)
        for d in recs:
            a=d.get("trader_address")
            if a: out[a]=d.get("trader_address_label","")
        if len(out)==before: break          # no new traders → stop (save credits)
    return out

# ── PROFILING (free, Helius parsed swaps) ──────────────────────────────────────
def helius_txns(addr, pages=5):
    out=[]; before=None
    for _ in range(pages):
        u=f"{ENH}/addresses/{addr}/transactions?api-key={HELIUS}&limit=100&type=SWAP"
        if before: u+=f"&before={before}"
        try: txs=requests.get(u,timeout=25).json()
        except Exception: break
        if not isinstance(txs,list) or not txs: break
        out+=txs; before=txs[-1].get("signature")
        if len(txs)<100: break
    return out

def _flows(tx, w):
    f=defaultdict(float)
    for tt in tx.get("tokenTransfers",[]) or []:
        amt=float(tt.get("tokenAmount",0) or 0); m=tt.get("mint")
        if tt.get("toUserAccount")==w: f[m]+=amt
        if tt.get("fromUserAccount")==w: f[m]-=amt
    for nt in tx.get("nativeTransfers",[]) or []:
        amt=float(nt.get("amount",0) or 0)/1e9
        if nt.get("toUserAccount")==w: f[WSOL]+=amt
        if nt.get("fromUserAccount")==w: f[WSOL]-=amt
    return f

def _quote_usd(mint, amt):
    if mint in STABLES: return abs(amt)
    if mint==WSOL: return abs(amt)*SOLPX
    return None

def profile(addr):
    """Reconstruct buy/sell events per token from swaps; price in the quote leg (USD)."""
    txs=helius_txns(addr)
    swaps=[t for t in txs if t.get("type")=="SWAP" and not t.get("transactionError")]
    if len(swaps)<10: return None
    swaps.sort(key=lambda t:t.get("timestamp",0))
    ev=defaultdict(lambda: dict(opens=0,adds=0,trims=0,full=0)); pos=defaultdict(float); cost=defaultdict(float)
    closes=[]; hold_open={}; holds=[]; tok_count=defaultdict(int)
    for t in swaps:
        f=_flows(t, addr); ts=t.get("timestamp",0)
        # quote leg = stable/SOL with biggest |usd|; token leg = the other significant mint
        quote=None; qusd=0.0
        for m,a in f.items():
            u=_quote_usd(m,a)
            if u is not None and u>qusd and abs(a)>1e-9: quote=m; qusd=u
        tok=None; tamt=0.0
        for m,a in f.items():
            if m==quote: continue
            if abs(a)>abs(tamt): tok=m; tamt=a
        if not tok or qusd<1: continue        # ignore dust / non-priceable
        usd=qusd
        if tamt>0:                              # received token = BUY (quote went out)
            e=ev[tok]
            if pos[tok]<=1e-9: e["opens"]+=1; tok_count[tok]+=1; hold_open[tok]=ts
            else: e["adds"]+=1
            pos[tok]+=tamt; cost[tok]+=usd
        elif tamt<0 and pos[tok]>1e-9:          # sent token = SELL
            sold=min(-tamt,pos[tok]); frac=sold/pos[tok]
            basis=cost[tok]*frac; proceeds=usd*(sold/max(-tamt,1e-9))
            closes.append(proceeds-basis)
            pos[tok]-=sold; cost[tok]-=basis
            if pos[tok]<=1e-9:
                ev[tok]["full"]+=1
                if tok in hold_open: holds.append((ts-hold_open.pop(tok))/3600)
            else: ev[tok]["trims"]+=1
    span=(swaps[-1]["timestamp"]-swaps[0]["timestamp"])/86400 or 1
    opens=sum(e["opens"] for e in ev.values()); adds=sum(e["adds"] for e in ev.values())
    full=sum(e["full"] for e in ev.values()); trims=sum(e["trims"] for e in ev.values())
    if opens<3 or len(closes)<5 or span<10: return None
    rt=(full)/max(opens,1)
    cad=opens/span
    if cad>10: style="scalp"
    elif rt>=0.5 and full>=4: style="round_trip"
    elif adds>=opens*3 and full<=opens: style="hold_scale"
    else: style="mixed"
    realized=sum(closes); wins=[c for c in closes if c>0]; losses=[c for c in closes if c<0]
    aw=sum(wins)/len(wins) if wins else 0; al=abs(sum(losses)/len(losses)) if losses else 0
    holds.sort(); med=holds[len(holds)//2] if holds else 0
    return dict(addr=addr, swaps=len(swaps), span=round(span), opens=opens, adds=adds, trims=trims,
        full=full, style=style, rt=round(rt,2), cadence=round(cad,2),
        realized_usd=round(realized), wr=round(len(wins)/max(len(wins)+len(losses),1)*100),
        payoff=round(aw/al,2) if al else 0, med_hold_h=round(med,1),
        n_tokens=len(tok_count), top_tokens=sorted(tok_count.items(),key=lambda x:-x[1])[:5])

def score(r):
    if not r or r["realized_usd"]<=0: return 0
    sm={"round_trip":1.0,"mixed":0.55,"hold_scale":0.2,"scalp":0.15}.get(r["style"],0.5)
    pay=min(r["payoff"],4)/4
    return round(sm*(0.4+0.6*pay)*100,1)

RES=f"{OUT}/results.jsonl"; DONE=f"{OUT}/done.json"; SHORT=f"{OUT}/sol_shortlist.md"

def write_shortlist():
    rows=[json.loads(l) for l in open(RES)] if os.path.exists(RES) else []
    best={}                                              # upsert by addr (keep latest)
    for r in rows: best[r["addr"]]=r
    rows=sorted(best.values(), key=lambda r:-r.get("score",0))
    copy=[r for r in rows if r.get("style")=="round_trip" and r.get("realized_usd",0)>0 and r.get("med_hold_h",0)>=2]
    with open(SHORT,"w") as f:
        f.write(f"# Solana spot smart-money shortlist — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC} | {len(rows)} profiled | {len(copy)} copyable(round_trip, hold≥2h)\n\n")
        f.write("| score | addr | nansen | style | WR | payoff | realized$ | hold_h | tokens |\n|--|--|--|--|--|--|--|--|--|\n")
        for r in (copy or rows)[:40]:
            f.write(f"| {r.get('score',0)} | `{r['addr'][:8]}…` | {r.get('nansen_label','')[:20]} | {r.get('style')} | {r['wr']}% | {r['payoff']} | ${r['realized_usd']:,} | {r['med_hold_h']} | {r['n_tokens']} |\n")

def run_once():
    seed=nansen_smart_money()
    pool=list(seed.keys()) or helius_recent_swappers(int(os.getenv("SOL_POOL","60")))
    done=set(json.load(open(DONE))) if os.path.exists(DONE) else set()
    new=[a for a in pool if a not in done]
    print(f"[solana] pool {len(pool)} ({'Nansen' if seed else 'Helius'}), {len(new)} new to profile")
    kept=0
    for i,a in enumerate(new):
        try:
            pr=profile(a)
            if pr:
                pr["nansen_label"]=seed.get(a,""); pr["score"]=score(pr); kept+=1
                open(RES,"a").write(json.dumps(pr)+"\n")
        except Exception as e: print(f"  skip {a[:8]}: {e}")
        done.add(a)
        if (i+1)%20==0: json.dump(list(done),open(DONE,"w")); write_shortlist()
    json.dump(list(done),open(DONE,"w")); write_shortlist()
    print(f"[solana] run done: +{kept} profiled, {len(done)} total seen")

def main():
    print(f"[solana] SOL ${SOLPX:.0f} | nansen-seed={'ON' if os.getenv('NANSEN_DISCOVERY')=='1' else 'OFF'}")
    if os.getenv("SOL_LOOP")=="1":
        iv=int(os.getenv("SOL_INTERVAL","10800"))        # accumulate every ~3h
        while True:
            try: run_once()
            except Exception as e: print("run err:", e)
            time.sleep(iv)
    else:
        run_once()

if __name__=="__main__": main()
