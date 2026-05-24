"""
SqueezeGuard — detects copy-trader manipulation and stop hunts.

Tracks every position's full lifecycle:
  - MAE (max adverse excursion): worst price seen while open
  - MFE (max favorable excursion): best price seen while open
  - Exit reason: trader_closed / stop_loss / take_profit / max_hold
  - Post-exit price at +5min, +15min, +30min

Stop hunt signature: position hits SL, then price recovers past entry within 30min.
Source poisoning: a tracked trader's signals hit SL > 60% of the time.

Alerts sent via Telegram.
All data stored in SQLite for analysis.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from loguru import logger


@dataclass
class TrackedPosition:
    pos_id:       int
    coin:         str
    direction:    str       # "long" | "short"
    entry_price:  float
    entry_time:   float     # unix timestamp
    source:       str       # trader address or strategy
    size_usd:     float

    # Live tracking
    mae:          float = 0.0   # max adverse excursion (positive = % against us)
    mfe:          float = 0.0   # max favorable excursion (positive = % for us)
    last_price:   float = 0.0

    # Post-close monitoring
    closed:       bool  = False
    exit_price:   float = 0.0
    exit_time:    float = 0.0
    exit_reason:  str   = ""
    hit_sl:       bool  = False

    # Recovery checks scheduled after SL hit
    recovery_checks: list = field(default_factory=list)  # [(check_at_ts, label)]


class SqueezeGuard:
    """
    Run alongside position guardian.
    Call update(coin, price) on every price tick.
    Call on_position_opened / on_position_closed from executor.
    """

    def __init__(self, store, alerter):
        self.store   = store
        self.alerter = alerter
        self._positions: dict[int, TrackedPosition] = {}  # pos_id → TrackedPosition

        # Per-source win/loss tracking
        self._source_stats: dict[str, dict] = {}  # address → {wins, losses, sl_hits, fast_sl}

        # Suspicious muted sources (auto-muted after too many SL hits)
        self.muted_sources: set[str] = set()

    # ── Position lifecycle ─────────────────────────────────────────────────────

    def on_position_opened(self, pos_id: int, coin: str, direction: str,
                           entry_price: float, source: str, size_usd: float):
        tp = TrackedPosition(
            pos_id=pos_id, coin=coin, direction=direction,
            entry_price=entry_price, entry_time=time.time(),
            source=source, size_usd=size_usd,
            last_price=entry_price,
        )
        self._positions[pos_id] = tp
        s = self._source_stats.setdefault(source, {"wins":0,"losses":0,"sl_hits":0,"fast_sl":0,"total":0})
        s["total"] += 1
        logger.debug(f"[SqueezeGuard] Tracking pos#{pos_id} {direction} {coin} from {source[:10]}")

    def on_position_closed(self, pos_id: int, exit_price: float, exit_reason: str):
        tp = self._positions.get(pos_id)
        if not tp:
            return

        tp.closed      = True
        tp.exit_price  = exit_price
        tp.exit_time   = time.time()
        tp.exit_reason = exit_reason
        hold_s         = tp.exit_time - tp.entry_time

        # Determine outcome
        if tp.direction == "long":
            pnl_pct = (exit_price - tp.entry_price) / tp.entry_price
        else:
            pnl_pct = (tp.entry_price - exit_price) / tp.entry_price

        win = pnl_pct > 0
        is_sl = exit_reason == "stop_loss"
        fast_sl = is_sl and hold_s < 300   # SL within 5 minutes = suspicious

        s = self._source_stats.setdefault(tp.source, {"wins":0,"losses":0,"sl_hits":0,"fast_sl":0,"total":0})
        if win:
            s["wins"] += 1
        else:
            s["losses"] += 1
        if is_sl:
            s["sl_hits"] += 1
        if fast_sl:
            s["fast_sl"] += 1

        log_msg = (
            f"[SqueezeGuard] Closed pos#{pos_id} {tp.direction} {tp.coin} | "
            f"reason={exit_reason} | hold={hold_s:.0f}s | "
            f"pnl={pnl_pct:+.2%} | MAE={tp.mae:.2%} | MFE={tp.mfe:.2%} | "
            f"src={tp.source[:10]}"
        )
        if fast_sl:
            logger.warning(f"⚠️  {log_msg}  ← FAST SL (possible stop hunt)")
        else:
            logger.info(log_msg)

        # Log to DB
        asyncio.create_task(self._log_to_db(tp, pnl_pct, exit_reason))

        # Schedule recovery checks after SL hits (detect stop hunt reversal)
        if is_sl:
            tp.hit_sl = True
            now = time.time()
            tp.recovery_checks = [
                (now + 300,  "5min"),
                (now + 900,  "15min"),
                (now + 1800, "30min"),
            ]
            asyncio.create_task(self._monitor_recovery(tp))

        # Check if source should be muted
        asyncio.create_task(self._check_source_health(tp.source))

    # ── Price update ───────────────────────────────────────────────────────────

    def update_price(self, coin: str, price: float):
        for tp in self._positions.values():
            if tp.coin != coin or tp.closed:
                continue
            tp.last_price = price

            # MAE / MFE in %
            if tp.direction == "long":
                excursion = (price - tp.entry_price) / tp.entry_price
            else:
                excursion = (tp.entry_price - price) / tp.entry_price

            if excursion > tp.mfe:
                tp.mfe = excursion
            if -excursion > tp.mae:
                tp.mae = -excursion  # store as positive %

    # ── Recovery monitor (stop hunt detection) ────────────────────────────────

    async def _monitor_recovery(self, tp: TrackedPosition):
        """
        After a SL hit, check if price recovered past our entry.
        If so → stop hunt confirmed → alert.
        """
        for check_at, label in tp.recovery_checks:
            wait = check_at - time.time()
            if wait > 0:
                await asyncio.sleep(wait)

            current = self.store.latest_mid(tp.coin)
            if not current:
                continue

            # Did price recover past our entry (stop hunt reversal)?
            if tp.direction == "long":
                recovered = current > tp.entry_price
                recovery_pct = (current - tp.exit_price) / tp.exit_price
            else:
                recovered = current < tp.entry_price
                recovery_pct = (tp.exit_price - current) / tp.exit_price

            if recovered and recovery_pct > 0.015:   # recovered >1.5% past entry
                msg = (
                    f"🚨 *STOP HUNT DETECTED* — {tp.coin}\n"
                    f"We were {tp.direction.upper()}, hit SL @ ${tp.exit_price:.4f}\n"
                    f"Price recovered {recovery_pct:+.1%} in {label} to ${current:.4f}\n"
                    f"Entry was ${tp.entry_price:.4f} | Source: `{tp.source[:12]}`\n"
                    f"MAE was {tp.mae:.2%} — classic wick stop hunt"
                )
                logger.warning(f"[SqueezeGuard] STOP HUNT {tp.coin} — recovered {recovery_pct:+.1%} in {label}")
                await self.alerter.send(msg)
                break   # one alert is enough

    # ── Source health check ────────────────────────────────────────────────────

    async def _check_source_health(self, source: str):
        s = self._source_stats.get(source, {})
        total   = s.get("total", 0)
        sl_hits = s.get("sl_hits", 0)
        fast_sl = s.get("fast_sl", 0)

        if total < 5:
            return   # not enough data

        sl_rate   = sl_hits / total
        fast_rate = fast_sl / total

        # Mute if >65% SL rate or >40% fast SL rate
        if (sl_rate > 0.65 or fast_rate > 0.40) and source not in self.muted_sources:
            self.muted_sources.add(source)
            msg = (
                f"🔇 *SOURCE MUTED* — `{source[:12]}`\n"
                f"SL rate: {sl_rate:.0%} ({sl_hits}/{total} trades)\n"
                f"Fast SL (<5min): {fast_rate:.0%}\n"
                f"Too many of their signals are getting squeezed. Stopped copying."
            )
            logger.warning(f"[SqueezeGuard] MUTED source {source[:12]} — SL rate {sl_rate:.0%}")
            await self.alerter.send(msg)

    # ── DB logging ─────────────────────────────────────────────────────────────

    async def _log_to_db(self, tp: TrackedPosition, pnl_pct: float, exit_reason: str):
        try:
            db = self.store._db
            if not db:
                return
            await db.execute("""
                INSERT OR IGNORE INTO position_log
                (pos_id, coin, direction, source, entry_price, entry_time,
                 exit_price, exit_time, exit_reason, pnl_pct, mae, mfe,
                 hold_seconds, size_usd)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                tp.pos_id, tp.coin, tp.direction, tp.source,
                tp.entry_price, datetime.fromtimestamp(tp.entry_time, tz=timezone.utc).isoformat(),
                tp.exit_price, datetime.fromtimestamp(tp.exit_time, tz=timezone.utc).isoformat(),
                exit_reason, pnl_pct, tp.mae, tp.mfe,
                tp.exit_time - tp.entry_time, tp.size_usd,
            ))
            await db.commit()
        except Exception as e:
            logger.debug(f"[SqueezeGuard] DB log failed: {e}")

    # ── Stats summary ──────────────────────────────────────────────────────────

    def summary(self) -> str:
        lines = ["*Source performance:*"]
        for src, s in sorted(self._source_stats.items(),
                              key=lambda x: x[1].get("total", 0), reverse=True):
            total = s.get("total", 0)
            if total == 0:
                continue
            wr  = s.get("wins", 0) / total
            slr = s.get("sl_hits", 0) / total
            muted = " 🔇" if src in self.muted_sources else ""
            lines.append(
                f"`{src[:12]}` — {total} trades | WR {wr:.0%} | SL {slr:.0%}{muted}"
            )
        return "\n".join(lines) if len(lines) > 1 else "No data yet"
