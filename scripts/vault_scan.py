#!/usr/bin/env python3
"""
VAULT SCAN — find TRUSTWORTHY AUTOMATION among HL vaults (public strategies w/ depositor capital).

A vault is automation you can actually trust to be automation: it runs a published strategy, the
leader has skin-in-the-game, depositors vote with capital, and the on-chain fill record IS the
strategy. This sweeps the full vault leaderboard and applies the SAME consistency-first screen as
trust_forensic (no ROI/size ranking; martingale + drawdown + concentration gates).

Two stages so we don't hammer the shared API:
  1. CHEAP pre-screen from the leaderboard payload itself (tvl / age / apr / the published `allTime`
     PnL curve → curve-drawdown + monotonicity). Cuts 9k vaults → a few dozen plausible ones.
  2. DEEP forensic (reuses trust_forensic.forensic/gates/cscore) on the survivors' vaultAddress:
     full-history fills, averaging-down test, daily Sharpe, positive-day ratio, recency.

A vault that PASSES is exactly what the mandate asked for: consistent, active, trustworthy automation.

Env: VAULT_MIN_TVL (50000), VAULT_MIN_AGE_D (45), VAULT_TOP (35 deep-screened), VAULT_MIN_CURVE (0.0)
FREE — HL public API, read-only. Output: data/research2/VAULT_SCAN.md
"""
import sys, os, json, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/scripts"); sys.path.insert(0, "/root/hl-bot/src")
import requests
from trust_forensic import forensic, gates, cscore, is_bot, etherscan_origin

LEADER="https://stats-data.hyperliquid.xyz/Mainnet/vaults"
OUT="/root/hl-bot/data/research2/VAULT_SCAN.md"
MIN_TVL=float(os.getenv("VAULT_MIN_TVL","50000"))
MIN_AGE_D=float(os.getenv("VAULT_MIN_AGE_D","45"))
TOP=int(os.getenv("VAULT_TOP","35"))
MIN_CURVE=float(os.getenv("VAULT_MIN_CURVE","0.0"))

def curve_metrics(pnls):
    """Cheap consistency read from the published cumulative-PnL curve (no API calls)."""
    series={p:[float(x) for x in v] for p,v in pnls}
    cum=series.get("allTime") or []
    if len(cum)<4: return None
    final=cum[-1]
    peak=mdd=0.0
    for v in cum:
        peak=max(peak,v); mdd=min(mdd,v-peak)
    # rising-segment fraction (how monotone the equity climb is) + recent slope
    rises=sum(1 for a,b in zip(cum,cum[1:]) if b>a); steps=len(cum)-1
    monotone=rises/max(steps,1)
    recent=cum[-1]-cum[max(0,len(cum)-3)]
    dd_ratio=abs(mdd)/max(abs(final),1)
    # curve score: profitable + shallow drawdown + monotone climb + not currently bleeding
    s=0.0
    if final>0:
        s=(1-min(dd_ratio,1))*0.5 + monotone*0.35 + (0.15 if recent>=0 else 0.0)
    return dict(final=round(final), dd_ratio=round(dd_ratio,2), monotone=round(monotone,2),
                recent_up=recent>=0, curve_score=round(s,3))

def discover():
    rows=requests.get(LEADER,timeout=90).json()
    now=time.time()*1000; pool=[]
    for x in rows:
        s=x.get("summary",{})
        try:
            tvl=float(s.get("tvl",0)); age=(now-s["createTimeMillis"])/86400000
        except Exception: continue
        if s.get("isClosed"): continue
        if tvl<MIN_TVL or age<MIN_AGE_D: continue
        cm=curve_metrics(x.get("pnls",[]))
        if not cm or cm["final"]<=0 or cm["curve_score"]<MIN_CURVE: continue
        pool.append(dict(name=s.get("name",""), addr=s["vaultAddress"], leader=s.get("leader",""),
                         tvl=round(tvl), age=round(age), apr=round(float(x.get("apr",0)),3), **cm))
    pool.sort(key=lambda r:-r["curve_score"])
    return pool

