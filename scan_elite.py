"""
scan_elite.py — one-shot elite trader scanner
Run: python scan_elite.py
"""
import urllib.request, urllib.error, json, time, sys
from scan_common import hold_stats, fmt_hold_short

HL    = "https://api.hyperliquid.xyz/info"
STATS = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
H     = {"Content-Type": "application/json"}
now_ms   = time.time() * 1000
month_ms = 30 * 86_400_000
week_ms  =  7 * 86_400_000
day_ms   =      86_400_000

CURRENT = {
    "0x162cc7c861ebd0c06b3d72319201150482518185",
    "0xfc667adba8d4837586078f4fdcdc29804337ca06",
    "0x42b6d907f36255d48f70db8b4a2684088a162634",
    "0xf517639a8872e756ac98d3c65507d2ebc25cc032",
}
CURRENT_PNL = {
    "0x162cc7c861ebd0c06b3d72319201150482518185": 43_578_582,
    "0xfc667adba8d4837586078f4fdcdc29804337ca06": 19_295_029,
    "0x42b6d907f36255d48f70db8b4a2684088a162634": 17_160_668,
    "0xf517639a8872e756ac98d3c65507d2ebc25cc032": 26_791_043,
}

def get(url):
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def post(payload, retries=4):
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(HL, data=data, headers=H, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (attempt + 1))
            else:
                return None
        except Exception:
            return None
    return None

def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            w, s = item
            out[w] = {"pnl": float(s.get("pnl", 0)), "vlm": float(s.get("vlm", 0))}
    return out

def profile(addr):
    fills = post({"type": "userFills", "user": addr})
    if not fills or not isinstance(fills, list) or len(fills) < 10:
        return None
    fills = sorted(fills, key=lambda f: float(f.get("time", 0)))
    r30 = [f for f in fills if now_ms - float(f.get("time", 0)) < month_ms]
    r7  = [f for f in fills if now_ms - float(f.get("time", 0)) < week_ms]
    r1  = [f for f in fills if now_ms - float(f.get("time", 0)) < day_ms]
    if len(r30) < 10:
        return None

    closes = [f for f in fills if float(f.get("closedPnl", 0)) != 0]

    def wr(s):
        return round(sum(1 for f in s if float(f.get("closedPnl", 0)) > 0) / len(s), 3) if len(s) > 2 else 0.5

    # streak
    streak = 0
    for f in reversed(closes):
        p = float(f.get("closedPnl", 0))
        if p == 0:
            break
        if streak == 0:
            streak = 1 if p > 0 else -1
        elif streak > 0 and p > 0:
            streak += 1
        elif streak < 0 and p < 0:
            streak -= 1
        else:
            break

    # hold times — episode reconstruction over full history (incl. open positions)
    hs = hold_stats(fills, now_ms)

    # max concurrent
    oc = {}
    snaps = []
    for f in r30:
        coin = f.get("coin", "")
        d    = str(f.get("dir", ""))
        cp   = float(f.get("closedPnl", 0))
        if "Open" in d or (cp == 0 and "Close" not in d):
            oc[coin] = 1
        elif "Close" in d or cp != 0:
            oc.pop(coin, None)
        snaps.append(len(oc))

    # weekly pnl
    wk = {}
    for f in closes:
        wid = int(float(f.get("time", 0)) / week_ms)
        wk[wid] = wk.get(wid, 0) + float(f.get("closedPnl", 0))
    wks = sorted(wk.items())[-8:]
    pw  = sum(1 for _, p in wks if p > 0)

    # max loss streak
    mls = 0
    cl  = 0
    for f in closes:
        if float(f.get("closedPnl", 0)) < 0:
            cl += 1
            mls = max(mls, cl)
        else:
            cl = 0

    # per-coin WR
    coin_stats = {}
    for f in closes:
        c = f.get("coin", "")
        p = float(f.get("closedPnl", 0))
        if c not in coin_stats:
            coin_stats[c] = {"w": 0, "l": 0, "pnl": 0.0}
        if p > 0:
            coin_stats[c]["w"] += 1
        else:
            coin_stats[c]["l"] += 1
        coin_stats[c]["pnl"] += p
    top_coins = sorted(coin_stats.items(), key=lambda x: -(x[1]["w"] + x[1]["l"]))[:4]
    coin_str = "  ".join(
        f"{c}({v['w']/(v['w']+v['l']):.0%},{v['w']+v['l']}T,${v['pnl']:+,.0f})"
        for c, v in top_coins
    )

    last_h = (now_ms - float(fills[-1].get("time", 0))) / 3_600_000

    return {
        "wr50":   wr(closes[-50:]),
        "wr_all": wr(closes),
        "streak": streak,
        "tpd":    round(len(r30) / 30, 1),
        "tpd_7d": round(len(r7) / 7, 1),
        "tpd_1d": len(r1),
        "hold":   round(hs["mean_closed_h"], 1) if hs["mean_closed_h"] is not None else (round(hs["med_open_h"], 1) if hs["med_open_h"] is not None else 99),
        "hold_str": fmt_hold_short(hs),
        "med_hold_h": hs["med_closed_h"],
        "med_open_h": hs["med_open_h"],
        "pct_intraday": hs["pct_intraday"],
        "pct_multiday": hs["pct_multiday"],
        "max_c":  max(snaps) if snaps else 0,
        "avg_c":  round(sum(snaps) / len(snaps), 1) if snaps else 0,
        "pw":     pw,
        "tw":     len(wks),
        "mls":    mls,
        "last_h": round(last_h, 1),
        "coins":  coin_str,
        "nfills": len(fills),
        "closes": len(closes),
    }

