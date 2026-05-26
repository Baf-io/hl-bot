"""
scan_specialists.py — Coin-specialist trader hunter
====================================================
Finds traders concentrated in coins our current whitelist does NOT cover.
New unique coin = new position slot = actual capital working.

Logic:
  Stage 1 : Leaderboard filter (PnL, activity, week positive)
  Stage 2 : Position state  — skip basket traders (>12 open), confirm active
  Stage 3 : Fill concentration — skip unless >=60% volume in <=2 coins
  Stage 4 : Deep per-coin profile — WR, hold time, leverage, streaks,
             weekly consistency, max drawdown per coin, direction bias
  Stage 5 : Conflict check vs occupied coins, print ranked specialists

Usage:
    python scan_specialists.py              # scan all target coins
    python scan_specialists.py SOL          # SOL specialists only
    python scan_specialists.py SOL AVAX SUI # multiple targets
"""
import sys, time, json, math, urllib.request, urllib.error
from collections import defaultdict
from scan_common import hold_stats, fmt_hold, fmt_hold_short

sys.stdout.reconfigure(encoding="utf-8")

# ── Config ────────────────────────────────────────────────────────────────────
HL    = "https://api.hyperliquid.xyz/info"
STATS = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
H     = {"Content-Type": "application/json"}

now_ms   = time.time() * 1000
MS_HOUR  = 3_600_000
MS_DAY   = 86_400_000
MS_WEEK  = 7  * MS_DAY
MS_MONTH = 30 * MS_DAY

# Current traders & their occupied coins — update when whitelist changes
CURRENT_TRADERS = {
    "0xfc667adba8d4837586078f4fdcdc29804337ca06",
    "0x42b6d907f36255d48f70db8b4a2684088a162634",
    "0xa9b95f2a2e7ef219021efc5c04c32761b8553bbd",
}

# Coins already covered by our whitelist traders (dedup means no new value)
# NOTE: SOL removed — it was only in test hypotheticals; fc667 may not hold SOL right now
OCCUPIED_COINS = {"BTC", "ETH", "HYPE", "ZEC"}

# Target coins to hunt specialists for.
# Ordered by interest — include ALL coins where specialists have been observed.
# Meme tier added: MEGA, PURR, GRASS, PENGU (actual specialists found on HL)
# Alt tier: SOL, LIT, TON, NEAR, WLD, ZRO are where real specialists live
TARGET_COINS_ALL = [
    # Real-market alts with confirmed specialists on HL leaderboard
    "SOL", "LIT", "TON", "NEAR", "ZRO", "WLD", "PENGU", "GRASS",
    # Meme / HL-native with confirmed specialists
    "MEGA", "PURR", "PEPE", "WIF", "BONK", "FLOKI",
    # Blue-chip alts (fewer specialists but worth scanning)
    "AVAX", "SUI", "APT", "ARB", "LINK", "DOT", "ADA", "XRP",
    "ATOM", "OP", "INJ", "TIA", "SEI",
    # DeFi / narrative
    "ONDO", "W", "EIGEN", "PENDLE",
    # Legacy
    "LTC", "BCH", "XMR",
]

# CLI: optionally filter to specific coins
cli_targets = [a.upper() for a in sys.argv[1:] if not a.startswith("-")]
TARGET_COINS = cli_targets if cli_targets else TARGET_COINS_ALL

SKIP_PREFIXES = ("xyz:", "@", "km:", "k:")  # tokenised stocks / synthetics

# Thresholds
MIN_AT_PNL          = 30_000     # minimum all-time PnL (specialists aren't always top earners)
MIN_WR              = 0.55       # minimum WR on specialty coin
MIN_COIN_TRADES     = 10         # minimum trades in specialty coin
MIN_CONCENTRATION   = 0.40       # >= 40% of 30d volume in specialty coin (more realistic)
MAX_OPEN_POSITIONS  = 12         # skip basket traders
MIN_HOLD_MINUTES    = 10         # skip only pure HFT (lower bar)
MAX_LAST_ACTIVE_H   = 120        # skip if no trade in 5 days
MIN_WEEKLY_PROF_PCT = 0.45       # at least 45% of last 8 weeks profitable
STAGE2_LIMIT        = 500        # check more traders (specialists aren't top PnL whales)
STAGE3_LIMIT        = 250        # deep profile more candidates

