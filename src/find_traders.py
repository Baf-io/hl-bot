"""
Auto-discover + profile top traders from Hyperliquid's stats API.
Profiles each trader's fill history to find high-frequency profitable ones.

Filters for:
  - High frequency: 5+ trades/day average
  - Short holds: avg hold time < 2h
  - Profitable: win rate > 55%, positive weekly PnL
  - Active: traded today

Run: python src/find_traders.py
Writes: data/traders.json
Then restart bot: python src/main.py
"""
import asyncio, sys, json, time
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import aiohttp
from datetime import datetime, timezone

STATS_URL   = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_REST     = "https://api.hyperliquid.xyz/info"
OUTPUT_FILE = "data/traders.json"

# ── Stage 1 filters (from leaderboard stats) ──────────────────────────────────
MIN_ALLTIME_PNL  = 50_000
MIN_WEEK_PNL     = 0
MIN_DAY_VLM      = 5_000       # traded at least $5k today
MIN_ALLTIME_VLM  = 100_000
STAGE1_LIMIT     = 300         # profile top 300 candidates

# ── Stage 2 filters (from fill history profiling) ────────────────────────────
MIN_TRADES_PER_DAY   = 3.0     # at least 3 trades/day average
MAX_AVG_HOLD_HOURS   = 3.0     # average hold under 3h
MIN_WIN_RATE         = 0.52    # at least 52% profitable trades
MIN_RECENT_TRADES_7D = 10      # must have 10+ trades in last 7 days
TOP_N                = 75      # final output size


def parse_windows(perfs):
    out = {}
    for item in perfs:
        if isinstance(item, list) and len(item) == 2:
            window, stats = item
            out[window] = {
                "pnl": float(stats.get("pnl", 0)),
                "roi": float(stats.get("roi", 0)),
                "vlm": float(stats.get("vlm", 0)),
            }
    return out


async def profile_trader(session, address: str) -> dict | None:
    """
    Fetch fill history and compute:
    - trades per day (last 30d)
    - avg hold time in hours
    - win rate
    - recent activity (last 7d trade count)
    """
    try:
        async with session.post(
            HL_REST,
            json={"type": "userFills", "user": address},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            fills = await resp.json(content_type=None)

        if not fills or len(fills) < 5:
            return None

        now_ms = time.time() * 1000
        day_ms = 86_400_000
        week_ms = 7 * day_ms
        month_ms = 30 * day_ms

        # Filter to last 30 days
        recent = [f for f in fills if now_ms - f.get("time", 0) < month_ms]
        last7d = [f for f in fills if now_ms - f.get("time", 0) < week_ms]

        if len(recent) < 5:
            return None

        # Trades per day (last 30d)
        trades_per_day = len(recent) / 30

        # Win rate from closedPnl
        closes = [f for f in recent if float(f.get("closedPnl", 0)) != 0]
        if len(closes) < 3:
            win_rate = 0.5  # not enough data
        else:
            wins = sum(1 for f in closes if float(f.get("closedPnl", 0)) > 0)
            win_rate = wins / len(closes)

        # Avg hold time — match opens to closes by coin
        hold_times = []
        opens_by_coin: dict[str, float] = {}
        for fill in sorted(recent, key=lambda f: f.get("time", 0)):
            coin = fill.get("coin", "")
            direction = fill.get("dir", "")
            ts = fill.get("time", 0)
            closed_pnl = float(fill.get("closedPnl", 0))

            if "Open" in direction or closed_pnl == 0:
                opens_by_coin[coin] = ts
            elif "Close" in direction or closed_pnl != 0:
                if coin in opens_by_coin:
                    hold_ms = ts - opens_by_coin.pop(coin)
                    hold_h = hold_ms / 3_600_000
                    if 0 < hold_h < 48:  # sanity filter
                        hold_times.append(hold_h)

        avg_hold_h = sum(hold_times) / len(hold_times) if hold_times else 99

        return {
            "trades_per_day": trades_per_day,
            "avg_hold_h":     avg_hold_h,
            "win_rate":       win_rate,
            "trades_7d":      len(last7d),
            "total_fills":    len(fills),
        }

    except Exception as e:
        return None


async def main():
    print("=" * 65)
    print("STAGE 1: Fetch leaderboard …")
    print("=" * 65)

    async with aiohttp.ClientSession() as s:
        async with s.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json(content_type=None)

    rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
    print(f"Total traders in API: {len(rows):,}")

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
        })

    # Stage 1 filter
    s1 = [
        t for t in parsed
        if t["alltime_pnl"] >= MIN_ALLTIME_PNL
        and t["week_pnl"]   >= MIN_WEEK_PNL
        and t["day_vlm"]    >= MIN_DAY_VLM
        and t["alltime_vlm"] >= MIN_ALLTIME_VLM
    ]
    s1.sort(key=lambda t: t["alltime_pnl"], reverse=True)
    candidates = s1[:STAGE1_LIMIT]
    print(f"Stage 1 passed: {len(s1):,} → profiling top {len(candidates)}")

    print(f"\nSTAGE 2: Profiling {len(candidates)} traders (fill history) …")
    print("This takes ~60 seconds …\n")

    # Profile in batches of 20 to avoid rate limits
    results = []
    async with aiohttp.ClientSession() as session:
        for i in range(0, len(candidates), 20):
            batch = candidates[i:i+20]
            profiles = await asyncio.gather(*[
                profile_trader(session, t["address"]) for t in batch
            ])
            for trader, profile in zip(batch, profiles):
                if profile is None:
                    continue
                # Stage 2 filter
                if (profile["trades_per_day"] >= MIN_TRADES_PER_DAY
                    and profile["avg_hold_h"]  <= MAX_AVG_HOLD_HOURS
                    and profile["win_rate"]     >= MIN_WIN_RATE
                    and profile["trades_7d"]    >= MIN_RECENT_TRADES_7D):
                    results.append({**trader, **profile})

            done = min(i + 20, len(candidates))
            print(f"  Profiled {done}/{len(candidates)} | qualifying so far: {len(results)}")
            await asyncio.sleep(0.5)   # gentle rate limiting

    # Sort by composite score: trades/day * win_rate / avg_hold
    for r in results:
        r["score"] = (r["trades_per_day"] * r["win_rate"]) / max(r["avg_hold_h"], 0.1)

    results.sort(key=lambda t: t["score"], reverse=True)
    top = results[:TOP_N]

    # ── Print results ──────────────────────────────────────────────────────────
    print(f"\n{'='*75}")
    print(f"TOP {len(top)} HIGH-FREQUENCY PROFITABLE TRADERS")
    print(f"{'='*75}")
    print(f"{'#':<4} {'Address':<14} {'Trades/day':>11} {'Avg hold':>10} {'Win rate':>10} {'7d trades':>10} {'Score':>8}")
    print("-" * 75)
    for i, t in enumerate(top, 1):
        print(
            f"{i:<4} {t['address'][:12]}…  "
            f"{t['trades_per_day']:>10.1f}  "
            f"{t['avg_hold_h']:>8.1f}h  "
            f"{t['win_rate']:>9.0%}  "
            f"{t['trades_7d']:>9}  "
            f"{t['score']:>7.2f}"
        )

    # Write to JSON
    out = [{"address": t["address"], "alltime_pnl": t["alltime_pnl"], "score": t["score"]} for t in top]
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n✅ Wrote {len(top)} high-frequency traders to {OUTPUT_FILE}")
    print(f"Total passing all filters: {len(results)}")
    print("\nRestart bot: python src/main.py")


if __name__ == "__main__":
    asyncio.run(main())
