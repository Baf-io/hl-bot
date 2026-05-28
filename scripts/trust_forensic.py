#!/usr/bin/env python3
"""
TRUST FORENSIC — consistency-first trader vetting. NOT ROI/size. Answers: is this a clean,
consistent, active trader running TRUSTWORTHY automation we'd want to mirror?

Reads the authoritative on-chain record (every HL fill carries its L1 tx `hash`), FULL history,
and grades on four pillars + hard gates:

  CONSISTENCY  pnl-concentration (one-trade-wonder?), maxDD/realized + DD duration, daily Sharpe,
               positive-day ratio, WR-stability across history thirds, worst-day/realized.
  ACTIVITY     active-days/span, days-since-last, largest dormant gap.
  AUTOMATION   24/7 hour-coverage + overnight/weekend fills (bot vs human), inter-fill regularity,
               maker/taker mix, round-size — i.e. IS it a bot.
  TRUST        the martingale test: does it AVERAGE DOWN into losing positions (untrustworthy) or
               cut losers? + worst-loss discipline. This is the sharpest filter.
  BACKGROUND   Etherscan EVM funding trail (CEX/fund), Nansen labels, HL vault status (skin-in-game).

Hard gates (zero the score): blowup day, DD>40% of realized, >30% profit from one trade,
heavy averaging-down. ROI and size NEVER enter the ranking.

Usage: DOSSIER_ADDRS="0x..,0x.." python scripts/trust_forensic.py   (deep report on those)
       (no addrs → ranks the existing intraday/HFT pools consistency-first)
Output: data/research2/TRUST_FORENSIC.md
"""
import sys, os, json, time, math, statistics as st, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests
try:
    from config import settings
    ETHERSCAN=getattr(settings,"ETHERSCAN_API_KEY",os.getenv("ETHERSCAN_API_KEY",""))
    NANSEN=getattr(settings,"NANSEN_API_KEY",os.getenv("NANSEN_API_KEY",""))
except Exception:
    ETHERSCAN=os.getenv("ETHERSCAN_API_KEY",""); NANSEN=os.getenv("NANSEN_API_KEY","")

API="https://api.hyperliquid.xyz/info"; OUT="/root/hl-bot/data/research2/TRUST_FORENSIC.md"
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","kPEPE"}
_last=[0.0]
def post(t,**k):
    for _ in range(5):
        try:
            d=time.time()-_last[0]
            if d<0.35: time.sleep(0.35-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=25).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: time.sleep(1.2)
    return None

def full_fills(addr, days=400, pages=30):
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