# ── Helpers ───────────────────────────────────────────────────────────────────

def get(url, retries=3):
    req = urllib.request.Request(url, headers=H)
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except Exception as e:
            if i < retries - 1:
                time.sleep(2)
    return None


def post(payload, retries=4):
    data = json.dumps(payload).encode()
    for i in range(retries):
        try:
            req = urllib.request.Request(HL, data=data, headers=H, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (i + 1))
        except Exception:
            time.sleep(1)
    return None


def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            w, s = item
            out[w] = {"pnl": float(s.get("pnl", 0)), "vlm": float(s.get("vlm", 0))}
    return out


def bar(v, width=10):
    filled = round(v * width)
    return "#" * filled + "." * (width - filled)


# ── Stage 1: Leaderboard ─────────────────────────────────────────────────────

print("=" * 90)
print("  COIN-SPECIALIST SCANNER")
if cli_targets:
    print(f"  Target coins: {', '.join(TARGET_COINS)}")
else:
    print(f"  Scanning ALL {len(TARGET_COINS_ALL)} target coins")
print(f"  Occupied coins (skip conflicts): {', '.join(sorted(OCCUPIED_COINS))}")
print("=" * 90)

print("\n[1] Fetching leaderboard...")
data = get(STATS)
if not data:
    print("ERROR: could not fetch leaderboard")
    sys.exit(1)
rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
print(f"    {len(rows):,} traders total")

s1 = []
for e in rows:
    addr = e.get("ethAddress", "")
    if not addr or len(addr) != 42:
        continue
    if addr.lower() in {a.lower() for a in CURRENT_TRADERS}:
        continue
    w      = parse_windows(e.get("windowPerformances", []))
    at     = w.get("allTime", {})
    wk     = w.get("week",    {})
    dy     = w.get("day",     {})
    at_pnl = at.get("pnl", 0)
    wk_pnl = wk.get("pnl", 0)
    dy_vlm = dy.get("vlm", 0)
    at_vlm = at.get("vlm", 0)
    if at_pnl  < MIN_AT_PNL: continue
    if wk_pnl  <= 0:          continue
    if at_vlm  < 100_000:     continue   # lowered from 500k — small-book specialists
    s1.append({"addr": addr, "at_pnl": at_pnl, "wk_pnl": wk_pnl,
               "dy_vlm": dy_vlm, "at_vlm": at_vlm})

# Sort: blend all-time PnL with week PnL (recent activity bonus)
# Specialists won't be in the pure PnL top — mix in weekly performance
s1.sort(key=lambda x: -(x["at_pnl"] * 0.5 + x["wk_pnl"] * 5 * 0.5))
print(f"    Stage 1: {len(s1)} pass filters | will check top {min(STAGE2_LIMIT, len(s1))}")


# ── Stage 2: Position state + basket filter ───────────────────────────────────