def score(p, pnl):
    freq  = min(p["tpd"] / 10, 1.0)
    wr_s  = max(0, min((p["wr50"] - 0.50) / 0.30, 1.0))
    str_s = max(-0.5, min(p["streak"] / 10, 1.0))
    hold  = max(0, 1.0 - p["hold"] / 12)
    pnl_s = min(pnl / 2_000_000, 1.0)
    rec   = 0.10 if p["last_h"] < 6 else (0 if p["last_h"] < 24 else (-0.10 if p["last_h"] < 72 else -0.30))
    wc    = (p["pw"] / p["tw"] - 0.5) * 0.4 if p["tw"] else 0
    mls_p = max(-0.15, -(p["mls"] - 5) * 0.03)
    conc  = -0.15 if p["max_c"] > 20 else 0
    return round(freq * 0.28 + wr_s * 0.22 + str_s * 0.17 + hold * 0.12 + pnl_s * 0.08 + rec + wc * 0.06 + mls_p + conc, 4)

def print_row(i_tag, t):
    ss  = f"+{t['streak']}" if t["streak"] > 0 else str(t["streak"])
    wc  = f"{t['pw']}/{t['tw']}wk"
    lh  = f"{t['last_h']:.0f}h"
    print(
        f"  {i_tag:<6} {t['addr'][:12]}..  ${t['pnl']/1e6:5.1f}M"
        f"  WR={t['wr50']:.0%}/{t['wr_all']:.0%}"
        f"  T/d={t['tpd']:4.1f}({t['tpd_7d']:3.1f})"
        f"  hold={t['hold_str']:>5}"
        f"  str={ss:>4}"
        f"  conc={t['avg_c']:.0f}/{t['max_c']}"
        f"  {wc}"
        f"  mls={t['mls']}"
        f"  act={lh}"
        f"  sc={t['score']:.4f}"
    )


# ── Stage 1 ────────────────────────────────────────────────────────────────────
print("Fetching leaderboard...")
data = get(STATS)
rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
print(f"  {len(rows):,} traders total")

