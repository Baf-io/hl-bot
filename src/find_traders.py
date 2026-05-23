"""
Auto-discover top traders from Hyperliquid's stats API.
Schema: windowPerformances = [["day",{pnl,roi,vlm}], ["week",...], ["month",...], ["allTime",...]]

Run: python src/find_traders.py
"""
import asyncio, sys, json
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))
import aiohttp

STATS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# ── Filters ────────────────────────────────────────────────────────────────────
MIN_ALLTIME_PNL   = 100_000     # $100k+ all-time PnL
MIN_WEEK_PNL      =       0     # profitable this week
MIN_DAY_VLM       =  10_000     # traded at least $10k today (active right now)
MIN_ALLTIME_VLM   = 500_000     # serious trader, not one-off
TOP_N             =      75     # return top 75


def parse_windows(perfs: list) -> dict:
    """Extract stats per window into a flat dict."""
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
    print(f"Total rows in API: {len(rows):,}")

    # ── Parse ──────────────────────────────────────────────────────────────────
    parsed = []
    for entry in rows:
        addr = entry.get("ethAddress", "")
        if not addr or len(addr) != 42:
            continue

        w = parse_windows(entry.get("windowPerformances", []))
        alltime = w.get("allTime", {})
        week    = w.get("week",    {})
        month   = w.get("month",   {})
        day     = w.get("day",     {})

        parsed.append({
            "address":      addr,
            "display":      entry.get("displayName") or addr[:10] + "…",
            "account_val":  float(entry.get("accountValue", 0)),
            "alltime_pnl":  alltime.get("pnl", 0),
            "alltime_roi":  alltime.get("roi", 0),
            "alltime_vlm":  alltime.get("vlm", 0),
            "month_pnl":    month.get("pnl", 0),
            "month_vlm":    month.get("vlm", 0),
            "week_pnl":     week.get("pnl", 0),
            "week_vlm":     week.get("vlm", 0),
            "day_pnl":      day.get("pnl", 0),
            "day_vlm":      day.get("vlm", 0),
        })

    print(f"Parsed: {len(parsed):,} valid addresses")

    # ── Filter ─────────────────────────────────────────────────────────────────
    filtered = [
        t for t in parsed
        if t["alltime_pnl"] >= MIN_ALLTIME_PNL
        and t["week_pnl"]   >= MIN_WEEK_PNL
        and t["day_vlm"]    >= MIN_DAY_VLM
        and t["alltime_vlm"] >= MIN_ALLTIME_VLM
    ]
    print(f"After filter (alltime PnL>${MIN_ALLTIME_PNL:,} + active today): {len(filtered):,}")

    # Sort by all-time PnL
    filtered.sort(key=lambda t: t["alltime_pnl"], reverse=True)
    top = filtered[:TOP_N]

    # ── Print summary ──────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"TOP {len(top)} ACTIVE PROFITABLE TRADERS")
    print(f"{'='*80}")
    print(f"{'#':<4} {'Address':<14} {'AllTime PnL':>14} {'Week PnL':>12} {'Day VLM':>12} {'AllTime ROI':>12}")
    print("-"*80)
    for i, t in enumerate(top, 1):
        print(
            f"{i:<4} {t['address'][:12]}… "
            f"${t['alltime_pnl']:>13,.0f} "
            f"${t['week_pnl']:>11,.0f} "
            f"${t['day_vlm']:>11,.0f} "
            f"{t['alltime_roi']:>11.1%}"
        )

    # ── Ready-to-paste output ──────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print("# PASTE INTO src/signals/leaderboard_copy.py:")
    print("KNOWN_TRADERS = [")
    for t in top:
        print(
            f'    ("{t["address"]}", '
            f'{int(t["alltime_pnl"])}, '
            f'0.60, 0.20, 10, 500, 60),  '
            f'# AllTime=${t["alltime_pnl"]:,.0f} | Week=${t["week_pnl"]:,.0f} | DayVlm=${t["day_vlm"]:,.0f}'
        )
    print("]")
    print(f"\nTotal traders found passing filter: {len(filtered)}")


if __name__ == "__main__":
    asyncio.run(main())
