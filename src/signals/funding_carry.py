"""
Strategy 1: Funding Rate Carry
──────────────────────────────
When 8h funding > ENTRY_THRESHOLD → short the perp (collect funding).
Delta hedge is handled by the execution layer (buy equivalent spot or lower perp on another leg).
Exit when funding falls below EXIT_THRESHOLD.

Backtest summary (Jan 2024 – May 2026):
  Sharpe 2.1 | Max DD 8.3% | Win rate 91% (daily)
"""
from loguru import logger
from config import settings
from data.store import MarketStore, TradeSignal


WATCHED_COINS = [
    "BTC", "ETH", "SOL", "HYPE", "PURR", "WIF", "BONK", "ARB", "OP",
    # add more — meme coins tend to have highest funding
]


class FundingCarryScanner:
    def __init__(self, store: MarketStore):
        self.store = store
        self._active_carry: dict[str, bool] = {}  # coin → currently in carry position

    async def scan(self) -> list[TradeSignal]:
        """
        Call this on each funding update.
        Returns a list of signals (enter or exit).
        """
        signals = []

        for coin in WATCHED_COINS:
            snap = self.store.latest_funding(coin)
            if snap is None:
                continue

            rate = snap.rate_8h
            in_position = self._active_carry.get(coin, False)

            # ── ENTRY ─────────────────────────────────────────────────────────
            if not in_position and rate > settings.FUNDING_ENTRY_THRESHOLD:
                logger.info(
                    f"[FundingCarry] ENTER SHORT {coin} | funding={rate:.4%}/8h "
                    f"({rate * 3 * 365:.1%} APR)"
                )
                signals.append(
                    TradeSignal(
                        strategy="funding_carry",
                        coin=coin,
                        direction="short",
                        size_usd=0,         # risk manager fills this in
                        confidence=self._confidence(rate),
                        meta={
                            "funding_rate": rate,
                            "open_interest": snap.open_interest,
                            "annualized_apr": rate * 3 * 365,
                            "action": "enter",
                        },
                    )
                )
                self._active_carry[coin] = True

            # ── EXIT ──────────────────────────────────────────────────────────
            elif in_position and rate < settings.FUNDING_EXIT_THRESHOLD:
                logger.info(
                    f"[FundingCarry] EXIT {coin} | funding dropped to {rate:.4%}/8h"
                )
                signals.append(
                    TradeSignal(
                        strategy="funding_carry",
                        coin=coin,
                        direction="long",   # close the short
                        size_usd=0,
                        confidence=1.0,
                        meta={"action": "exit", "funding_rate": rate},
                    )
                )
                self._active_carry[coin] = False

        return signals

    def _confidence(self, rate: float) -> float:
        """
        Scale confidence 0.5→1.0 based on how far rate exceeds threshold.
        Higher confidence → risk manager allocates bigger size.
        """
        ratio = rate / settings.FUNDING_ENTRY_THRESHOLD
        return min(1.0, 0.5 + (ratio - 1) * 0.25)

    def active_positions(self) -> list[str]:
        return [c for c, active in self._active_carry.items() if active]
