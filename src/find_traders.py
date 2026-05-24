"""
find_traders.py — Hyperliquid Leaderboard Deep Scanner v2
══════════════════════════════════════════════════════════
Scans the full HL leaderboard, fetches recent fill history for top candidates,
and scores them on what ACTUALLY matters for copy trading:

  1. FREQUENCY     — trades per day (need steady signal flow, not 1/week)
  2. RECENT WIN %  — last 50 closed trades (recency beats all-time)
  3. WIN STREAK    — current consecutive winning trades (hot hand filter)
  4. HOLD TIME     — shorter = cleaner copy window (we can mirror and exit fast)
  5. REALIZED PNL  — absolute proof of edge, not just % return on small account

Two-stage approach:
  Stage 1: cheap leaderboard filter (PnL, volume, alltime WR)
  Stage 2: per-trader fill analysis (frequency, streak, hold time, recent WR)

Output: data/traders.json  (bot loads this automatically)

Run on VPS:
    cd /root/hl-bot && python src/find_traders.py

Then:
    sudo systemctl restart hl-bot
"""
import asyncio, sys, json, time
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import aiohttp

STATS_URL   = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_REST     = "https://api.hyperliquid.xyz/info"
OUTPUT_FILE = "data/traders.json"

# ── Stage 1 filters (leaderboard data only — fast) ───────────────────────────
MIN_ALLTIME_PNL  = 50_000      # must have made at least $50K ever
MIN_WEEK_PNL     = 0           # net positive this week
MIN_DAY_VLM      = 2_000       # traded at least $2K today (active)
MIN_ALLTIME_VLM  = 100_000
STAGE1_LIMIT     = 200         # deep-scan top 200 by alltime PnL

# ── Stage 2 filters (from fill history) — loose, let scoring do the ranking ──
MIN_TRADES_PER_DAY   = 1.0     # at least 1 trade/day on average
MAX_AVG_HOLD_HOURS   = 24.0    # exclude overnight bagholders
MIN_WIN_RATE_RECENT  = 0.50    # at least 50% on last 50 closed trades
MIN_FILLS            = 10      # need enough data
MIN_TRADES_7D        = 3       # must have traded this week
TOP_N                = 15      # traders written to traders.json


def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            window, stats = item
            out[window] = {
                "pnl": float(stats.get("pnl", 0)),
                "vlm": float(stats.get("vlm", 0)),
                "wr":  float(stats.get("winRate", 0.5)),
            }
    return out


