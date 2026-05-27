#!/usr/bin/env python3
"""
COPY-LAG BACKTEST — "what would WE have made copying him, entering 1 minute late?"

Models our DIRECTION-only sleeve copying a source with an aggressive fixed copy lag (default 60s):
we detect his fresh OPEN `LAG` seconds late and enter at the market price THEN; we detect his
CLOSE/FLIP `LAG` seconds late and exit at the market price THEN. One entry per episode (we mirror
direction, not his adds) — exactly how the live sleeve behaves.

For each of his last N days of closed episodes it compares, per trade and cumulatively:
  HIS  directional capture: his first-entry price → his close price
  OURS lagged capture:      market price LAG after his open → market price LAG after his close
The gap is the pure cost of copy-lag. Win-rate preservation shows if lag flips winners to losers.
Returns are UNLEVERED price-move % (both scale identically by leverage), equal-weighted per trade.

Prices LAG-after each event come from 1m candleSnapshot (open of the next minute, nearest within 5m).
Usage: LAG_ADDR=0x.. LAG_DAYS=30 LAG_SECONDS=60 python scripts/lag_backtest.py
FREE — HL public API, read-only.
"""
import sys, os, time, datetime as dt
from collections import defaultdict
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import requests

API="https://api.hyperliquid.xyz/info"
ADDR=os.getenv("LAG_ADDR","").lower()
DAYS=int(os.getenv("LAG_DAYS","30"))
LAG=int(os.getenv("LAG_SECONDS","60"))
LIQUID={"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC","ARB","OP","TON","WLD","kPEPE","NEAR"}
_last=[0.0]
def post(t,**k):
    for _ in range(6):
        try:
            d=time.time()-_last[0]
            if d<0.4: time.sleep(0.4-d)
            _last[0]=time.time()
            j=requests.post(API,json={"type":t,**k},timeout=30).json()
            if j is not None and not (isinstance(j,dict) and j.get("error")): return j
        except Exception: time.sleep(1.2)
    return None

def fills(addr):
    out=[]; end=int(time.time()*1000); cur=end-(DAYS+2)*86400*1000
    for _ in range(20):
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

def episodes(fl, cutoff_ms):
    """Reconstruct closed episodes; keep those CLOSED within the window."""
    run=defaultdict(float); first_px=defaultdict(float); first_t=defaultdict(float); EPS=1e-9; eps=[]
    for f in fl:
        c=f["coin"]; sz=float(f["sz"]); px=float(f["px"]); d=sz if f.get("side")=="B" else -sz; t=f["time"]
        prev=run[c]; new=round(prev+d,8)
        if abs(prev)<EPS and abs(new)>=EPS:
            first_px[c]=px; first_t[c]=t
        if abs(prev)>=EPS and (abs(new)<EPS or (prev>0)!=(new>0)):
            if f["time"]>=cutoff_ms:
                eps.append(dict(coin=c, dir=1 if prev>0 else -1, t_open=first_t[c], px_open=first_px[c],
                                t_close=t, px_close=px))
            if (prev>0)!=(new>0) and abs(new)>=EPS:                 # flip opens a new episode
                first_px[c]=px; first_t[c]=t
        run[c]=new
    return eps