print(f"\n[2] Position state check (skip >{MAX_OPEN_POSITIONS} open)...")
s2 = []
for i, c in enumerate(s1[:STAGE2_LIMIT]):
    sys.stdout.write(f"\r    [{i+1:03d}/{min(STAGE2_LIMIT, len(s1))}] {c['addr'][:14]}...  ")
    sys.stdout.flush()
    time.sleep(0.5)

    state = post({"type": "clearinghouseState", "user": c["addr"]})
    if not state:
        continue
    ms       = state.get("marginSummary", {})
    acct_val = float(ms.get("accountValue", 0))
    raw_pos  = [p for p in state.get("assetPositions", [])
                if float(p["position"]["szi"]) != 0
                and not p["position"]["coin"].startswith(SKIP_PREFIXES)]
    n_pos = len(raw_pos)

    if n_pos > MAX_OPEN_POSITIONS:
        continue   # basket trader
    if acct_val < 10_000:
        continue   # too small

    # Summarise current open positions
    open_coins = {}
    for ap in raw_pos:
        pos  = ap["position"]
        szi  = float(pos["szi"])
        ep   = float(pos.get("entryPx") or 0)
        coin = pos["coin"]
        ntl  = abs(szi) * ep
        mu   = float(pos.get("marginUsed") or 0)
        lev  = round(ntl / mu, 1) if mu > 0 else 1.0
        open_coins[coin] = {
            "dir": "LONG" if szi > 0 else "SHORT",
            "ntl": ntl,
            "lev": lev,
            "upnl": float(pos.get("unrealizedPnl", 0)),
        }

    s2.append({**c, "acct_val": acct_val, "n_pos": n_pos, "open_coins": open_coins})

print(f"\n    Passed: {len(s2)} traders")


# ── Stage 3: Fill concentration ───────────────────────────────────────────────

print(f"\n[3] Concentration analysis (>={MIN_CONCENTRATION:.0%} volume in specialty coin)...")
s3 = []
for i, c in enumerate(s2[:STAGE3_LIMIT]):
    sys.stdout.write(f"\r    [{i+1:02d}/{min(STAGE3_LIMIT, len(s2))}] {c['addr'][:14]}...  ")
    sys.stdout.flush()
    time.sleep(0.9)

    fills = post({"type": "userFills", "user": c["addr"]})
    if not fills or not isinstance(fills, list) or len(fills) < 20:
        continue
    fills.sort(key=lambda f: float(f.get("time", 0)))

    r30 = [f for f in fills
           if now_ms - float(f.get("time", 0)) < MS_MONTH
           and not f.get("coin", "").startswith(SKIP_PREFIXES)]
    if len(r30) < 10:
        continue

    # Volume per coin in last 30d
    coin_vol  = defaultdict(float)
    coin_fills = defaultdict(list)
    for f in r30:
        coin = f.get("coin", "")
        vol  = abs(float(f.get("sz", 0))) * float(f.get("px", 0))
        coin_vol[coin]   += vol
        coin_fills[coin].append(f)

    total_vol = sum(coin_vol.values()) or 1
    top2      = sorted(coin_vol.items(), key=lambda x: -x[1])[:2]
    top2_vol  = sum(v for _, v in top2)
    conc      = top2_vol / total_vol

    # Debug: log why traders fail
    primary_coin = top2[0][0] if top2 else "?"
    secondary    = top2[1][0] if len(top2) > 1 else ""
    if conc < MIN_CONCENTRATION:
        sys.stdout.write(
            f"\r    skip {c['addr'][:12]} conc={conc:.0%} top={primary_coin}/{secondary}  \n"
        )
        sys.stdout.flush()
        continue

    if primary_coin not in TARGET_COINS:
        sys.stdout.write(
            f"\r    skip {c['addr'][:12]} coin={primary_coin} not in targets           \n"
        )
        sys.stdout.flush()
        continue
    # And must NOT conflict with occupied coins
    if primary_coin in OCCUPIED_COINS:
        sys.stdout.write(
            f"\r    skip {c['addr'][:12]} coin={primary_coin} occupied                 \n"
        )
        sys.stdout.flush()
        continue

    s3.append({
        **c,
        "fills":        fills,
        "r30":          r30,
        "coin_vol":     dict(coin_vol),
        "coin_fills":   dict(coin_fills),
        "concentration": conc,
        "primary_coin": primary_coin,
        "top2":         top2,
    })

print(f"\n    Specialists found: {len(s3)}")


# ── Stage 4: Deep per-coin profile ────────────────────────────────────────────

print(f"\n[4] Deep profiling {len(s3)} specialists...")