def forensic(addr):
    fl=full_fills(addr)
    if len(fl)<30: return None
    now=time.time()*1000
    span=(fl[-1]["time"]-fl[0]["time"])/86400000 or 1
    # ── closed-segment reconstruction + averaging-down (martingale) test ──
    run=defaultdict(float); avg=defaultdict(float); seg=defaultdict(float)
    EPS=1e-9; closes=[]; adds=0; adds_down=0
    hours=defaultdict(int); weekend=0; overnight=0; gaps=[]; prev_t=None
    crossed=0; maker=0; daily=defaultdict(float)
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); px=float(f["px"]); pnl=float(f.get("closedPnl",0))
        side=f.get("side"); d=sz if side=="B" else -sz
        t=f["time"]; D=dt.datetime.fromtimestamp(t/1000,dt.UTC)
        hours[D.hour]+=1
        if D.weekday()>=5: weekend+=1
        if 0<=D.hour<6: overnight+=1
        if prev_t is not None: gaps.append((t-prev_t)/1000)
        prev_t=t
        if f.get("crossed") is True: crossed+=1
        elif f.get("crossed") is False: maker+=1
        daily[D.strftime("%Y-%m-%d")]+=pnl
        prev=run[c]; new=round(prev+d,8); seg[c]+=pnl
        if abs(new)>abs(prev) and abs(prev)>EPS and (prev>0)==(new>0):   # add same direction
            adds+=1
            if (prev>0 and px<avg[c]) or (prev<0 and px>avg[c]): adds_down+=1   # adding into a loser
        if abs(new)>abs(prev):                                          # update weighted avg entry
            avg[c]=(avg[c]*abs(prev)+px*sz)/abs(new) if abs(new)>0 else px
        if abs(prev)>=EPS and (abs(new)<EPS or (prev>0)!=(new>0)):      # closed / flipped
            closes.append(round(seg[c])); seg[c]=0.0
        if abs(new)<EPS: avg[c]=0.0
        run[c]=new
    if len(closes)<8: return None
    realized=sum(closes); tot=realized if realized>0 else 1
    wins=[x for x in closes if x>0]; losses=[x for x in closes if x<0]
    # consistency
    biggest=max(closes,key=abs) if closes else 0
    concentration=abs(biggest)/max(abs(realized),1)
    cum=peak=mdd=0.0; underwater=0; max_uw=0
    for x in closes:
        cum+=x; peak=max(peak,cum)
        if cum<peak: underwater+=1; max_uw=max(max_uw,underwater)
        else: underwater=0
        mdd=min(mdd,cum-peak)
    dvals=list(daily.values())
    sharpe=(st.mean(dvals)/st.pstdev(dvals)) if len(dvals)>1 and st.pstdev(dvals)>0 else 0
    pos_day=sum(1 for v in dvals if v>0)/max(len(dvals),1)
    worst_day=min(dvals) if dvals else 0
    # WR stability across thirds
    th=len(closes)//3 or 1
    wr_thirds=[round(sum(1 for x in g if x>0)/max(len(g),1)*100) for g in (closes[:th],closes[th:2*th],closes[2*th:]) if g]
    # activity
    active_days=len(daily); recency_d=(now-fl[-1]["time"])/86400000
    max_gap_d=max(gaps)/86400 if gaps else 0
    # automation
    hour_cov=len(hours)/24; n=len(fl)
    intervals_cv=(st.pstdev(gaps)/st.mean(gaps)) if len(gaps)>1 and st.mean(gaps)>0 else 0
    taker_frac=crossed/max(crossed+maker,1)
    # current positions
    cs=post("clearinghouseState",user=addr) or {}; mids=post("allMids") or {}
    netusd=0.0; pos=[]
    for ap in cs.get("assetPositions",[]):
        p=ap["position"]; szi=float(p["szi"]); c=p["coin"]; lev=p["leverage"].get("value")
        mid=float(mids.get(c,0) or p.get("entryPx") or 0); val=szi*mid; netusd+=val
        pos.append(f"{c} {'L' if szi>0 else 'S'} ${abs(val):,.0f}@{lev}x uPnL${float(p['unrealizedPnl']):,.0f}")
    # ── knife-trap detector: small wins, big losses = he catches falling knives ──
    # Payoff < 0.5 with a high WR is the classic loss-hider / knife-catcher signature
    # (closes winners small to keep WR, holds losers until they slice through stops).
    avg_win  = (sum(wins)/len(wins)) if wins else 0
    avg_loss = (sum(abs(x) for x in losses)/len(losses)) if losses else 0
    payoff   = avg_win/avg_loss if avg_loss > 0 else (10.0 if avg_win > 0 else 0)
    # Open uPnL drag: how much paper-loss he's currently hiding relative to realized
    open_upnl = sum(float(ap["position"].get("unrealizedPnl",0)) for ap in cs.get("assetPositions",[]))
    paper_drag = abs(min(open_upnl, 0)) / max(abs(realized), 1) if realized > 0 else 0
    return dict(addr=addr, fills=len(fl), span=round(span,1), active_days=active_days,
        recency_d=round(recency_d,1), max_gap_d=round(max_gap_d,1), realized=round(realized),
        n_closed=len(closes), wr=round(len(wins)/max(len(wins)+len(losses),1)*100),
        concentration=round(concentration,2), maxdd=round(mdd), maxdd_ratio=round(abs(mdd)/tot,2),
        dd_duration=max_uw, sharpe=round(sharpe,2), pos_day_ratio=round(pos_day,2),
        worst_day=round(worst_day), worst_day_ratio=round(abs(worst_day)/tot,2), wr_thirds=wr_thirds,
        adds=adds, adds_down=adds_down, avg_down_ratio=round(adds_down/max(adds,1),2),
        hour_cov=round(hour_cov,2), overnight_frac=round(overnight/n,2), weekend_frac=round(weekend/n,2),
        interval_cv=round(intervals_cv,2), taker_frac=round(taker_frac,2),
        avg_win=round(avg_win), avg_loss=round(avg_loss), payoff=round(payoff,2),
        open_upnl=round(open_upnl), paper_drag=round(paper_drag,2),
        net_usd_now=round(netusd), positions=pos[:6], biggest_trip=biggest)