def main():
    pool=discover()
    print(f"[vault] {len(pool)} open vaults pass cheap pre-screen (tvl>{MIN_TVL:.0f}, age>{MIN_AGE_D:.0f}d, profitable curve)", flush=True)
    cands=pool[:TOP]
    out=[f"# Vault scan — trustworthy-automation, consistency-first — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC}\n",
         f"_{len(pool)} vaults cleared the cheap curve pre-screen; deep-forensic on top {len(cands)}. "
         f"Same gates as trust_forensic (martingale / drawdown>40% / concentration>30% / sporadic / stale)._\n"]
    scored=[]
    for v in cands:
        try:
            p=forensic(v["addr"])
            if not p:
                scored.append((-1,v,None,["insufficient fill history"])); print(f"  {v['name'][:24]:24} no-fills",flush=True); continue
            fails=gates(p); sc=0 if fails else cscore(p)
            scored.append((sc,v,p,fails)); print(f"  {v['name'][:24]:24} score={sc} {'REJECT' if fails else 'CLEAN'}",flush=True)
        except Exception as e:
            print(f"  skip {v['name'][:24]}: {e}",flush=True)
    scored.sort(key=lambda x:-x[0])
    clean=[r for r in scored if r[0]>0]
    out.append(f"\n**Pre-screen leaders (curve):** "+" · ".join(f"`{v['name'][:14]}`={v['curve_score']}" for v in cands[:12])+"\n")
    out.append(f"\n**Deep-forensic ranked (clean only):** "+(" · ".join(f"`{v['name'][:14]}`={s}" for s,v,p,f in clean) or "_none passed the gates_")+"\n")
    for s,v,p,fails in scored:
        head=f"\n## {v['name']}  `{v['addr'][:10]}…`\n*tvl ${v['tvl']:,} · age {v['age']}d · apr {int(v['apr']*100)}% · curve {v['curve_score']} (dd {int(v['dd_ratio']*100)}%, monotone {int(v['monotone']*100)}%)*"
        if not p:
            out.append(head+f"\n- ❌ {'; '.join(fails)}\n"); continue
        verdict=("❌ REJECT — "+"; ".join(fails)) if fails else f"✅ CLEAN — consistency score {s}"
        out.append(head+f"\n**{verdict}**\n"
            f"- **Automation**: {is_bot(p)} — hour-cov {int(p['hour_cov']*100)}%, overnight {int(p['overnight_frac']*100)}%, weekend {int(p['weekend_frac']*100)}%, interval-CV {p['interval_cv']}, taker {int(p['taker_frac']*100)}%\n"
            f"- **TRUST (martingale)**: averages-into-losers {int(p['avg_down_ratio']*100)}% of {p['adds']} adds {'⚠️ UNTRUSTWORTHY' if p['avg_down_ratio']>0.3 and p['adds']>=5 else '✅ disciplined'}\n"
            f"- **Consistency**: concentration {int(p['concentration']*100)}% (biggest ${p['biggest_trip']:,}) · maxDD ${p['maxdd']:,} ({int(p['maxdd_ratio']*100)}%, {p['dd_duration']} trips underwater) · Sharpe {p['sharpe']} · pos-days {int(p['pos_day_ratio']*100)}% · WR-thirds {p['wr_thirds']}\n"
            f"- **Activity**: {p['active_days']}/{p['span']}d active · last {p['recency_d']}d ago · {p['n_closed']} trips · WR {p['wr']}% · realized ${p['realized']:,}\n"
            f"- **Now**: net ${p['net_usd_now']:,} — {', '.join(p['positions']) or 'flat'}\n"
            f"- **Leader**: `{v['leader'][:10]}…` · {etherscan_origin(v['leader'])}")
    open(OUT,"w").write("\n".join(out)+"\n")
    print(f"\n[vault] {len(clean)} CLEAN of {len(cands)} deep-screened → {OUT}", flush=True)

if __name__=="__main__": main()