def profile_coin(fills_all, coin, now_ms):
    """Full per-coin analysis for a specialist trader."""
    cf = [f for f in fills_all if f.get("coin") == coin]
    closes = [f for f in cf if float(f.get("closedPnl", 0)) != 0]
    r30c   = [f for f in closes if now_ms - float(f.get("time", 0)) < MS_MONTH]
    r7c    = [f for f in closes if now_ms - float(f.get("time", 0)) < MS_WEEK]

    if len(closes) < MIN_COIN_TRADES:
        return None

    # Win rate (last 50 closes, all-time, 30d)
    def wr(s):
        return sum(1 for f in s if float(f.get("closedPnl", 0)) > 0) / len(s) if s else 0

    wr50  = wr(closes[-50:])
    wr_at = wr(closes)
    wr_30 = wr(r30c) if r30c else wr_at

    if wr50 < MIN_WR:
        return None

    # Realized PnL
    real_at  = sum(float(f.get("closedPnl", 0)) for f in closes)
    real_30d = sum(float(f.get("closedPnl", 0)) for f in r30c)
    real_7d  = sum(float(f.get("closedPnl", 0)) for f in r7c)

    # Hold time via episode reconstruction over full history (incl. currently-open
    # position). med closed-hold and med open-age are reported separately so a
    # patient holder isn't mistaken for "no data" — see scan_common.py.
    hs = hold_stats(cf, now_ms, coin)
    avg_hold = (hs["mean_closed_h"] if hs["mean_closed_h"] is not None
                else (hs["med_open_h"] or 0))
    if (hs["mean_closed_h"] is not None
            and hs["mean_closed_h"] < MIN_HOLD_MINUTES / 60
            and hs["n_closed"] > 3):
        return None  # pure HFT

    # Direction bias (long/short ratio)
    opens_d = [f for f in cf if "Open" in str(f.get("dir", ""))]
    longs   = sum(1 for f in opens_d if "Long" in str(f.get("dir", "")))
    shorts  = len(opens_d) - longs
    if opens_d:
        bias = "LONG" if longs > shorts * 1.5 else ("SHORT" if shorts > longs * 1.5 else "BOTH")
    else:
        bias = "?"

    # Leverage (from non-zero fee fills)
    levs = []
    for f in closes[-30:]:
        ntl = abs(float(f.get("sz", 0))) * float(f.get("px", 0))
        fee = abs(float(f.get("fee", 0)))
        # Very rough: fee rate is ~0.03%, so lev ≈ notional / (fee / 0.0003) — skip, use margin
        if ntl > 0:
            levs.append(ntl)   # placeholder; real lev from position state

    # Win streak (current)
    streak = 0
    for f in reversed(closes):
        p = float(f.get("closedPnl", 0))
        if streak == 0:
            streak = 1 if p > 0 else -1
        elif streak > 0 and p > 0:
            streak += 1
        elif streak < 0 and p < 0:
            streak -= 1
        else:
            break

    # Max losing streak
    mls = cl = 0
    for f in closes:
        if float(f.get("closedPnl", 0)) < 0:
            cl += 1; mls = max(mls, cl)
        else:
            cl = 0

    # Max single-trade loss
    worst_trade = min((float(f.get("closedPnl", 0)) for f in closes), default=0)

    # Weekly consistency (last 8 weeks)
    wk_pnl = {}
    for f in closes:
        wid = int(float(f.get("time", 0)) / MS_WEEK)
        wk_pnl[wid] = wk_pnl.get(wid, 0) + float(f.get("closedPnl", 0))
    wks = sorted(wk_pnl.items())[-8:]
    prof_weeks = sum(1 for _, p in wks if p > 0)
    wk_pct = prof_weeks / len(wks) if wks else 0

    # Trade frequency per day (30d)
    tpd = len(r30c) / 30

    # Avg win / avg loss ratio
    wins_pnl  = [float(f.get("closedPnl", 0)) for f in closes if float(f.get("closedPnl", 0)) > 0]
    loss_pnl  = [float(f.get("closedPnl", 0)) for f in closes if float(f.get("closedPnl", 0)) < 0]
    avg_win   = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0
    avg_loss  = sum(loss_pnl) / len(loss_pnl) if loss_pnl else 0
    rr        = abs(avg_win / avg_loss) if avg_loss else 999

    return {
        "wr50":        round(wr50,  3),
        "wr_at":       round(wr_at, 3),
        "wr_30d":      round(wr_30, 3),
        "real_at":     real_at,
        "real_30d":    real_30d,
        "real_7d":     real_7d,
        "avg_hold_h":  round(avg_hold, 2),
        "med_hold_h":  round(hs["med_closed_h"], 2) if hs["med_closed_h"] is not None else None,
        "med_open_h":  round(hs["med_open_h"], 2) if hs["med_open_h"] is not None else None,
        "n_open":      hs["n_open"],
        "pct_intraday": round(hs["pct_intraday"], 3) if hs["pct_intraday"] is not None else None,
        "pct_multiday": round(hs["pct_multiday"], 3) if hs["pct_multiday"] is not None else None,
        "hold_str":    fmt_hold_short(hs),
        "hold_full":   fmt_hold(hs),
        "bias":        bias,
        "streak":      streak,
        "mls":         mls,
        "worst_trade": worst_trade,
        "tpd":         round(tpd, 2),
        "wk_pct":      round(wk_pct, 2),
        "prof_weeks":  prof_weeks,
        "total_weeks": len(wks),
        "rr":          round(rr, 2),
        "avg_win":     avg_win,
        "avg_loss":    avg_loss,
        "n_trades":    len(closes),
        "n_30d":       len(r30c),
    }