async def profile_trader(session, address: str) -> dict | None:
    """
    Fetch full fill history and compute:
      - trades_per_day  (last 30d)
      - avg_hold_h      (open→close pairs)
      - win_rate_recent (last 50 closes)
      - win_rate_all    (all closes in history)
      - current_streak  (+ = win streak, - = loss streak)
      - trades_7d       (activity check)
      - last_trade_h    (hours since last trade)
      - avg_notional    (their typical position size)
    """
    try:
        async with session.post(
            HL_REST,
            json={"type": "userFills", "user": address},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            fills = await resp.json(content_type=None)

        if not fills or len(fills) < MIN_FILLS:
            return None

        now_ms   = time.time() * 1000
        month_ms = 30 * 86_400_000
        week_ms  =  7 * 86_400_000

        # Sort oldest → newest
        fills = sorted(fills, key=lambda f: float(f.get("time", 0)))

        recent30 = [f for f in fills if now_ms - float(f.get("time", 0)) < month_ms]
        last7d   = [f for f in fills if now_ms - float(f.get("time", 0)) < week_ms]

        if len(recent30) < MIN_FILLS:
            return None

        # ── Trades per day ─────────────────────────────────────────────────────
        trades_per_day = len(recent30) / 30

        # ── Win rate (all closes & recent 50) ──────────────────────────────────
        all_closes = [f for f in fills if float(f.get("closedPnl", 0)) != 0]
        recent_closes = all_closes[-50:]   # last 50 closed trades

        def wr(subset):
            if len(subset) < 3:
                return 0.5
            wins = sum(1 for f in subset if float(f.get("closedPnl", 0)) > 0)
            return wins / len(subset)

        win_rate_all    = wr(all_closes)
        win_rate_recent = wr(recent_closes)

        # ── Current win/loss streak ────────────────────────────────────────────
        streak = 0
        for f in reversed(all_closes):
            pnl = float(f.get("closedPnl", 0))
            if pnl == 0:
                break
            if streak == 0:
                streak = 1 if pnl > 0 else -1
            elif streak > 0 and pnl > 0:
                streak += 1
            elif streak < 0 and pnl < 0:
                streak -= 1
            else:
                break

        # ── Avg hold time ──────────────────────────────────────────────────────
        hold_times      = []
        opens_by_coin: dict[str, float] = {}
        for fill in recent30:
            coin       = fill.get("coin", "")
            direction  = str(fill.get("dir", ""))
            ts         = float(fill.get("time", 0))
            closed_pnl = float(fill.get("closedPnl", 0))

            is_open  = "Open"  in direction or (closed_pnl == 0 and "Close" not in direction)
            is_close = "Close" in direction or closed_pnl != 0

            if is_open and not is_close:
                opens_by_coin[coin] = ts
            elif is_close and coin in opens_by_coin:
                hold_h = (ts - opens_by_coin.pop(coin)) / 3_600_000
                if 0 < hold_h < 168:   # sanity: 0–7 days
                    hold_times.append(hold_h)

        avg_hold_h = sum(hold_times) / len(hold_times) if hold_times else 99.0

        # ── Average notional (their conviction per trade) ──────────────────────
        notionals = [float(f.get("sz", 0)) * float(f.get("px", 0)) for f in recent30]
        notionals = [n for n in notionals if n > 0]
        avg_notional = sum(notionals) / len(notionals) if notionals else 0

        # ── Recency ────────────────────────────────────────────────────────────
        last_ts = float(fills[-1].get("time", 0))
        last_trade_h = (now_ms - last_ts) / 3_600_000

        return {
            "trades_per_day":  trades_per_day,
            "avg_hold_h":      avg_hold_h,
            "win_rate_all":    win_rate_all,
            "win_rate_recent": win_rate_recent,
            "current_streak":  streak,
            "trades_7d":       len(last7d),
            "last_trade_h":    last_trade_h,
            "avg_notional":    avg_notional,
            "total_fills":     len(fills),
            "closes_found":    len(all_closes),
        }

    except Exception:
        return None


def composite_score(t: dict) -> float:
    """
    Copy-trading suitability score (0–1+ range).

    Weights:
      30% frequency     — need steady signal flow
      25% recent WR     — edge RIGHT NOW, not historical luck
      20% win streak    — hot hand / current form
      15% hold time     — shorter = cleaner copy, less slippage
      10% realized PnL  — proof the edge is real and large
    """
    freq   = t.get("trades_per_day", 0)
    wr     = t.get("win_rate_recent", 0.5)
    streak = t.get("current_streak", 0)
    hold   = max(t.get("avg_hold_h", 99), 0.1)
    pnl    = t.get("alltime_pnl", 0)
    last_h = t.get("last_trade_h", 999)

    # Frequency: 10+/day = 1.0, 5/day = 0.5, 1/day = 0.1
    freq_score   = min(freq / 10.0, 1.0)

    # Recent WR: 50% = 0, 65% = 0.5, 80% = 1.0
    wr_score     = max(0, min((wr - 0.50) / 0.30, 1.0))

    # Streak: +10 = 1.0, +5 = 0.5, 0 = 0, negative = penalty
    streak_score = max(-0.5, min(streak / 10.0, 1.0))

    # Hold: 1h = 0.92, 4h = 0.67, 12h = 0, >12h = negative
    hold_score   = max(0, 1.0 - hold / 12.0)

    # PnL: $500K = 0.5, $1M = 1.0
    pnl_score    = min(pnl / 1_000_000, 1.0)

    # Recency bonus/penalty
    if last_h < 6:
        recency = +0.10
    elif last_h < 24:
        recency = 0.0
    elif last_h < 72:
        recency = -0.10
    else:
        recency = -0.30

    return (
        freq_score   * 0.30
        + wr_score   * 0.25
        + streak_score * 0.20
        + hold_score * 0.15
        + pnl_score  * 0.10
        + recency
    )


def passes_stage2(p: dict) -> bool:
    return (
        p["trades_per_day"]  >= MIN_TRADES_PER_DAY
        and p["avg_hold_h"]  <= MAX_AVG_HOLD_HOURS
        and p["win_rate_recent"] >= MIN_WIN_RATE_RECENT
        and p["trades_7d"]   >= MIN_TRADES_7D
        and p["last_trade_h"] < 72   # active in last 3 days
    )


def print_table(traders: list[dict], title: str):
    print(f"\n{'═'*108}")
    print(f"  {title}")
    print(f"{'═'*108}")
    print(f"  {'#':<3} {'Address':<14} {'PnL':>10} {'WR-rec':>7} {'WR-all':>7} "
          f"{'T/day':>6} {'Hold':>7} {'Streak':>7} {'7d':>5} {'Active':>9} {'Score':>7}")
    print(f"  {'-'*103}")
    for i, t in enumerate(traders, 1):
        streak_s = f"+{t['current_streak']}" if t['current_streak'] > 0 else str(t['current_streak'])
        last_s   = f"{t['last_trade_h']:.0f}h ago"
        hold_s   = f"{t['avg_hold_h']:.1f}h" if t['avg_hold_h'] < 99 else "  ?"
        print(
            f"  {i:<3} {t['address'][:12]+'…':<14} "
            f"${t['alltime_pnl']:>9,.0f} "
            f"{t['win_rate_recent']:>7.1%} "
            f"{t['win_rate_all']:>7.1%} "
            f"{t['trades_per_day']:>6.1f} "
            f"{hold_s:>7} "
            f"{streak_s:>7} "
            f"{t['trades_7d']:>5} "
            f"{last_s:>9} "
            f"{t['score']:>7.4f}"
        )


async def main():
    print("═" * 65)
    print("  HL-BOT TRADER SCANNER v2")
    print("  Scanning for: high frequency + strong recent WR + hot streak")
    print("═" * 65)

    # ── Stage 1: fetch leaderboard ─────────────────────────────────────────────
    print("\nSTAGE 1: Fetching leaderboard…")
    async with aiohttp.ClientSession() as s:
        async with s.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json(content_type=None)

    rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
    print(f"  Total traders: {len(rows):,}")

    parsed = []
    for entry in rows:
        addr = entry.get("ethAddress", "")
        if not addr or len(addr) != 42:
            continue
        w = parse_windows(entry.get("windowPerformances", []))
        parsed.append({
            "address":     addr,
            "alltime_pnl": w.get("allTime", {}).get("pnl", 0),
            "week_pnl":    w.get("week",    {}).get("pnl", 0),
            "day_vlm":     w.get("day",     {}).get("vlm", 0),
            "alltime_vlm": w.get("allTime", {}).get("vlm", 0),
            "alltime_wr":  w.get("allTime", {}).get("wr", 0.5),
        })

    s1 = [
        t for t in parsed
        if t["alltime_pnl"] >= MIN_ALLTIME_PNL
        and t["week_pnl"]   >= MIN_WEEK_PNL
        and t["day_vlm"]    >= MIN_DAY_VLM
        and t["alltime_vlm"] >= MIN_ALLTIME_VLM
    ]
    s1.sort(key=lambda t: t["alltime_pnl"], reverse=True)
    candidates = s1[:STAGE1_LIMIT]
    print(f"  Stage 1 filter: {len(s1):,} qualify → deep-scanning top {len(candidates)}")

    # ── Stage 2: fill history analysis ────────────────────────────────────────
    print(f"\nSTAGE 2: Profiling {len(candidates)} traders (fill history + streaks)…")
    print("  This takes ~2 minutes…\n")

    results = []
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(candidates), 15):
            batch = candidates[i:i+15]
            profiles = await asyncio.gather(*[
                profile_trader(session, t["address"]) for t in batch
            ])
            for trader, profile in zip(batch, profiles):
                if profile is None:
                    continue
                merged = {**trader, **profile}
                merged["score"] = composite_score(merged)
                if passes_stage2(profile):
                    results.append(merged)
                    flag = "✅"
                else:
                    flag = "  "
                streak_s = f"+{profile['current_streak']}" if profile['current_streak'] > 0 else str(profile['current_streak'])
                print(
                    f"  {flag} {trader['address'][:14]}… "
                    f"WR={profile['win_rate_recent']:.0%} "
                    f"T/d={profile['trades_per_day']:.1f} "
                    f"hold={profile['avg_hold_h']:.1f}h "
                    f"streak={streak_s} "
                    f"score={merged['score']:.4f}"
                )
            await asyncio.sleep(0.3)

    # ── Rank and output ────────────────────────────────────────────────────────
    results.sort(key=lambda t: t["score"], reverse=True)
    top = results[:TOP_N]

    print_table(top, f"🏆 TOP {len(top)} COPY-TRADING CANDIDATES")

    # Write full stats to JSON
    out = []
    for t in top:
        out.append({
            "address":         t["address"],
            "alltime_pnl":     round(t["alltime_pnl"]),
            "win_rate_recent": round(t["win_rate_recent"], 4),
            "win_rate_all":    round(t["win_rate_all"], 4),
            "trades_per_day":  round(t["trades_per_day"], 2),
            "avg_hold_h":      round(t["avg_hold_h"], 2),
            "current_streak":  t["current_streak"],
            "trades_7d":       t["trades_7d"],
            "last_trade_h":    round(t["last_trade_h"], 1),
            "avg_notional":    round(t.get("avg_notional", 0)),
            "score":           round(t["score"], 5),
        })

    import os
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n✅ Wrote {len(top)} traders to {OUTPUT_FILE}")

    if top:
        print("\n📋 Suggested whitelist (top 5):")
        for t in top[:5]:
            streak_s = f"+{t['current_streak']}" if t['current_streak'] > 0 else str(t['current_streak'])
            print(
                f"   {t['address']}  "
                f"WR={t['win_rate_recent']:.0%}  "
                f"T/d={t['trades_per_day']:.1f}  "
                f"hold={t['avg_hold_h']:.1f}h  "
                f"streak={streak_s}  "
                f"PnL=${t['alltime_pnl']:,.0f}"
            )
        whitelist = ",".join(t["address"] for t in top[:5])
        print(f"\n.env line:")
        print(f"  COPY_TRADER_WHITELIST={whitelist}")
        print(f"  STRATEGY_LEADERBOARD_COPY=true")
        print("\nThen: sudo systemctl restart hl-bot")


if __name__ == "__main__":
    asyncio.run(main())
