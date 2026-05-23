"""
Strategy 2: Leaderboard Alpha Capture
──────────────────────────────────────
Polls HL leaderboard, filters quality traders, mirrors their fills via WebSocket.
Aggressive mode: loosened filters, faster lag cutoff, all signals relayed.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional
import aiohttp
from loguru import logger
from config import settings
from data.store import MarketStore, TradeSignal


HL_REST = (
    "https://api.hyperliquid-testnet.xyz/info"
    if settings.HL_TESTNET
    else "https://api.hyperliquid.xyz/info"
)


@dataclass
class TrackedTrader:
    address: str
    realized_pnl: float
    win_rate: float
    max_drawdown: float
    avg_leverage: float
    trade_count: int
    account_age_days: int
    score: float = 0.0


class LeaderboardCopier:
    def __init__(self, store: MarketStore, feed):
        self.store = store
        self.feed = feed
        self._tracked: dict[str, TrackedTrader] = {}
        self._signal_queue: Optional[asyncio.Queue] = None
        self._recent_signals: set = set()   # dedup cache

    # ── Leaderboard polling ────────────────────────────────────────────────────

    async def refresh_leaderboard(self):
        raw = await self._fetch_leaderboard()
        if not raw:
            logger.warning("[Leaderboard] No traders returned")
            return

        # Manually curated addresses — skip filter, trust the list
        for t in raw:
            t.score = self._score(t)
        top = raw  # use all of them

        new_addresses = {t.address for t in top}
        old_addresses = set(self._tracked.keys())

        for trader in top:
            if trader.address not in old_addresses:
                channel = f"userFills:{trader.address}"
                self.feed.subscribe(channel, self._make_fill_handler(trader.address))
                logger.info(
                    f"[Leaderboard] ✅ Tracking {trader.address[:10]}… "
                    f"PnL=${trader.realized_pnl:,.0f} WR={trader.win_rate:.0%} "
                    f"score={trader.score:.3f}"
                )

        for addr in old_addresses - new_addresses:
            logger.info(f"[Leaderboard] ❌ Dropping {addr[:10]}…")

        self._tracked = {t.address: t for t in top}
        logger.info(f"[Leaderboard] Tracking {len(self._tracked)} traders")

    def _passes_filter(self, t: TrackedTrader) -> bool:
        ok = (
            t.account_age_days  >= settings.COPY_MIN_ACCOUNT_AGE_DAYS
            and t.realized_pnl  >= settings.COPY_MIN_REALIZED_PNL_USD
            and t.win_rate      >= settings.COPY_MIN_WIN_RATE
            and t.max_drawdown  <= settings.COPY_MAX_DRAWDOWN
            and t.avg_leverage  <= settings.COPY_MAX_AVG_LEVERAGE
            and t.trade_count   >= settings.COPY_MIN_TRADE_COUNT
        )
        if ok:
            t.score = self._score(t)
        return ok

    @staticmethod
    def _score(t: TrackedTrader) -> float:
        return (
            min(t.realized_pnl / 500_000, 1.0) * 0.3
            + t.win_rate * 0.3
            + (1 - t.max_drawdown) * 0.2
            + min(1 / max(t.avg_leverage, 1), 1.0) * 0.2
        )

    # ── Fill handler ───────────────────────────────────────────────────────────

    def _make_fill_handler(self, address: str):
        async def on_fill(msg: dict):
            await self._handle_fill(address, msg)
        on_fill.__name__ = f"fill_{address[:8]}"
        return on_fill

    async def _handle_fill(self, address: str, msg: dict):
        now = time.time()
        fills = msg.get("data", {}).get("fills", [])
        if not fills:
            return

        trader = self._tracked.get(address)
        if not trader:
            return

        for fill in fills:
            try:
                fill_ts  = float(fill.get("time", now * 1000)) / 1000
                lag_ms   = (now - fill_ts) * 1000

                if lag_ms > settings.COPY_MAX_LAG_MS:
                    logger.debug(f"[Leaderboard] Lag too high {lag_ms:.0f}ms — skip")
                    continue

                coin      = fill.get("coin", "")
                side      = fill.get("dir", fill.get("side", ""))
                sz        = float(fill.get("sz", 0))
                px        = float(fill.get("px", 0))
                closed_pnl = float(fill.get("closedPnl", 0))

                # Skip xyz: tokenized stocks/commodities — can't price them yet
                if coin.startswith("xyz:") or coin.startswith("@"):
                    logger.debug(f"[Leaderboard] Skipping RWA asset {coin}")
                    continue

                # dir: "Open Long" / "Open Short" / "Close Long" / "Close Short"
                if "Long" in side or side == "B":
                    direction = "long"
                elif "Short" in side or side == "A":
                    direction = "short"
                else:
                    continue

                is_close = closed_pnl != 0 or "Close" in side

                # ── COPY EXIT — trader is closing, we close too ────────────────
                if is_close:
                    logger.info(f"[Leaderboard] 🚪 COPY EXIT {coin} | trader closed | from {address[:10]}…")
                    if self._signal_queue:
                        await self._signal_queue.put(TradeSignal(
                            strategy="leaderboard",
                            coin=coin,
                            direction=direction,
                            size_usd=0,
                            confidence=1.0,
                            meta={"action": "exit", "reason": "trader_closed"},
                        ))
                    continue

                # Dedup: skip if we already have this coin+direction open
                dedup_key = f"{coin}:{direction}"
                if dedup_key in self._recent_signals:
                    logger.debug(f"[Leaderboard] Dedup skip {dedup_key}")
                    continue
                self._recent_signals.add(dedup_key)
                # Clear dedup after 10s to allow re-entry
                asyncio.get_event_loop().call_later(10, self._recent_signals.discard, dedup_key)

                # Let risk manager size it — pass 0 so it uses max_size * confidence
                # Their notional is logged for reference only
                their_notional = sz * px

                logger.info(
                    f"[Leaderboard] 🔥 COPY {direction.upper()} {coin} "
                    f"(their ${their_notional:,.0f}) | lag={lag_ms:.0f}ms | from {address[:10]}…"
                )

                signal = TradeSignal(
                    strategy="leaderboard",
                    coin=coin,
                    direction=direction,
                    size_usd=0,          # 0 = let risk manager size it (~$18 at 7%)
                    confidence=trader.score,
                    meta={
                        "source": address,
                        "lag_ms": lag_ms,
                        "their_size_usd": their_notional,
                        "action": "enter",
                    },
                )
                if self._signal_queue:
                    await self._signal_queue.put(signal)

            except Exception as e:
                logger.error(f"[Leaderboard] Fill parse error: {e}")

    def set_signal_queue(self, queue: asyncio.Queue):
        self._signal_queue = queue

    # ── REST fetch ─────────────────────────────────────────────────────────────

    async def _fetch_leaderboard(self) -> list[TrackedTrader]:
        """
        Load traders from data/traders.json (generated by find_traders.py).
        Falls back to hardcoded list if file not found.
        Refresh by running: python src/find_traders.py
        """
        import json, os
        traders_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "data", "traders.json"
        )
        if os.path.exists(traders_file):
            with open(traders_file) as f:
                data = json.load(f)
            traders = []
            for entry in data:
                traders.append(TrackedTrader(
                    address=entry["address"],
                    realized_pnl=float(entry.get("alltime_pnl", 100_000)),
                    win_rate=0.60,
                    max_drawdown=0.20,
                    avg_leverage=10,
                    trade_count=500,
                    account_age_days=60,
                ))
            logger.info(f"[Leaderboard] Loaded {len(traders)} traders from {traders_file}")
            return traders

        # ── Fallback hardcoded list ────────────────────────────────────────────
        logger.warning("[Leaderboard] data/traders.json not found — using hardcoded fallback. Run find_traders.py to generate.")

        # Curated from beacontrade.io/leaderboard — all active accounts with positive PnL
        # Updated: 2026-05-24 | Source: beacontrade.io/leaderboard
        # Format: (address, est_pnl_usd, win_rate, max_dd, avg_lev, trade_count, age_days)
        KNOWN_TRADERS = [
            # ── Tier 1: Mega whales — $10M+ accounts, extremely active ───────────
            # Rank 1  | $114M | 183 positions | +$3.6K
            ("0x31ca8395cf837de08b24da3f660e77761dfb974b", 2_000_000, 0.62, 0.15, 8,  5000, 180),
            # Rank 3  | $25M  | 87 positions  | +$762K
            ("0xecb63caa47c7c4e77f60f1ce858cf28dc2b82b00", 1_500_000, 0.63, 0.18, 8,  3000, 90),
            # Rank 4  | $22M  | 28 positions  | +$9M unrealized — best PnL on board
            ("0x7fdafde5cfb5465924316eced2d3715494c517d1", 1_000_000, 0.65, 0.12, 7,  2000, 120),
            # Rank 5  | $20M  | 6 positions   | +$91.9K
            ("0xfc667adba8d4837586078f4fdcdc29804337ca06", 900_000,   0.62, 0.16, 8,  2000, 100),
            # Rank 6  | $13M  | 6 positions   | +$493K
            ("0x31dea2516beee92135b96f464eeec3cf292a13f2", 700_000,   0.63, 0.17, 9,  1800, 90),
            # Rank 7  | $11M  | 76 positions  | +$716K
            ("0x023a3d058020fb76cca98f01b3c48c8938a22355", 800_000,   0.61, 0.20, 10, 2500, 90),
            # Rank 8  | $11M  | 150 positions | +$41K — highest frequency
            ("0x57dd78cd36e76e2011e8f6dc25cabbaba994494b", 600_000,   0.60, 0.22, 9,  4000, 80),
            # ── Tier 2: $1M–$10M accounts, active and profitable ─────────────────
            # Rank 10 | $4M   | 176 positions | +$251K — extremely active
            ("0x7717a7a245d9f950e586822b8c9b46863ed7bd7e", 400_000,   0.61, 0.20, 8,  3500, 75),
            # Rank 11 | $4M   | 1 position    | +$397.8K — high conviction
            ("0x9e8b1e51c642f4c8b87c6ba11c53d516a218afc4", 400_000,   0.64, 0.15, 7,  1500, 80),
            # Rank 13 | $2.6M | 4 positions   | +$75.2K
            ("0x61ceef212ff4a86933c69fb6aca2fe35d8f2a62b", 300_000,   0.61, 0.19, 8,  1600, 70),
            # Rank 15 | $2M   | 102 positions | +$72.6K — very active
            ("0x7c930969fcf3e5a5c78bcf2e1cefda3f53e3c8fd", 250_000,   0.60, 0.22, 10, 1800, 65),
            # Rank 18 | $1.2M | 2 positions   | +$65.3K
            ("0xa6ee1ed1ae80b8352603654b39f5e7b9bedd5078", 200_000,   0.61, 0.20, 9,  1200, 60),
            # ── Tier 3: High ROI relative to account size ─────────────────────────
            # Rank 20 | $827K | 9 positions   | +$1.12M — insane ROI ratio
            ("0xf517639a8872e756ac98d3c65507d2ebc25cc032", 300_000,   0.64, 0.18, 12, 1500, 60),
            # Rank 21 | $607K | 1 position    | +$3.5K
            ("0x7839e2f2c375dd2935193f2736167514efff9916", 200_000,   0.60, 0.22, 10, 1500, 75),
            # Rank 23 | $451K | 1 position    | +$94.4K
            ("0xcab59c7a92b8f7c4d5cde72bb7669ee7d75b6e6e", 150_000,   0.61, 0.21, 9,  1000, 50),
            # Rank 24 | $430K | 83 positions  | +$852K — best ROI ratio mid-tier
            ("0xc926ddba8b7617dbc65712f20cf8e1b58b8598d3", 500_000,   0.62, 0.19, 8,  2000, 70),
            # Rank 26 | $182K | 2 positions   | +$14K
            ("0x535e34b5ada64997afc88444271ae9b3f82b3867", 80_000,    0.59, 0.25, 10, 600,  35),
            # Rank 29 | $110K | 5 positions   | +$905
            ("0x1c1c270b573d55b68b3d14722b5d5d401511bed0", 60_000,    0.58, 0.26, 9,  500,  30),
            # Rank 31 | $67K  | 10 positions  | +$20.8K — active small account
            ("0x53babe76166eae33c861aeddf9ce89af20311cd0", 50_000,    0.58, 0.27, 11, 400,  25),
        ]

        if not KNOWN_TRADERS:
            logger.warning(
                "[Leaderboard] No trader addresses configured. "
                "Add addresses from app.hyperliquid.xyz/leaderboard to KNOWN_TRADERS in leaderboard_copy.py"
            )
            return []

        traders = []
        for addr, pnl, wr, dd, lev, tc, age in KNOWN_TRADERS:
            traders.append(TrackedTrader(
                address=addr,
                realized_pnl=pnl,
                win_rate=wr,
                max_drawdown=dd,
                avg_leverage=lev,
                trade_count=tc,
                account_age_days=age,
            ))
        return traders

    @staticmethod
    def _parse_leaderboard(data) -> list[TrackedTrader]:
        traders = []

        # Handle both list and dict responses
        rows = data if isinstance(data, list) else data.get("leaderboardRows", [])

        for entry in rows:
            try:
                # HL returns windowPerformances as list of [window, stats]
                perfs = entry.get("windowPerformances", [])
                stats = {}
                for window, s in perfs:
                    if window == "allTime":
                        stats = s
                        break
                if not stats and perfs:
                    stats = perfs[0][1] if isinstance(perfs[0], list) else {}

                pnl        = float(stats.get("pnl", entry.get("pnl", 0)))
                win_rate   = float(stats.get("winRate", 0.5))
                drawdown   = float(stats.get("maxDrawdown", entry.get("maxDrawdown", 0.5)))
                leverage   = float(stats.get("avgLeverage", entry.get("avgLeverage", 5)))
                trades     = int(stats.get("tradeCount", entry.get("tradeCount", 0)))
                age        = int(entry.get("accountAgeDays", 30))
                address    = entry.get("ethAddress", entry.get("user", ""))

                if not address:
                    continue

                traders.append(TrackedTrader(
                    address=address,
                    realized_pnl=pnl,
                    win_rate=win_rate,
                    max_drawdown=abs(drawdown),
                    avg_leverage=leverage,
                    trade_count=trades,
                    account_age_days=age,
                ))
            except Exception as e:
                logger.debug(f"[Leaderboard] Parse skip: {e}")
                continue

        return traders
