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

    # ── Leaderboard polling ────────────────────────────────────────────────────

    async def refresh_leaderboard(self):
        raw = await self._fetch_leaderboard()
        if not raw:
            logger.warning("[Leaderboard] No traders returned from API")
            return

        qualified = [t for t in raw if self._passes_filter(t)]
        qualified.sort(key=lambda t: t.score, reverse=True)
        top = qualified[:10]

        if not top:
            logger.warning("[Leaderboard] No traders passed filter")
            return

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

                # Skip closing trades (closedPnl != 0 means they're exiting)
                if closed_pnl != 0:
                    continue

                # dir: "Open Long" / "Open Short" / "Close Long" / "Close Short"
                if "Long" in side or side == "B":
                    direction = "long"
                elif "Short" in side or side == "A":
                    direction = "short"
                else:
                    continue

                our_size_usd = sz * px * settings.COPY_SIZE_SCALE

                # Min trade size check
                if our_size_usd < 1.0:
                    logger.debug(f"[Leaderboard] Size too small ${our_size_usd:.2f} — skip")
                    continue

                logger.info(
                    f"[Leaderboard] 🔥 COPY {direction.upper()} {coin} "
                    f"${our_size_usd:.2f} | lag={lag_ms:.0f}ms | from {address[:10]}…"
                )

                signal = TradeSignal(
                    strategy="leaderboard",
                    coin=coin,
                    direction=direction,
                    size_usd=our_size_usd,
                    confidence=trader.score,
                    meta={
                        "source": address,
                        "lag_ms": lag_ms,
                        "their_size_usd": sz * px,
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
        try:
            async with aiohttp.ClientSession() as session:
                # HL leaderboard endpoint
                async with session.post(
                    HL_REST,
                    json={"type": "leaderboard", "window": "allTime"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        logger.error(f"[Leaderboard] HTTP {resp.status}")
                        return []
                    data = await resp.json(content_type=None)
                    traders = self._parse_leaderboard(data)
                    logger.info(f"[Leaderboard] Fetched {len(traders)} traders from API")
                    return traders
        except Exception as e:
            logger.error(f"[Leaderboard] Fetch failed: {e}")
            return []

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
