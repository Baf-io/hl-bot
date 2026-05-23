"""
Auto-discover top traders from Hyperliquid's stats API.
Filters by PnL, win rate, trade frequency, and recency.

Run on VPS: python src/find_traders.py
Outputs ready-to-paste KNOWN_TRADERS list.
"""
import asyncio
import sys, os
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import aiohttp
from datetime import datetime, timezone

STATS_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HL_REST   = "https://api.hyperliquid.xyz/info"

# ── Filter thresholds ──────────────────────────────────────────────────────────
MIN_PNL_USD        = 50_000      # minimum all-time realized PnL
MIN_WIN_RATE       = 0.50        # minimum win rate
MIN_TRADE_COUNT    = 100         # minimum trades ever
MAX_LAST_TRADE_H   = 48          # must have traded within 48 hours
TOP_N              = 50          # how many to return


async def fetch_leaderboard() -> list[dict]:
    print("Fetching leaderboard from stats-data.hyperliquid.xyz …")
    async with aiohttp.ClientSession() as session:
        async with session.get(
            STATS_URL,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status != 200:
                print(f"HTTP {resp.status} — trying fallback")
                return []
            data = await resp.json(content_type=None)
            print(f"Got {len(data)} traders from API")
            return data if isinstance(data, list) else data.get("leaderboardRows", [])


async def get_last_trade_hours(session, address: str) -> float:
    """Returns hours since last trade, or 9999 if no fills."""
    try:
        async with session.post(
            HL_REST,
            json={"type": "userFills", "user": address},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            fills = await resp.json(content_type=None)
        if not fills:
            return 9999
        last_ts = fills[-1]["time"] / 1000
        age_h = (datetime.now(timezone.utc).timestamp() - last_ts) / 3600
        return age_h
    except Exception:
        return 9999


async def main():
    raw = await fetch_leaderboard()
    if not raw:
        print("No data returned. Trying alternative parse…")
        return

    # ── Parse each entry ───────────────────────────────────────────────────────
    parsed = []
    for entry in raw:
        try:
            # Handle different schema versions
            perfs = entry.get("windowPerformances", [])
            stats = {}
            for item in perfs:
                if isinstance(item, list) and len(item) >= 2:
                    if item[0] == "allTime":
                        stats = item[1]
                        break
            if not stats and perfs:
                stats = perfs[-1][1] if isinstance(perfs[-1], list) else {}

            pnl    = float(stats.get("pnl", entry.get("pnl", 0)))
            wr     = float(stats.get("winRate", entry.get("winRate", 0)))
            trades = int(stats.get("tradeCount", entry.get("tradeCount", 0)))
            addr   = entry.get("ethAddress", entry.get("user", ""))

            if not addr or len(addr) != 42:
                continue

            parsed.append({
                "address": addr,
                "pnl": pnl,
                "win_rate": wr,
                "trade_count": trades,
            })
        except Exception:
            continue

    print(f"Parsed {len(parsed)} valid entries")

    # ── Apply filters ──────────────────────────────────────────────────────────
    filtered = [
        t for t in parsed
        if t["pnl"]         >= MIN_PNL_USD
        and t["win_rate"]   >= MIN_WIN_RATE
        and t["trade_count"] >= MIN_TRADE_COUNT
    ]
    print(f"After PnL/WR/trades filter: {len(filtered)} traders")

    # Sort by PnL descending
    filtered.sort(key=lambda t: t["pnl"], reverse=True)
    candidates = filtered[:200]  # check top 200 for recency

    # ── Check last trade time (parallel) ──────────────────────────────────────
    print(f"Checking last trade time for {len(candidates)} traders …")
    async with aiohttp.ClientSession() as session:
        tasks = [get_last_trade_hours(session, t["address"]) for t in candidates]
        ages  = await asyncio.gather(*tasks)

    results = []
    for trader, age_h in zip(candidates, ages):
        trader["last_trade_h"] = age_h
        if age_h <= MAX_LAST_TRADE_H:
            results.append(trader)

    results.sort(key=lambda t: t["pnl"], reverse=True)
    top = results[:TOP_N]

    # ── Output ─────────────────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"TOP {len(top)} ACTIVE TRADERS (last {MAX_LAST_TRADE_H}h, PnL > ${MIN_PNL_USD:,})")
    print(f"{'='*70}\n")

    print("# Paste this into src/signals/leaderboard_copy.py KNOWN_TRADERS list:")
    print("KNOWN_TRADERS = [")
    for t in top:
        icon = "🔥" if t["last_trade_h"] < 1 else "✅" if t["last_trade_h"] < 6 else "⏰"
        print(
            f'    # {icon} PnL=${t["pnl"]:>12,.0f} | WR={t["win_rate"]:.0%} '
            f'| trades={t["trade_count"]:>5} | last={t["last_trade_h"]:.1f}h ago'
        )
        print(
            f'    ("{t["address"]}", '
            f'{int(t["pnl"])}, '
            f'{t["win_rate"]:.2f}, '
            f'0.20, 10, '
            f'{t["trade_count"]}, 60),'
        )
    print("]")

    print(f"\n{'='*70}")
    print(f"Total qualifying traders found: {len(results)}")
    print(f"Showing top {len(top)} by all-time PnL")


if __name__ == "__main__":
    asyncio.run(main())