def is_bot(p):
    # 24/7 coverage + overnight activity + many fills = automation
    if p["hour_cov"]>=0.8 and p["overnight_frac"]>=0.15 and p["fills"]>=200: return "BOT (24/7)"
    if p["hour_cov"]>=0.6 and p["overnight_frac"]>=0.1: return "likely-automated"
    if p["overnight_frac"]<0.05 and p["hour_cov"]<0.6: return "discretionary (human hours)"
    return "mixed"

def gates(p):
    fails=[]
    if p["concentration"]>0.30: fails.append(f"one-trade-dependent ({int(p['concentration']*100)}% from 1 trip)")
    if p["maxdd_ratio"]>0.40: fails.append(f"deep drawdown ({int(p['maxdd_ratio']*100)}% of realized)")
    if p["worst_day_ratio"]>0.40: fails.append(f"blowup day (-${abs(p['worst_day']):,})")
    if p["avg_down_ratio"]>0.30 and p["adds"]>=5: fails.append(f"MARTINGALE — averages into losers ({int(p['avg_down_ratio']*100)}% of adds)")
    if p["active_days"]/max(p["span"],1)<0.15: fails.append(f"sporadic ({p['active_days']} active days in {int(p['span'])})")
    if p["recency_d"]>14: fails.append(f"stale (last trade {int(p['recency_d'])}d ago)")
    # KNIFE-TRAP: small wins + big losses = catches falling knives. Combined w/ high WR = loss-hider.
    if p["payoff"]<0.40 and p["n_closed"]>=10:
        fails.append(f"KNIFE-TRAP — small wins/big losses (payoff {p['payoff']}, avg win ${p['avg_win']} vs avg loss ${p['avg_loss']})")
    # PAPER-DRAG: he's sitting on open losses ≥ his realized PnL (hiding losers in unrealized)
    if p["paper_drag"]>1.0:
        fails.append(f"PAPER-DRAG — open uPnL ${p['open_upnl']:,} hides losses (drag {int(p['paper_drag']*100)}% of realized)")
    return fails

def cscore(p):
    # consistency-first composite (no ROI, no size). 0..100.
    if p["realized"]<=0: return 0
    c_conc=max(0,1-p["concentration"]/0.5)
    c_dd  =max(0,1-p["maxdd_ratio"]/0.6)
    c_shp =min(max(p["sharpe"],0)/0.5,1)
    c_pos =p["pos_day_ratio"]
    c_act =min(p["active_days"]/max(p["span"],1)/0.5,1)
    consistency=(0.28*c_conc+0.24*c_dd+0.18*c_shp+0.15*c_pos+0.15*c_act)
    trust=1.0-min(p["avg_down_ratio"],1.0)*0.7          # martingale crushes trust
    fresh=1.0 if p["recency_d"]<=7 else (0.6 if p["recency_d"]<=14 else 0.2)
    return round(consistency*trust*fresh*100,1)

def etherscan_origin(addr):
    if not ETHERSCAN: return "n/a"
    for chain,name in ((42161,"arb"),(1,"eth")):
        try:
            r=requests.get("https://api.etherscan.io/v2/api",params={"chainid":chain,"module":"account",
                "action":"txlist","address":addr,"startblock":0,"endblock":99999999,"page":1,"offset":3,
                "sort":"asc","apikey":ETHERSCAN},timeout=20).json()
            res=r.get("result")
            if isinstance(res,list) and res:
                frm=res[0].get("from","")
                return f"{name}: first-funder {frm[:10]}… ({len(res)}+ early txs)"
        except Exception: pass
    return "no EVM trail (HL-native)"

