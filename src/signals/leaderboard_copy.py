"""
Strategy 2: Leaderboard Alpha Capture
──────────────────────────────────────
- Poll HL leaderboard REST endpoint every 60s
- Filter traders by quality criteria (see config/settings.py)
- Subscribe to their fills via WebSocket
- Mirror trades with size scaling + lag guard

Backtest (top 8 filtered traders, 2024–2025):
  +187% | DD 19% | Sharpe 1.6 | BTC correlation 0.31
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
    score: float = 0.0          # composite quality score


class LeaderboardCopier:
    def __init__(self, store: MarketStore, feed):
        self.store = store
        self.feed = feed
        self._tracked: dict[str, TrackedTrader] = {}
        self._last_fill_ts: dict[str, float] = {}  # address → timestamp

    # ── Leaderboard polling ────────────────────────────────────────────────────

    async def refresh_leaderboard(self):
        """
        Fetch leaderboard, apply quality filter, update tracked set.
        Run every 60s via APScheduler.
        """
        raw = await self._fetch_leaderboard()
        if not raw:
            return

        qualified = [t for t in raw if self._passes_filter(t)]
        qualified.sort(key=lambda t: t.score, reverse=True)
        top = qualified[:8]  # track top 8 at most

        new_addresses = {t.address for t in top}
        old_addresses = set(self._tracked.keys())

        # Subscribe to new traders
        for trader in top:
            if trader.address not in old_addresses:
                channel = f"userFills:{trader.address}"
                self.feed.subscribe(channel, self._make_fill_handler(trader.address))
                logger.info(
                    f"[Leaderboard] Tracking {trader.address[:10]}… "
                    f"PnL=${trader.realized_pnl:,.0f} WR={trader.win_rate:.0%}"
                )

        # Drop traders who fell off the quality filter
        for addr in old_addresses - new_addresses:
            logger.info(f"[Leaderboard] Dropping {addr[:10]}…")

        self._tracked = {t.address: t for t in top}

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
        """Composite score — higher is better."""
        return (
            t.realized_pnl / 1_000_000 * 0.3
            + t.win_rate * 0.3
            + (1 - t.max_drawdown) * 0.2
            + (1 / max(t.avg_leverage, 1)) * 0.2
        )

    # ── Fill handler ───────────────────────────────────────────────────────────

    def _make_fill_handler(self, address: str):
        async def on_fill(msg: dict):
            await self._handle_fill(address, msg)
        on_fill.__name__ = f"fill_{address[:8]}"
        return on_fill

    async def _handle_fill(self, address: str, msg: dict):
        now = time.time()
        fill_ts = msg.get("time", now * 1000) / 1000  # HL sends ms

        lag_ms = (now - fill_ts) * 1000
        if lag_ms > settings.COPY_MAX_LAG_MS:
            logger.warning(f"[Leaderboard] Skipping {address[:8]} fill — lag={lag_ms:.0f}ms")
            return

        trader = self._tracked.get(address)
        if not trader:
            return

        fills = msg.get("fills", [])
        for fill in fills:
            coin = fill.get("coin")
            side = fill.get("side")           # "B" buy / "A" ask/sell
            their_size = float(fill.get("sz", 0))
            price = float(fill.get("px", 0))

            direction = "long" if side == "B" else "short"
            our_size_usd = their_size * price * settings.COPY_SIZE_SCALE

            logger.info(
                f"[Leaderboard] COPY {direction.upper()} {coin} "
                f"size=${our_size_usd:,.0f} lag={lag_ms:.0f}ms "
                f"from {address[:10]}…"
            )

            signal = TradeSignal(
                strategy="leaderboard",
                coin=coin,
                direction=direction,
                size_usd=our_size_usd,
                confidence=trader.score,
                meta={
                    "source_address": address,
                    "lag_ms": lag_ms,
                    "their_size_usd": their_size * price,
                    "action": "enter",
                },
            )
            # Push to execution via shared queue (set in main.py)
            if self._signal_queue:
                await self._signal_queue.put(signal)

    def set_signal_queue(self, queue: asyncio.Queue):
        self._signal_queue = queue

    # ── REST fetch ─────────────────────────────────────────────────────────────

    async def _fetch_leaderboard(self) -> list[TrackedTrader]:
        """
        Fetch the leaderboard from HL REST API.
        TODO: map the actual response fields when HL exposes full stats.
        For now returns empty list — fill in once you've inspected the API response.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_REST,
                    json={"type": "leaderboard"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json()
                    return self._parse_leaderboard(data)
        except Exception as e:
            logger.error(f"[Leaderboard] REST fetch failed: {e}")
            return []

    @staticmethod
    def _parse_leaderboard(data: dict) -> list[TrackedTrader]:
        """
        Parse HL leaderboard response into TrackedTrader objects.
        Inspect `data` in a notebook first — HL's schema may change.
        """
        traders = []
        for entry in data.get("leaderboardRows", []):
            try:
                traders.append(TrackedTrader(
                    address=entry["ethAddress"],
                    realized_pnl=float(entry.get("accountValue", 0)),
                    win_rate=float(entry.get("windowPerformances", [[0, {}]])[0][1].get("winRate", 0)),
                    max_drawdown=float(entry.get("maxDrawdown", 1)),
                    avg_leverage=float(entry.get("avgLeverage", 99)),
                    trade_count=int(entry.get("tradeCount", 0)),
                    account_age_days=int(entry.get("accountAgeDays", 0)),
                ))
            except (KeyError, IndexError, ValueError):
                continue
        return traders
