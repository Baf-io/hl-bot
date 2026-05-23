"""
Stress test: fetch recent fills for all tracked traders via REST.
Confirms WebSocket subscriptions are working and shows last trade time.

Run: python src/test_leaderboard.py
"""
import asyncio
import sys, os
import pathlib as _pl
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))
sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))

import aiohttp
from datetime import datetime, timezone
from config import settings

HL_REST = (
    "https://api.hyperliquid-testnet.xyz/info"
    if settings.HL_TESTNET
    else "https://api.hyperliquid.xyz/info"
)

TRADERS = [
    ("0x31ca8395cf837de08b24da3f660e77761dfb974b", "Rank1 $114M"),
    ("0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00", "Rank3 $25M"),
    ("0x7fdafde5cfb5465924316eced2d3715494c517d1", "Rank4 $22M +$9M PnL"),
    ("0xfc667adba8d4837586078f4fdcdc29804337ca06", "Rank5 $20M"),
    ("0x31dea2516beee92135b96f464eeec3cf292a13f2", "Rank6 $13M"),
    ("0x023a3d058020fb76cca98f01b3c48c8938a22355", "Rank7 $11M 76pos"),
    ("0x57dd78cd36e76e2011e8f6dc25cabbaba994494b", "Rank8 $11M 150pos"),
    ("0x7717a7a245d9f950e586822b8c9b46863ed7bd7e", "Rank10 $4M 176pos"),
    ("0x9e8b1e51c642f4c8b87c6ba11c53d516a218afc4", "Rank11 $4M +$397K"),
    ("0x61ceef212ff4a86933c69fb6aca2fe35d8f2a62b", "Rank13 $2.6M"),
    ("0x7c930969fcf3e5a5c78bcf2e1cefda3f53e3c8fd", "Rank15 $2M 102pos"),
    ("0xa6ee1ed1ae80b8352603654b39f5e7b9bedd5078", "Rank18 $1.2M"),
    ("0xf517639a8872e756ac98d3c65507d2ebc25cc032", "Rank20 $827K +$1.12M"),
    ("0x7839e2f2c375dd2935193f2736167514efff9916", "Rank21 $607K"),
    ("0xcab59c7a92b8f7c4d5cde72bb7669ee7d75b6e6e", "Rank23 $451K +$94K"),
    ("0xc926ddba8b7617dbc65712f20cf8e1b58b8598d3", "Rank24 $430K 83pos"),
    ("0x535e34b5ada64997afc88444271ae9b3f82b3867", "Rank26 $182K"),
    ("0x1c1c270b573d55b68b3d14722b5d5d401511bed0", "Rank29 $110K"),
    ("0x53babe76166eae33c861aeddf9ce89af20311cd0", "Rank31 $67K 10pos"),
]


async def check_trader(session, address, label):
    try:
        async with session.post(
            HL_REST,
            json={"type": "userFills", "user": address},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            fills = await resp.json(content_type=None)

        if not fills:
            print(f"  ⚪ {label[:30]:<30} {address[:12]}… | NO FILLS EVER")
            return

        last = fills[-1]
        ts = datetime.fromtimestamp(last["time"] / 1000, tz=timezone.utc)
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
        coin = last.get("coin", "?")
        side = last.get("dir", last.get("side", "?"))
        px   = last.get("px", "?")

        if age_h < 1:
            icon = "🔥"
        elif age_h < 24:
            icon = "✅"
        elif age_h < 72:
            icon = "⚠️ "
        else:
            icon = "❌"

        print(
            f"  {icon} {label[:30]:<30} {address[:12]}… | "
            f"last trade: {age_h:.1f}h ago | {side} {coin} @ {px}"
        )

    except Exception as e:
        print(f"  ❌ {label[:30]:<30} {address[:12]}… | ERROR: {e}")


async def main():
    print(f"\n{'='*70}")
    print(f"LEADERBOARD STRESS TEST — {len(TRADERS)} traders")
    print(f"Network: {'TESTNET' if settings.HL_TESTNET else 'MAINNET'}")
    print(f"{'='*70}")
    print("🔥 < 1h ago  ✅ < 24h  ⚠️  < 72h  ❌ inactive\n")

    async with aiohttp.ClientSession() as session:
        tasks = [check_trader(session, addr, label) for addr, label in TRADERS]
        await asyncio.gather(*tasks)

    print(f"\n{'='*70}")
    print("Any 🔥 traders = active right now, fills incoming via WebSocket")
    print("Any ❌ traders = dead accounts, remove from list")


if __name__ == "__main__":
    asyncio.run(main())