parsed = []
for e in rows:
    addr = e.get("ethAddress", "")
    if not addr or len(addr) != 42:
        continue
    w  = parse_windows(e.get("windowPerformances", []))
    at = w.get("allTime", {})
    wk = w.get("week", {})
    dy = w.get("day", {})
    parsed.append({
        "addr":     addr,
        "pnl":      at.get("pnl", 0),
        "vlm":      at.get("vlm", 0),
        "week_pnl": wk.get("pnl", 0),
        "day_vlm":  dy.get("vlm", 0),
    })

s1 = [
    t for t in parsed
    if t["pnl"]      > 200_000
    and t["week_pnl"] > 0
    and t["day_vlm"]  > 10_000
    and t["vlm"]      > 1_000_000
]
s1.sort(key=lambda t: -t["pnl"])
candidates = [t for t in s1[:70] if t["addr"] not in CURRENT][:50]
print(f"  Stage 1: {len(s1)} qualify | profiling top {len(candidates)} non-current\n")

# ── Stage 2: profile ───────────────────────────────────────────────────────────
results = []
for i, t in enumerate(candidates, 1):
    sys.stdout.write(f"\r  [{i:02d}/{len(candidates)}] {t['addr'][:14]}...  ")
    sys.stdout.flush()
    time.sleep(0.9)
    p = profile(t["addr"])
    if not p:
        continue
    if p["max_c"] > 15:      # skip basket traders
        continue
    if p["last_h"] > 72:     # skip inactive
        continue
    if p["wr50"] < 0.52:     # skip bad WR
        continue
    s = score(p, t["pnl"])
    results.append({"addr": t["addr"], "pnl": t["pnl"], **p, "score": s})

results.sort(key=lambda t: -t["score"])
print(f"\n  Qualified: {len(results)}\n")

# ── Profile current 4 for comparison ─────────────────────────────────────────
print("Profiling current whitelist for comparison...")
current_results = []
for addr in CURRENT:
    time.sleep(0.9)
    p = profile(addr)
    if p:
        pnl = CURRENT_PNL.get(addr, 1_000_000)
        s   = score(p, pnl)
        current_results.append({"addr": addr, "pnl": pnl, **p, "score": s})
current_results.sort(key=lambda t: -t["score"])

# ── Print results ──────────────────────────────────────────────────────────────
HDR = (f"  {'':6} {'addr':12}  {'PnL':>7}  {'WR50/all':^12}  {'T/d(7d)':>9}"
       f"  {'hold':>6}  {'streak':>5}  {'conc':>7}  {'weeks':>6}  {'mls':>4}  {'active':>6}  {'score':>7}")

print()
print("=" * 115)
print("  CURRENT WHITELIST")
print("=" * 115)
print(HDR)
print("  " + "-" * 110)
for t in current_results:
    print_row("[C]", t)

print()
print("=" * 115)
print("  NEW CANDIDATES — RANKED BY COMPOSITE SCORE")
print("=" * 115)
print(HDR)
print("  " + "-" * 110)
for i, t in enumerate(results[:20], 1):
    print_row(f"[{i:02d}]", t)

print()
print("TOP COINS per candidate (WR%, trade count, PnL):")
print("-" * 100)
for i, t in enumerate(results[:12], 1):
    print(f"  [{i:02d}] {t['addr'][:14]}…  {t['coins']}")
print()
print("CURRENT:")
for t in current_results:
    print(f"  [C]  {t['addr'][:14]}…  {t['coins']}")

print()
print("=" * 115)
print("  VERDICT")
print("=" * 115)
beats = [t for t in results if t["score"] > min(c["score"] for c in current_results)]
print(f"  Candidates beating weakest current trader: {len(beats)}")
if beats:
    print("  Top upgrade candidates:")
    for t in beats[:5]:
        ss = f"+{t['streak']}" if t["streak"] > 0 else str(t["streak"])
        print(f"    {t['addr']}  score={t['score']:.4f}  WR={t['wr50']:.0%}  T/d={t['tpd']:.1f}  hold={t['hold']:.1f}h  streak={ss}")
