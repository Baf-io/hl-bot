"""
scan_focused.py — focused trader scanner
Filters leaderboard for conviction traders (<=20 positions, <=200 orders)
Run: python scan_focused.py
"""
import urllib.request, json, time, sys
from collections import defaultdict
from scan_common import hold_stats, fmt_hold_short

HL    = "https://api.hyperliquid.xyz/info"
STATS = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
H     = {"Content-Type": "application/json"}
now_ms   = time.time() * 1000
month_ms = 30 * 86_400_000
week_ms  =  7 * 86_400_000
day_ms   =      86_400_000

CURRENT = {
    "0xfc667adba8d4837586078f4fdcdc29804337ca06",
    "0xf517639a8872e756ac98d3c65507d2ebc25cc032",
    "0x42b6d907f36255d48f70db8b4a2684088a162634",
}
SKIP = {
    "0x31ca8395cf837de08b24da3f660e77761dfb974b",   # 183-pos basket
    "0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00",   # 87-pos algo
    "0x7fdafde5cfb5465924316eced2d3715494c517d1",   # 28-pos, conflicts
    "0x162cc7c861ebd0c06b3d72319201150482518185",   # WR50=14%
    "0x023a3d058020fb76cca98f01b3c48c8938a22355",   # 76-pos basket
}


def post(payload, retries=4):
    data = json.dumps(payload).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(HL, data=data, headers=H, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(4 * (i + 1))
        except Exception:
            pass
    return None


def get(url):
    req = urllib.request.Request(url, headers=H)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read())


def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            out[item[0]] = {
                "pnl": float(item[1].get("pnl", 0)),
                "vlm": float(item[1].get("vlm", 0)),
            }
    return out


def wr(fs):
    if not fs:
        return 0
    return sum(1 for f in fs if float(f.get("closedPnl", 0)) > 0) / len(fs)


# Stage 1 ─────────────────────────────────────────────────────────────────────
print("Fetching leaderboard...")
data = get(STATS)
rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
print(f"  {len(rows):,} traders total")

candidates = []
for e in rows:
    addr = e.get("ethAddress", "")
    if not addr or len(addr) != 42:
        continue
    if addr.lower() in {a.lower() for a in CURRENT | SKIP}:
        continue
    w       = parse_windows(e.get("windowPerformances", []))
    at      = w.get("allTime", {})
    wk      = w.get("week", {})
    dy      = w.get("day", {})
    at_pnl  = at.get("pnl", 0)
    wk_pnl  = wk.get("pnl", 0)
    day_vlm = dy.get("vlm", 0)
    if at_pnl  < 500_000: continue
    if wk_pnl  <= 0:      continue
    if day_vlm < 5_000:   continue
    candidates.append({
        "addr":    addr,
        "at_pnl":  at_pnl,
        "wk_pnl":  wk_pnl,
        "mo_pnl":  w.get("month", {}).get("pnl", 0),
        "at_vlm":  at.get("vlm", 0),
        "day_vlm": day_vlm,
    })

candidates.sort(key=lambda x: -x["at_pnl"])
print(f"  Stage 1: {len(candidates)} pass (AT PnL>500K, week+, active today)")
print()

# Stage 2 ─────────────────────────────────────────────────────────────────────
print("Stage 2: position check (skip >20 positions / >200 orders)...")
focused = []
for i, c in enumerate(candidates[:80]):
    sys.stdout.write(f"\r  [{i+1:02d}/80] {c['addr'][:14]}...  ")
    sys.stdout.flush()
    time.sleep(0.5)

    state = post({"type": "clearinghouseState", "user": c["addr"]})
    if not state:
        continue
    ms        = state.get("marginSummary", {})
    acct_val  = float(ms.get("accountValue", 0))
    positions = [p for p in state.get("assetPositions", [])
                 if float(p["position"]["szi"]) != 0]
    n_pos = len(positions)
    if n_pos == 0: continue
    if n_pos > 20: continue

    orders = post({"type": "openOrders", "user": c["addr"]}) or []
    if len(orders) > 200: continue

    total_upnl = sum(float(p["position"].get("unrealizedPnl", 0)) for p in positions)
    upnl_pct   = total_upnl / acct_val * 100 if acct_val else 0

    focused.append({
        **c,
        "acct_val":  acct_val,
        "n_pos":     n_pos,
        "n_ord":     len(orders),
        "upnl_pct":  upnl_pct,
        "positions": positions,
    })

print(f"\n  Focused traders: {len(focused)}")
print()

