"""
Phase 3: Statistical Arbitrage — Correlated Pair Mean Reversion
────────────────────────────────────────────────────────────────
Tracks price ratios between correlated assets. When the ratio drifts
> 2 standard deviations from its rolling mean, it fades back.

Pairs traded:
  BTC/ETH  — tightest correlation on crypto, mean-reverts within hours
  SOL/ETH  — alt L1 relative value
  HYPE/ETH — newer but highly correlated during risk-on/off moves

Entry: ratio > mean + 2σ  → short the expensive leg, long the cheap leg
Exit:  ratio returns within 0.5σ of mean

Market neutral: dollar-balanced long + short = no directional exposure.

Enable: STRATEGY_STAT_ARB=true in .env
"""
import asyncio
from collections import deque
from dataclasses import dataclass
from loguru import logger
from config import settings
from data.store import MarketStore, TradeSignal

# Pairs: (numerator_coin, denominator_coin, lookback_ticks, z_entry, z_exit)
# lookback 200 ticks × 15s = ~50 minutes of history
PAIRS = [
    ("ETH",  "BTC",  200, 2.0, 0.5),
    ("SOL",  "ETH",  200, 2.0, 0.5),
    ("HYPE", "ETH",  150, 2.2, 0.5),
    ("NEAR", "SOL",  150, 2.2, 0.5),
]


@dataclass
class PairState:
    coin_a:    str
    coin_b:    str
    ratios:    deque
    lookback:  int
    z_entry:   float
    z_exit:    float
    active_direction: str = ""   # "a_over" | "b_over" | ""


class StatArbScanner:
    def __init__(self, store: MarketStore):
        self.store = store
        self._pairs: list[PairState] = [
            PairState(a, b, deque(maxlen=lb), lb, ze, zx)
            for a, b, lb, ze, zx in PAIRS
        ]

    async def scan(self) -> list[TradeSignal]:
        signals = []
        for pair in self._pairs:
            self._update_ratio(pair)
            sigs = self._evaluate(pair)
            signals.extend(sigs)
        return signals

    def _update_ratio(self, pair: PairState):
        price_a = self.store.latest_mid(pair.coin_a)
        price_b = self.store.latest_mid(pair.coin_b)
        if price_a and price_b and price_b > 0:
            pair.ratios.append(price_a / price_b)

    def _evaluate(self, pair: PairState) -> list[TradeSignal]:
        if len(pair.ratios) < pair.lookback * 0.8:
            return []   # not enough history

        ratios = list(pair.ratios)
        mean   = sum(ratios) / len(ratios)
        var    = sum((r - mean) ** 2 for r in ratios) / len(ratios)
        std    = var ** 0.5
        if std == 0:
            return []

        current = ratios[-1]
        z_score = (current - mean) / std

        signals = []

        # Already in a trade — check for exit
        if pair.active_direction:
            if abs(z_score) < pair.z_exit:
                logger.info(
                    f"[StatArb] EXIT {pair.coin_a}/{pair.coin_b} | "
                    f"z={z_score:.2f} returned to mean"
                )
                # Exit both legs
                if pair.active_direction == "a_over":
                    # We were short A, long B — reverse
                    signals.append(self._exit_signal(pair.coin_a, "short"))
                    signals.append(self._exit_signal(pair.coin_b, "long"))
                else:
                    signals.append(self._exit_signal(pair.coin_a, "long"))
                    signals.append(self._exit_signal(pair.coin_b, "short"))
                pair.active_direction = ""
            return signals

        # No active trade — check for entry
        if z_score > pair.z_entry:
            # A is expensive relative to B → short A, long B
            pair.active_direction = "a_over"
            logger.info(
                f"[StatArb] 📐 ENTRY {pair.coin_a}/{pair.coin_b} | "
                f"z={z_score:.2f} > {pair.z_entry} | "
                f"ratio={current:.4f} mean={mean:.4f}"
            )
            signals.append(self._enter_signal(pair.coin_a, "short", z_score, pair))
            signals.append(self._enter_signal(pair.coin_b, "long",  z_score, pair))

        elif z_score < -pair.z_entry:
            # B is expensive relative to A → long A, short B
            pair.active_direction = "b_over"
            logger.info(
                f"[StatArb] 📐 ENTRY {pair.coin_a}/{pair.coin_b} | "
                f"z={z_score:.2f} < -{pair.z_entry} | "
                f"ratio={current:.4f} mean={mean:.4f}"
            )
            signals.append(self._enter_signal(pair.coin_a, "long",  z_score, pair))
            signals.append(self._enter_signal(pair.coin_b, "short", z_score, pair))

        return signals

    def _enter_signal(self, coin: str, direction: str,
                      z: float, pair: PairState) -> TradeSignal:
        confidence = min(abs(z) / pair.z_entry / 2, 1.0)
        return TradeSignal(
            strategy="arb",
            coin=coin,
            direction=direction,
            size_usd=0,
            confidence=confidence,
            meta={
                "action": "enter",
                "z_score": z,
                "pair": f"{pair.coin_a}/{pair.coin_b}",
                "reason": f"stat arb z={z:.2f}",
            },
        )

    def _exit_signal(self, coin: str, direction: str) -> TradeSignal:
        return TradeSignal(
            strategy="arb",
            coin=coin,
            direction=direction,
            size_usd=0,
            confidence=1.0,
            meta={"action": "exit", "reason": "arb mean reversion complete"},
        )
