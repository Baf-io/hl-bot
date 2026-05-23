"""
Auto-discover top traders from Hyperliquid's stats API.
Writes results to data/traders.json which the bot reads on startup.

Run: python src/find_traders.py
Then restart: python src/main.py
"""
import asyncio, sys, json
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import aiohttp

STATS_URL    = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OUTPUT_FILE  = "data/traders.json"

# ── Filters ────────────────────────────────────────────────────────────────────
MIN_ALLTIME_PNL  = 100_000    # $100k+ all-time PnL
MIN_WEEK_PNL     =       0    # profitable this week
MIN_DAY_VLM      =  10_000    # traded at least $10k today
MIN_ALLTIME_VLM  = 500_000    # serious volume
TOP_N            =     150    # subscribe to top 150 (sweet spot for WS load)


def parse_windows(perfs: list) -> dict:
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


async def main():
    print("Fetching leaderboard from stats-data.hyperliquid.xyz …")
    async with aiohttp.ClientSession() as s:
        async with s.get(STATS_URL, timeout=aiohttp.ClientTimeout(total=60)) as r:
            data = await r.json(content_type=None)

    rows = data.get("leaderboardRows", []) if isinstance(data, dict) else data
    print(f"Total rows: {len(rows):,}")

    parsed = []
    for entry in rows:
        addr = entry.get("ethAddress", "")
        if not addr or len(addr) != 42:
            continue
        w       = parse_windows(entry.get("windowPerformances", []))
        alltime = w.get("allTime", {})
        week    = w.get("week",    {})
        day     = w.get("day",     {})
        parsed.append({
            "address":     addr,
            "alltime_pnl": alltime.get("pnl", 0),
            "alltime_roi": alltime.get("roi", 0),
            "alltime_vlm": alltime.get("vlm", 0),
            "week_pnl":    week.get("pnl", 0),
            "day_vlm":     day.get("vlm", 0),
        })

    filtered = [
        t for t in parsed
        if t["alltime_pnl"] >= MIN_ALLTIME_PNL
        and t["week_pnl"]   >= MIN_WEEK_PNL
        and t["day_vlm"]    >= MIN_DAY_VLM
        and t["alltime_vlm"] >= MIN_ALLTIME_VLM
    ]
    filtered.sort(key=lambda t: t["alltime_pnl"], reverse=True)
    top = filtered[:TOP_N]

    print(f"Filtered: {len(filtered):,} → keeping top {len(top)}")
    print(f"\n{'#':<4} {'Address':<14} {'AllTime PnL':>14} {'Week PnL':>12} {'Day VLM':>12}")
    print("-"*60)
    for i, t in enumerate(top[:20], 1):  # preview top 20
        print(f"{i:<4} {t['address'][:12]}…  ${t['alltime_pnl']:>12,.0f}  ${t['week_pnl']:>10,.0f}  ${t['day_vlm']:>10,.0f}")
    if len(top) > 20:
        print(f"  … and {len(top)-20} more")

    # Write to JSON
    out = [{"address": t["address"], "alltime_pnl": t["alltime_pnl"]} for t in top]
    with open(OUTPUT_FILE, "w") as f:
        json.dump(out, f, indent=2)

    print(f"\n✅ Wrote {len(top)} traders to {OUTPUT_FILE}")
    print("Now restart the bot: python src/main.py")


if __name__ == "__main__":
    asyncio.run(main())