def specialist_score(p_coin, at_pnl, conc, n_pos, last_h):
    """Composite score tuned for copy-trading suitability."""
    # Core quality
    wr_s    = max(0, (p_coin["wr50"] - 0.55) / 0.35)          # 0 at 55%, 1 at 90%+
    rr_s    = min(p_coin["rr"] / 4.0, 1.0)                    # reward/risk ratio
    wk_s    = p_coin["wk_pct"]                                 # weekly consistency
    # Conviction
    conc_s  = min((conc - 0.55) / 0.35, 1.0)                  # 0 at 55%, 1 at 90%+
    # Safety
    mls_p   = max(-0.25, -(p_coin["mls"] - 4) * 0.05)         # penalise long loss streaks
    hold_s  = min(p_coin["avg_hold_h"] / 8.0, 1.0)            # hold > 8h = max score
    if p_coin["avg_hold_h"] < MIN_HOLD_MINUTES / 60:
        hold_s = 0
    # Activity
    rec_s   = 1.0 if last_h < 6 else (0.7 if last_h < 24 else (0.3 if last_h < 72 else 0.0))
    # Pnl
    pnl_s   = min(at_pnl / 2_000_000, 1.0)
    # Focus (fewer open positions = more conviction)
    focus_s = max(0, 1.0 - (n_pos - 1) / 10)

    total = (
        wr_s   * 0.30
        + rr_s   * 0.12
        + wk_s   * 0.18
        + conc_s * 0.14
        + hold_s * 0.10
        + focus_s* 0.06
        + pnl_s  * 0.05
        + rec_s  * 0.05
        + mls_p
    )
    return round(total, 4)


