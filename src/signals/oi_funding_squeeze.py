"""
Phase 2: OI/Funding Divergence Squeeze
────────────────────────────────────────
When open interest is at multi-day highs AND funding is extreme (one side paying
a lot), the market is overcrowded. Mean reversion is coming.

Logic:
  - OI > 90th percentile of last 500 ticks (crowded)
  - |funding_8h| > 0.05% (0.0005) — one side paying heavily
  - Price has moved > 3% in the funding direction in last hour (extended)
  → Fade: go opposite to funding direction (short if longs paying, long if shorts paying)

Edge: crowded + expensive to hold + extended price = squeeze setup.
Exit: when funding normalises back below 0.02%, or position guardian SL/TP.

Enable: STRATEGY_OI_SQUEEZE=true in .env
"""
import asyncio
from loguru import logger
from config import settings
from data.store import MarketStore, TradeSignal

# Coins to watch — high OI, liquid enough for this strategy
SQUEEZE_COINS = [
    "BTC", "ETH", "SOL", "HYPE", "WIF", "BONK",
    "ARB", "OP", "SUI", "AVAX", "NEAR", "INJ",
]

FUNDING_ENTRY    = 0.0005   # 0.05%/8h — longs/shorts paying a lot
FUNDING_EXIT     = 0.0002   # normalised — exit signal
OI_PERCENTILE    = 88       # OI must be in top 12% of recent history
MIN_PRICE_MOVE   = 0.025    # price extended 2.5%+ in funding direction


class OIFundingSqueeze:
    def __init__(self, store: MarketStore):
        self.store = store
        self._active: dict[str, str] = {}   # coin → direction we're short/long

    async def scan(self) -> list[TradeSignal]:
        signals = []

        for coin in SQUEEZE_COINS:
            try:
                sig = self._evaluate(coin)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logger.debug(f"[OISqueeeze] {coin} eval error: {e}")

        return signals

    def _evaluate(self, coin: str) -> TradeSignal | None:
        snap = self.store.latest_funding(coin)
        if not snap:
            return None

        funding  = snap.rate_8h
        oi       = snap.open_interest
        mid      = self.store.latest_mid(coin)
        if not mid or oi == 0:
            return None

        # OI percentile check
        oi_p = self.store.oi_percentile(coin, OI_PERCENTILE)
        if oi_p is None or oi < oi_p:
            return None   # OI not elevated enough

        abs_funding = abs(funding)
        if abs_funding < FUNDING_ENTRY:
            return None   # funding not extreme

        # Price extension check — look at price history
        prices = list(self.store.mid_prices.get(coin, []))
        if len(prices) < 20:
            return None
        # Compare current price to price ~1h ago (240 ticks at 15s each)
        lookback = min(240, len(prices) - 1)
        old_price = prices[-lookback - 1]
        price_move = (mid - old_price) / old_price  # positive = price went up

        # Funding positive = longs paying = too many longs = price went up too much
        # We fade: go SHORT
        if funding > FUNDING_ENTRY and price_move > MIN_PRICE_MOVE:
            direction = "short"
        # Funding negative = shorts paying = too many shorts = price went down too much
        # We fade: go LONG
        elif funding < -FUNDING_ENTRY and price_move < -MIN_PRICE_MOVE:
            direction = "long"
        else:
            return None

        # Don't double-enter same coin/direction
        if self._active.get(coin) == direction:
            return None
        self._active[coin] = direction

        annualised = funding * 3 * 365 * 100
        logger.info(
            f"[OISqueeeze] 🎯 SQUEEZE {direction.upper()} {coin} | "
            f"funding={funding*100:.3f}%/8h ({annualised:.0f}% APR) | "
            f"OI at {OI_PERCENTILE}th pct | price_move={price_move:+.1%}"
        )

        return TradeSignal(
            strategy="squeeze",
            coin=coin,
            direction=direction,
            size_usd=0,
            confidence=min(abs_funding / FUNDING_ENTRY, 1.5) / 1.5,  # scale with conviction
            meta={
                "action":      "enter",
                "funding":     funding,
                "oi":          oi,
                "price_move":  price_move,
                "reason":      f"OI squeeze — funding {funding*100:.3f}%/8h",
            },
        )

    def clear_active(self, coin: str):
        """Call when position closes so we can re-enter."""
        self._active.pop(coin, None)
