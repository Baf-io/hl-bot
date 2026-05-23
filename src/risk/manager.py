"""
Risk Manager — the only thing standing between you and a blown account.
NEVER bypass this layer. Every signal passes through here before execution.

Rules enforced:
  1. Max 5 open positions
  2. Max 8% portfolio per position
  3. Portfolio delta-neutral check (max 15% net delta)
  4. Daily loss halt at -3%
  5. No 3+ correlated positions simultaneously
  6. Max leverage cap
"""
from dataclasses import dataclass, field
from datetime import datetime, date
from loguru import logger
from config import settings
from data.store import TradeSignal


# Correlation groups — don't hold 3+ from same group
CORRELATION_GROUPS = [
    {"BTC", "ETH"},                          # majors (high correlation)
    {"SOL", "AVAX", "SUI", "APT"},           # alt L1s
    {"WIF", "BONK", "PURR", "PEPE", "FLOKI"}, # meme coins
]


@dataclass
class OpenPosition:
    id: int
    coin: str
    direction: str              # "long" | "short"
    size_usd: float
    entry_price: float
    strategy: str
    opened_at: datetime = field(default_factory=datetime.utcnow)
    unrealized_pnl: float = 0.0


class RiskManager:
    def __init__(self, portfolio_value_usd: float):
        self.portfolio_value     = portfolio_value_usd
        self.open_positions: list[OpenPosition] = []
        self._daily_pnl          = 0.0
        self._trading_halted     = False
        self._last_reset_date    = date.today()
        self._position_id_seq    = 0

    # ── Daily reset ────────────────────────────────────────────────────────────

    def _check_daily_reset(self):
        today = date.today()
        if today != self._last_reset_date:
            logger.info(f"Daily reset | yesterday PnL: ${self._daily_pnl:+,.2f}")
            self._daily_pnl = 0.0
            self._trading_halted = False
            self._last_reset_date = today

    # ── Main gate ──────────────────────────────────────────────────────────────

    def approve(self, signal: TradeSignal) -> tuple[bool, str, float]:
        """
        Evaluate signal. Returns (approved, reason, approved_size_usd).
        The returned size may be smaller than signal.size_usd.
        """
        self._check_daily_reset()

        if self._trading_halted:
            return False, "HALTED: daily loss limit hit", 0

        if signal.meta.get("action") == "exit":
            return True, "exit approved", signal.size_usd

        # ── Rule 1: max positions ──────────────────────────────────────────────
        if len(self.open_positions) >= settings.MAX_OPEN_POSITIONS:
            return False, f"max positions ({settings.MAX_OPEN_POSITIONS}) reached", 0

        # ── Rule 2: max per-position size ─────────────────────────────────────
        max_size = self.portfolio_value * settings.MAX_POSITION_SIZE_PCT
        raw_size = signal.size_usd if signal.size_usd > 0 else max_size * signal.confidence
        size = min(raw_size, max_size)

        # ── Rule 3: correlation check ─────────────────────────────────────────
        if self._too_correlated(signal.coin):
            return False, f"{signal.coin} would create 3+ correlated positions", 0

        # ── Rule 4: net delta check ────────────────────────────────────────────
        delta_ok, delta_msg = self._delta_check(signal, size)
        if not delta_ok:
            return False, delta_msg, 0

        # ── Rule 5: leverage check ────────────────────────────────────────────
        # (execution layer enforces the actual leverage; we just flag here)
        if signal.meta.get("leverage", 1) > settings.MAX_LEVERAGE:
            return False, f"leverage {signal.meta['leverage']}x > max {settings.MAX_LEVERAGE}x", 0

        logger.info(
            f"[Risk] APPROVED {signal.strategy} {signal.direction} {signal.coin} "
            f"size=${size:,.0f} confidence={signal.confidence:.2f}"
        )
        return True, "approved", size

    # ── Position tracking ──────────────────────────────────────────────────────

    def register_fill(self, signal: TradeSignal, filled_size_usd: float, price: float) -> int:
        self._position_id_seq += 1
        pos = OpenPosition(
            id=self._position_id_seq,
            coin=signal.coin,
            direction=signal.direction,
            size_usd=filled_size_usd,
            entry_price=price,
            strategy=signal.strategy,
        )
        self.open_positions.append(pos)
        logger.info(f"[Risk] Position registered #{pos.id} {pos.coin} {pos.direction}")
        return pos.id

    def close_position(self, position_id: int, exit_price: float):
        pos = next((p for p in self.open_positions if p.id == position_id), None)
        if not pos:
            logger.warning(f"[Risk] close_position: #{position_id} not found")
            return
        if pos.direction == "short":
            pnl = pos.size_usd * (pos.entry_price - exit_price) / pos.entry_price
        else:
            pnl = pos.size_usd * (exit_price - pos.entry_price) / pos.entry_price

        self._daily_pnl += pnl
        self.open_positions.remove(pos)

        pct = self._daily_pnl / self.portfolio_value
        logger.info(
            f"[Risk] Closed #{position_id} {pos.coin} | trade PnL=${pnl:+,.2f} "
            f"| day PnL={pct:+.2%}"
        )

        if pct <= -settings.DAILY_LOSS_HALT_PCT:
            self._trading_halted = True
            logger.warning(
                f"[Risk] ⛔ TRADING HALTED — daily loss {pct:.2%} "
                f"exceeds -{settings.DAILY_LOSS_HALT_PCT:.0%} limit"
            )

    def update_unrealized(self, coin: str, current_price: float):
        for pos in self.open_positions:
            if pos.coin != coin:
                continue
            if pos.direction == "short":
                pos.unrealized_pnl = pos.size_usd * (pos.entry_price - current_price) / pos.entry_price
            else:
                pos.unrealized_pnl = pos.size_usd * (current_price - pos.entry_price) / pos.entry_price

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _too_correlated(self, coin: str) -> bool:
        for group in CORRELATION_GROUPS:
            if coin not in group:
                continue
            count = sum(1 for p in self.open_positions if p.coin in group)
            if count >= 2:
                return True
        return False

    def _delta_check(self, signal: TradeSignal, size: float) -> tuple[bool, str]:
        current_delta = sum(
            p.size_usd if p.direction == "long" else -p.size_usd
            for p in self.open_positions
        )
        new_delta = size if signal.direction == "long" else -size
        total_delta = abs(current_delta + new_delta)
        max_delta = self.portfolio_value * settings.PORTFOLIO_DELTA_MAX
        if total_delta > max_delta:
            return False, f"net delta ${total_delta:,.0f} would exceed ${max_delta:,.0f} limit"
        return True, ""

    # ── Status ─────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        return {
            "portfolio_value": self.portfolio_value,
            "open_positions": len(self.open_positions),
            "daily_pnl": self._daily_pnl,
            "daily_pnl_pct": self._daily_pnl / self.portfolio_value,
            "halted": self._trading_halted,
            "positions": [
                {
                    "id": p.id, "coin": p.coin, "direction": p.direction,
                    "size_usd": p.size_usd, "unrealized_pnl": p.unrealized_pnl,
                    "strategy": p.strategy,
                }
                for p in self.open_positions
            ],
        }
