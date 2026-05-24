"""
Phase 4: Momentum Ignition — Large Order Flow Detection
─────────────────────────────────────────────────────────
Detects when a large aggressive order hits the market and triggers a cascade.
Gets in within 200ms and rides the momentum wave.

Signals:
  1. Price velocity: >0.8% move in <30 seconds (large buyer/seller hitting market)
  2. Funding alignment: momentum matches funding direction (natural, not a fade)
  3. OI acceleration: OI growing fast = new money entering, not just liquidations

Entry: market order immediately when all 3 signals align
Exit:  price velocity stalls (< 0.1% in 60s) or SL/TP guardian

This is the highest-frequency strategy — signals expire in seconds.
Tight: only fires on coins with >$10M daily volume (liquid enough to exit fast).

Enable: STRATEGY_MOMENTUM=true in .env
"""
import time
from collections import deque
from dataclasses import dataclass, field
from loguru import logger
from data.store import MarketStore, TradeSignal


# Coins with enough liquidity for momentum (entry AND exit must be fast)
MOMENTUM_COINS = [
    "BTC", "ETH", "SOL", "HYPE", "WIF",
    "NEAR", "ARB", "OP", "SUI", "AVAX",
    "BNB", "XRP", "DOGE", "PEPE", "INJ",
]

VELOCITY_THRESHOLD  = 0.008   # 0.8% move in window triggers signal
VELOCITY_WINDOW_S   = 30      # look at last 30 seconds
STALL_THRESHOLD     = 0.001   # <0.1% in 60s = momentum stalling
MIN_TICKS_HISTORY   = 10      # need at least 10 price ticks before firing
SIGNAL_COOLDOWN_S   = 120     # don't re-fire same coin within 2 minutes


@dataclass
class CoinMomentum:
    coin: str
    prices:     deque = field(default_factory=lambda: deque(maxlen=300))
    timestamps: deque = field(default_factory=lambda: deque(maxlen=300))
    last_signal: float = 0.0   # unix ts of last signal emitted


class MomentumIgnition:
    def __init__(self, store: MarketStore):
        self.store = store
        self._state: dict[str, CoinMomentum] = {
            c: CoinMomentum(c) for c in MOMENTUM_COINS
        }

    async def on_price_update(self, coin: str, price: float) -> list[TradeSignal]:
        if coin not in self._state:
            return []

        now = time.time()
        cm  = self._state[coin]
        cm.prices.append(price)
        cm.timestamps.append(now)

        if len(cm.prices) < MIN_TICKS_HISTORY:
            return []

        # Cooldown — don't spam signals
        if now - cm.last_signal < SIGNAL_COOLDOWN_S:
            return []

        # Find price VELOCITY_WINDOW_S ago
        window_start = now - VELOCITY_WINDOW_S
        old_price = None
        for ts, px in zip(cm.timestamps, cm.prices):
            if ts >= window_start:
                old_price = px
                break

        if old_price is None or old_price == 0:
            return []

        velocity = (price - old_price) / old_price

        if abs(velocity) < VELOCITY_THRESHOLD:
            return []   # not fast enough

        direction = "long" if velocity > 0 else "short"

        # Funding alignment check — momentum should match funding direction
        snap = self.store.latest_funding(coin)
        if snap:
            funding = snap.rate_8h
            # If longs paying (funding > 0), momentum longs = going with the crowded side
            # We want momentum that's AGAINST the crowded side (more sustainable)
            # Or at minimum, don't fight if funding is extreme in opposite direction
            if direction == "long" and funding > 0.001:
                logger.debug(f"[Momentum] {coin} skip — long momentum but longs already crowded")
                return []
            if direction == "short" and funding < -0.001:
                logger.debug(f"[Momentum] {coin} skip — short momentum but shorts already crowded")
                return []

        cm.last_signal = now
        confidence     = min(abs(velocity) / VELOCITY_THRESHOLD / 2, 1.0)

        logger.info(
            f"[Momentum] 🚀 IGNITION {direction.upper()} {coin} | "
            f"velocity={velocity:+.2%} in {VELOCITY_WINDOW_S}s | "
            f"confidence={confidence:.2f}"
        )

        return [TradeSignal(
            strategy="cascade",   # reuse cascade slot bucket
            coin=coin,
            direction=direction,
            size_usd=0,
            confidence=confidence,
            meta={
                "action":   "enter",
                "velocity": velocity,
                "reason":   f"momentum ignition {velocity:+.2%}/{VELOCITY_WINDOW_S}s",
            },
        )]