results = []
for i, c in enumerate(s3):
    sys.stdout.write(f"\r    [{i+1:02d}/{len(s3)}] {c['addr'][:14]}...  ")
    sys.stdout.flush()

    p = profile_coin(c["fills"], c["primary_coin"], now_ms)
    if not p:
        continue

    last_h = (now_ms - float(c["fills"][-1].get("time", 0))) / MS_HOUR
    if last_h > MAX_LAST_ACTIVE_H:
        continue
    if p["wk_pct"] < MIN_WEEKLY_PROF_PCT:
        continue

    # Conflict: does their current book overlap with OCCUPIED_COINS?
    conflict_coins = [coin for coin in c["open_coins"] if coin in OCCUPIED_COINS]
    addable_coins  = [coin for coin in c["open_coins"] if coin not in OCCUPIED_COINS]

    sc = specialist_score(p, c["at_pnl"], c["concentration"], c["n_pos"], last_h)

    # Current position in the specialty coin
    specialty_pos = c["open_coins"].get(c["primary_coin"])

    results.append({
        "addr":          c["addr"],
        "at_pnl":        c["at_pnl"],
        "wk_pnl":        c["wk_pnl"],
        "acct_val":      c["acct_val"],
        "n_pos":         c["n_pos"],
        "coin":          c["primary_coin"],
        "top2":          c["top2"],
        "concentration": c["concentration"],
        "open_coins":    c["open_coins"],
        "conflict":      conflict_coins,
        "addable":       addable_coins,
        "specialty_pos": specialty_pos,
        "last_h":        round(last_h, 1),
        "score":         sc,
        **{f"p_{k}": v for k, v in p.items()},
    })

results.sort(key=lambda x: -x["score"])
print(f"\n    Qualified specialists: {len(results)}")


# ── Stage 5: Output ───────────────────────────────────────────────────────────

# Group by target coin
by_coin = defaultdict(list)
for r in results:
    by_coin[r["coin"]].append(r)

def fmt_streak(s):
    return f"+{s}" if s > 0 else str(s)

def fmt_money(v):
    if abs(v) >= 1_000_000: return f"${v/1e6:+.1f}M"
    if abs(v) >= 1_000:     return f"${v/1e3:+.0f}K"
    return f"${v:+.0f}"

print("\n\n" + "=" * 90)
print("  COIN SPECIALIST RESULTS — ranked by copy-suitability score")
print("=" * 90)

coins_with_results = [c for c in TARGET_COINS if c in by_coin]
print(f"  Found specialists for: {', '.join(coins_with_results) or 'none'}")
print(f"  No specialists found : {', '.join(c for c in TARGET_COINS if c not in by_coin)}")

for coin in coins_with_results:
    traders = by_coin[coin][:5]   # top 5 per coin
    print(f"\n{'─'*90}")
    print(f"  {coin} SPECIALISTS  ({len(by_coin[coin])} found, showing top {len(traders)})")
    print(f"{'─'*90}")
    for r in traders:
        ss = fmt_streak(r["p_streak"])
        wk = f"{r['p_prof_weeks']}/{r['p_total_weeks']}wk"
        conf = f"  [CONFLICT: {', '.join(r['conflict'])}]" if r["conflict"] else ""
        cur  = ""
        if r["specialty_pos"]:
            sp = r["specialty_pos"]
            cur = f"  NOW: {sp['dir']} ${sp['ntl']:,.0f} {sp['lev']:.0f}x"

        print(f"  {r['addr']}  sc={r['score']:.4f}  AT={fmt_money(r['at_pnl'])}  "
              f"acct={fmt_money(r['acct_val'])}{conf}")
        print(f"    WR: 50tr={r['p_wr50']:.0%}  30d={r['p_wr_30d']:.0%}  all={r['p_wr_at']:.0%}"
              f"   R:R={r['p_rr']:.2f}x"
              f"   hold={r['p_hold_full']}"
              f"   bias={r['p_bias']}"
              f"   streak={ss}")
        pi, pm = r.get('p_pct_intraday'), r.get('p_pct_multiday')
        if pi is not None:
            verdict = ("SCALPER — not copy-able at 45s reconcile" if pi >= 0.6
                       else "SWING — copy-able" if pm and pm >= 0.5 else "mixed cadence")
            print(f"    cadence: intraday<1h={pi:.0%}  multiday>=24h={pm:.0%}  "
                  f"open_pos={r.get('p_n_open', 0)}  → {verdict}")
        print(f"    PnL: 7d={fmt_money(r['p_real_7d'])}  30d={fmt_money(r['p_real_30d'])}  "
              f"AT={fmt_money(r['p_real_at'])}"
              f"   worst={fmt_money(r['p_worst_trade'])}")
        print(f"    {wk} profitable   T/d={r['p_tpd']:.1f}   "
              f"trades={r['p_n_trades']}   conc={r['concentration']:.0%} in {coin}"
              f"   active={r['last_h']:.0f}h ago{cur}")
        print(f"    Volume focus: "
              + "  ".join(f"{c}({v/sum(vv for _,vv in r['top2'])*100:.0f}%)" for c,v in r["top2"])
              + f"   open positions: "
              + ("none" if not r["open_coins"] else
                 "  ".join(f"{c}:{d['dir'][0]}" for c, d in list(r["open_coins"].items())[:6])))
        print()