def hl_vault(addr):
    try:
        v=post("vaultDetails",vaultAddress=addr)
        if isinstance(v,dict) and v.get("name"): return f"VAULT '{v.get('name')}' (public, depositors)"
    except Exception: pass
    return "not a vault"

def report(addr):
    p=forensic(addr)
    if not p: return f"\n## `{addr}`\n_insufficient history_\n", 0
    fails=gates(p); score=0 if fails else cscore(p)
    bot=is_bot(p); ev=etherscan_origin(addr); vault=hl_vault(addr)
    verdict = ("❌ REJECT — "+"; ".join(fails)) if fails else f"✅ CLEAN — consistency score {score}"
    L=[f"\n## `{addr}`",
       f"**{verdict}**",
       f"- **Automation**: {bot} — 24/7 hour-coverage {int(p['hour_cov']*100)}%, overnight {int(p['overnight_frac']*100)}%, weekend {int(p['weekend_frac']*100)}%, interval-CV {p['interval_cv']}, taker {int(p['taker_frac']*100)}%",
       f"- **TRUST (martingale test)**: averages-into-losers **{int(p['avg_down_ratio']*100)}% of {p['adds']} adds** {'⚠️ UNTRUSTWORTHY' if p['avg_down_ratio']>0.3 and p['adds']>=5 else '✅ disciplined'}",
       f"- **Knife-trap test**: payoff {p['payoff']} (avg win ${p['avg_win']:,} / avg loss ${p['avg_loss']:,}) {'⚠️ CATCHES KNIVES' if p['payoff']<0.4 and p['n_closed']>=10 else '✅ asymmetric'} · open uPnL ${p['open_upnl']:,} (paper-drag {int(p['paper_drag']*100)}%)",
       f"- **Consistency**: concentration {int(p['concentration']*100)}% (biggest trip ${p['biggest_trip']:,}) · maxDD ${p['maxdd']:,} ({int(p['maxdd_ratio']*100)}% of realized, {p['dd_duration']} trips underwater) · daily-Sharpe {p['sharpe']} · positive-days {int(p['pos_day_ratio']*100)}% · WR-by-third {p['wr_thirds']}",
       f"- **Activity**: {p['active_days']} active days / {p['span']}d span · last trade {p['recency_d']}d ago · max gap {p['max_gap_d']}d · {p['n_closed']} closed trips · WR {p['wr']}%",
       f"- **Worst day**: ${p['worst_day']:,} ({int(p['worst_day_ratio']*100)}% of realized) · realized ${p['realized']:,}",
       f"- **Now**: net ${p['net_usd_now']:,} — {', '.join(p['positions']) or 'flat'}",
       f"- **Background**: {ev} · {vault}"]
    return "\n".join(L), score

def main():
    addrs=[a.strip() for a in os.getenv("DOSSIER_ADDRS","").split(",") if a.strip()]
    if not addrs:
        pool={}
        for fn in ("hft_results.jsonl","results.jsonl"):
            p=f"/root/hl-bot/data/research2/{fn}"
            if os.path.exists(p):
                for l in open(p):
                    r=json.loads(l)
                    if r.get("realized",0)>0: pool[r["addr"]]=r
        cand=[a for a,r in pool.items() if r.get("med_h",99)<=12 or r.get("cadence",0)>=2]
        addrs=cand[:int(os.getenv("TRUST_MAX","40"))]
    out=[f"# Trust forensic — consistency-first — {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M UTC}\n"]
    scored=[]
    for a in addrs:
        try:
            txt,sc=report(a); scored.append((sc,a,txt))
            print(f"  {a[:12]} score={sc}", flush=True)
        except Exception as e: print(f"  skip {a[:12]}: {e}", flush=True)
    scored.sort(key=lambda x:-x[0])
    out.append(f"\n**Ranked (consistency-first, clean-gated):** " + " · ".join(f"`{a[:8]}`={s}" for s,a,_ in scored) + "\n")
    for s,a,txt in scored: out.append(txt)
    open(OUT,"w").write("\n".join(out)+"\n")
    print("\n".join(out)); print(f"\n[trust] → {OUT}")

if __name__=="__main__": main()