# Stage 3 ─────────────────────────────────────────────────────────────────────
print("Stage 3: fill profiling top 30...")
results = []
for i, c in enumerate(focused[:30]):
    sys.stdout.write(f"\r  [{i+1:02d}/30] {c['addr'][:14]}...  ")
    sys.stdout.flush()
    time.sleep(0.9)

    fills = post({"type": "userFills", "user": c["addr"]})
    if not fills or not isinstance(fills, list) or len(fills) < 10:
        continue
    fills.sort(key=lambda f: float(f.get("time", 0)))

    r30    = [f for f in fills if now_ms - float(f.get("time", 0)) < month_ms]
    r7     = [f for f in fills if now_ms - float(f.get("time", 0)) < week_ms]
    r1     = [f for f in fills if now_ms - float(f.get("time", 0)) < day_ms]
    closes = [f for f in fills if float(f.get("closedPnl", 0)) != 0]
    r30c   = [f for f in closes if now_ms - float(f.get("time", 0)) < month_ms]

    if len(r30) < 5:
        continue

    perp30   = [f for f in r30 if not f.get("coin", "").startswith(("xyz:", "km:", "@", "k:"))]
    coin_vol = defaultdict(float)
    for f in perp30:
        coin_vol[f.get("coin", "?")] += abs(float(f.get("sz", 0)) * float(f.get("px", 0)))
    top_coins = sorted(coin_vol.items(), key=lambda x: -x[1])[:4]

    hs = hold_stats(fills, now_ms)   # full history, episode-based (incl. open)
    avg_hold = (hs["mean_closed_h"] if hs["mean_closed_h"] is not None
                else (hs["med_open_h"] or 0))

    streak = 0
    for f in reversed(closes[-50:]):
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

    last_h       = (now_ms - float(fills[-1].get("time", 0))) / 3_600_000
    realized_30d = sum(float(f.get("closedPnl", 0)) for f in r30c)

    results.append({
        **c,
        "wr50":         wr(closes[-50:]),
        "wr30d":        wr(r30c),
        "streak":       streak,
        "avg_hold":     avg_hold,
        "hold_str":     fmt_hold_short(hs),
        "med_hold_h":   hs["med_closed_h"],
        "med_open_h":   hs["med_open_h"],
        "pct_intraday": hs["pct_intraday"],
        "pct_multiday": hs["pct_multiday"],
        "tpd":          round(len(r30) / 30, 1),
        "tpd7":         round(len(r7) / 7, 1),
        "fills_1d":     len(r1),
        "n30c":         len(r30c),
        "realized_30d": realized_30d,
        "top_coins":    top_coins,
        "last_h":       round(last_h, 1),
    })

results.sort(key=lambda x: -(x["at_pnl"] + x["wk_pnl"] * 10 + x["upnl_pct"] * 5000))
print(f"\n  Profiled: {len(results)}")
print()

# Print results ────────────────────────────────────────────────────────────────
print("=" * 108)
print("  FOCUSED TRADER CANDIDATES  (<=20 positions, <=200 orders, AT PnL>500K)")
print("=" * 108)
for t in results[:18]:
    ss = f"+{t['streak']}" if t['streak'] > 0 else str(t['streak'])
    print(
        f"  {t['addr'][:16]}  "
        f"AT=${t['at_pnl']/1e6:.1f}M  "
        f"7d=${t['wk_pnl']/1e3:+.0f}K  "
        f"30dR=${t['realized_30d']/1e3:+.0f}K  "
        f"pos={t['n_pos']}  "
        f"ord={t['n_ord']}  "
        f"uPnL={t['upnl_pct']:+.1f}%  "
        f"WR={t['wr50']:.0%}  "
        f"hold={t['hold_str']}  "
        f"str={ss}  "
        f"T/d={t['tpd']:.1f}  "
        f"last={t['last_h']:.0f}h"
    )

print()
print("POSITIONS + COIN FOCUS:")
print("-" * 108)
for t in results[:14]:
    pos_parts = []
    for item in t["positions"][:7]:
        p    = item["position"]
        szi  = float(p["szi"])
        ep   = float(p.get("entryPx") or 0)
        d    = "L" if szi > 0 else "S"
        ntl  = abs(szi) * ep
        upnl = float(p.get("unrealizedPnl", 0))
        pct_pos  = upnl / ntl * 100 if ntl else 0
        pct_acct = ntl / t["acct_val"] * 100 if t["acct_val"] else 0
        pos_parts.append(f"{p['coin']}{d}({pct_acct:.0f}%,{pct_pos:+.0f}%)")
    total_cv = max(sum(vv for _, vv in t["top_coins"]), 1)
    coins_str = "  ".join(
        f"{c}({v/total_cv*100:.0f}%)"
        for c, v in t["top_coins"]
    )
    print(f"  {t['addr'][:16]}  acct=${t['acct_val']/1e6:.1f}M  " + "  ".join(pos_parts))
    print(f"  {'':16}  vol focus: {coins_str}")
    print()

print("Done.")