def candle_index(coin, start_ms, end_ms):
    """Hybrid oracle: 1m candles (only ~3.5d of history → exact 1-min lag for recent trades) +
    15m candles (full 30d → ~15-min-resolution fallback for older trades, conservative)."""
    idx1={}; idx15={}
    # 15m over the full span (chunks of 5000 bars = ~52d, so one pass covers it)
    cur=start_ms
    while cur<end_ms:
        j=post("candleSnapshot", req={"coin":coin,"interval":"15m","startTime":cur,"endTime":end_ms})
        if not isinstance(j,list) or not j: break
        for cd in j: idx15[cd["t"]//900000]=float(cd["c"])      # close of the 15m bar
        if len(j)<2: break
        cur=max(cd["t"] for cd in j)+900000
    # 1m for the recent window (API only serves ~3.5d back)
    j=post("candleSnapshot", req={"coin":coin,"interval":"1m","startTime":max(start_ms,end_ms-3*86400*1000),"endTime":end_ms})
    if isinstance(j,list):
        for cd in j: idx1[cd["t"]//60000]=float(cd["o"])        # open of the minute (price ~at that minute)
    return idx1, idx15

def price_at(idxs, t_ms):
    idx1, idx15 = idxs
    m=t_ms//60000
    for off in range(0,6):                                          # exact: 1m candle within +5m
        if m+off in idx1: return idx1[m+off], "1m"
    b=t_ms//900000
    for off in range(0,3):                                          # fallback: 15m bar at/after event
        if b+off in idx15: return idx15[b+off], "15m"
    for off in range(1,3):
        if b-off in idx15: return idx15[b-off], "15m"
    return None, None

def main():
    fl=fills(ADDR)
    cutoff=int((time.time()-DAYS*86400)*1000)
    eps=episodes(fl, cutoff)
    print(f"\n===== COPY-LAG BACKTEST  {ADDR[:12]}…  last {DAYS}d · lag {LAG}s =====")
    print(f"{len(eps)} closed episodes in window\n")
    if not eps: print("no episodes"); return
    coins=sorted({e["coin"] for e in eps})
    span_lo=min(e["t_open"] for e in eps)-120000; span_hi=max(e["t_close"] for e in eps)+360000
    print(f"fetching 1m candles for {len(coins)} coins…", flush=True)
    cidx={c:candle_index(c, span_lo, span_hi) for c in coins}

    rows=[]; his_cum=our_cum=0.0; his_w=our_w=0; missing=0; liq_his=liq_our=0.0; liq_n=0; res_n=defaultdict(int)
    for e in eps:
        oe,r1=price_at(cidx[e["coin"]], e["t_open"]+LAG*1000)
        xe,r2=price_at(cidx[e["coin"]], e["t_close"]+LAG*1000)
        if not oe or not xe or e["px_open"]<=0:
            missing+=1; continue
        his=e["dir"]*(e["px_close"]-e["px_open"])/e["px_open"]*100
        our=e["dir"]*(xe-oe)/oe*100
        his_cum+=his; our_cum+=our
        his_w+= his>0; our_w+= our>0
        res_n[r1]+=1; res_n[r2]+=1
        if e["coin"] in LIQUID: liq_his+=his; liq_our+=our; liq_n+=1
        rows.append((e,his,our,oe,xe))
    n=len(rows)
    print(f"-- per-trade (newest 22), return = unlevered directional price move --")
    print(f"  {'closed':16} {'coin':7} {'dir':3} {'his%':>7} {'our%':>7} {'lag drag':>9}")
    for e,his,our,oe,xe in rows[-22:][::-1]:
        d="L" if e["dir"]>0 else "S"
        print(f"  {dt.datetime.fromtimestamp(e['t_close']/1000,dt.UTC):%m-%d %H:%M}  {e['coin']:7} {d:3} "
              f"{his:>+7.2f} {our:>+7.2f} {our-his:>+9.2f}")
    print(f"\n===== RESULT ({n} copyable trades, {missing} skipped no-candle) =====")
    print(f"  price-oracle resolution: 1m (exact lag) on {res_n.get('1m',0)} event-prices, 15m (≈approx, conservative) on {res_n.get('15m',0)}")
    print(f"  HIS  cumulative directional return : {his_cum:>+8.1f}%   (avg {his_cum/n:+.2f}%/trade, WR {his_w}/{n}={his_w/n*100:.0f}%)")
    print(f"  OURS lagged ({LAG}s) cumulative     : {our_cum:>+8.1f}%   (avg {our_cum/n:+.2f}%/trade, WR {our_w}/{n}={our_w/n*100:.0f}%)")
    print(f"  COPY-LAG DRAG                       : {our_cum-his_cum:>+8.1f}%   ({(our_cum-his_cum)/n:+.2f}%/trade)")
    print(f"  capture ratio (ours/his)            : {our_cum/his_cum*100 if his_cum else 0:>7.0f}%")
    if liq_n:
        print(f"\n  -- LIQUID-only subset ({liq_n} trades on majors we'd actually mirror cleanly) --")
        print(f"  HIS {liq_his:+.1f}%  ·  OURS {liq_our:+.1f}%  ·  drag {liq_our-liq_his:+.1f}%  ·  capture {liq_our/liq_his*100 if liq_his else 0:.0f}%")

if __name__=="__main__": main()
