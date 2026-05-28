#!/usr/bin/env python3
"""
REGIME SCAN — find algos that make money in THIS bearish tape, then trust-gate them.

Discovery: pull the HL leaderboard, keep accounts that are PROFITABLE through the down-move
(week PnL > 0 AND month PnL > 0 AND allTime > 0) in a real size band — i.e. they made money
while BTC fell = short-capable / two-sided, not up-only beta. Rank by recent (week) PnL.
Then run the SAME consistency-first trust gate (`trust_forensic`) on the top N — martingale /
drawdown / concentration / sporadic / stale — and report the clean ones + their current net
direction (are they actually short-positioned now?).

Env: REGIME_MIN_VAL (20000), REGIME_MAX_VAL (10000000), REGIME_TOP (35).
FREE — HL public API, read-only. Output: data/research2/REGIME_SCAN.md
"""
import sys, os, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/scripts"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from trust_forensic import forensic, gates, cscore, is_bot

LB="https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUT="/root/hl-bot/data/research2/REGIME_SCAN.md"
MIN_VAL=float(os.getenv("REGIME_MIN_VAL","20000"))
MAX_VAL=float(os.getenv("REGIME_MAX_VAL","10000000"))
TOP=int(os.getenv("REGIME_TOP","35"))

def discover():
    rows=requests.get(LB,timeout=90).json()["leaderboardRows"]
    pool=[]
    for r in rows:
        try:
            av=float(r["accountValue"]); w=dict(r["windowPerformances"])
            wk=float(w["week"]["pnl"]); mo=float(w["month"]["pnl"]); at=float(w["allTime"]["pnl"])
        except Exception: continue
        if not (MIN_VAL<=av<=MAX_VAL): continue
        if wk<=0 or mo<=0 or at<=0: continue          # green through the down-move
        pool.append(dict(addr=r["ethAddress"], av=round(av), week=round(wk), month=round(mo)))
    pool.sort(key=lambda x:-x["week"])                  # strongest recent first
    return pool

def main():
    pool=discover()
    print(f"[regime] {len(pool)} accounts green on BOTH week & month (made money in the downmove)", flush=True)
    cands=pool[:TOP]
    out=[f"# Regime scan — bearish-capable algos, trust-gated — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC}\n",
         f"_{len(pool)} accounts profitable through the May down-move; deep trust-gate on top {len(cands)} by recent PnL._\n"]
    scored=[]
    for v in cands:
        try:
            p=forensic(v["addr"])
            if not p: print(f"  {v['addr'][:12]} no-hist",flush=True); continue
            fails=gates(p); sc=0 if fails else cscore(p)
            scored.append((sc,v,p,fails)); print(f"  {v['addr'][:12]} wk${v['week']:,} score={sc} {'REJECT' if fails else 'CLEAN'}",flush=True)
        except Exception as e: print(f"  skip {v['addr'][:12]}: {e}",flush=True)
    scored.sort(key=lambda x:-x[0])
    clean=[r for r in scored if r[0]>0]
    out.append(f"\n**CLEAN & bearish-capable (ranked):** "+(" · ".join(f"`{v['addr'][:8]}`={s} (wk+${v['week']:,})" for s,v,p,f in clean) or "_none passed_")+"\n")
    for s,v,p,fails in scored:
        head=f"\n## `{v['addr']}`\n*acct ${v['av']:,} · week +${v['week']:,} · month +${v['month']:,}*"
        if fails: out.append(head+f"\n❌ REJECT — {'; '.join(fails)}\n"); continue
        net=p['net_usd_now']; dirn = 'SHORT-net' if net<0 else 'LONG-net' if net>0 else 'flat'
        out.append(head+f"\n**✅ CLEAN — score {s}**\n"
            f"- {is_bot(p)} · martingale {int(p['avg_down_ratio']*100)}% of {p['adds']} adds {'⚠️' if p['avg_down_ratio']>0.3 and p['adds']>=5 else '✅'} · conc {int(p['concentration']*100)}% · maxDD {int(p['maxdd_ratio']*100)}% · WR-thirds {p['wr_thirds']}\n"
            f"- {p['active_days']}/{p['span']}d active · {p['n_closed']} trips · WR {p['wr']}% · realized ${p['realized']:,}\n"
            f"- **NOW: net ${net:,} ({dirn})** — {', '.join(p['positions']) or 'flat'}")
    open(OUT,"w").write("\n".join(out)+"\n")
    print(f"\n[regime] {len(clean)} CLEAN of {len(cands)} → {OUT}", flush=True)

if __name__=="__main__": main()