# ── Summary table ─────────────────────────────────────────────────────────────
print("=" * 90)
print("  SUMMARY TABLE — all qualified specialists")
print("=" * 90)
print(f"  {'Addr':18}  {'Coin':6}  {'Score':>7}  {'WR50':>6}  {'WR30':>6}  {'R:R':>5}  "
      f"{'Hold':>6}  {'Conc':>6}  {'7dPnL':>8}  {'Streak':>7}  {'Wks':>6}  "
      f"{'Pos':>4}  {'Last':>5}  Bias")
print("  " + "-" * 86)
for r in results[:25]:
    ss  = fmt_streak(r["p_streak"])
    wk  = f"{r['p_prof_weeks']}/{r['p_total_weeks']}"
    flag = "*" if r["conflict"] else " "
    print(f"  {r['addr'][:16]}{flag} {r['coin']:<6}  {r['score']:>7.4f}  "
          f"{r['p_wr50']:>5.0%}  {r['p_wr_30d']:>5.0%}  {r['p_rr']:>5.2f}  "
          f"{r['p_hold_str']:>6}  {r['concentration']:>5.0%}  "
          f"{fmt_money(r['p_real_7d']):>8}  {ss:>7}  {wk:>6}  "
          f"{r['n_pos']:>4}  {r['last_h']:>4.0f}h  {r['p_bias']}")
print()
print("  * = has conflicting coins with current whitelist (still addable if unique coin)")

# ── Add-to-whitelist recommendations ─────────────────────────────────────────
print()
print("=" * 90)
print("  RECOMMENDATIONS — traders safe to add NOW")
print("=" * 90)
clean = [r for r in results if not r["conflict"] and r["score"] > 0.35]
for r in clean[:8]:
    lev = r["open_coins"].get(r["coin"], {}).get("lev", "?")
    print(f"  ADD: {r['addr']}")
    print(f"       Coin: {r['coin']}  Bias: {r['p_bias']}  "
          f"WR={r['p_wr50']:.0%}  hold={r['p_hold_full']}  "
          f"lev={lev}x  score={r['score']:.4f}")
    print(f"       7d PnL: {fmt_money(r['p_real_7d'])}  "
          f"30d PnL: {fmt_money(r['p_real_30d'])}")
    print(f"       Currently holds: "
          + ("nothing" if not r["open_coins"] else
             "  ".join(f"{c} {d['dir']}" for c, d in r["open_coins"].items())))
    print()

if not clean:
    print("  No clean candidates above threshold right now.")
    print("  Try running again later or lower MIN_WR / MIN_CONCENTRATION in config.")

# ── Batch guidance ────────────────────────────────────────────────────────────
if not cli_targets and len(TARGET_COINS_ALL) > 6:
    print("=" * 90)
    print("  TIP — run in batches for fresh results:")
    batches = [TARGET_COINS_ALL[i:i+4] for i in range(0, len(TARGET_COINS_ALL), 4)]
    for b in batches:
        print(f"    python scan_specialists.py {' '.join(b)}")
    print()
