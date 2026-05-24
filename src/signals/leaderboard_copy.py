"""
Strategy 2: Leaderboard Alpha Capture
──────────────────────────────────────
Polls HL leaderboard, filters quality traders, mirrors their fills via WebSocket.

KEY DESIGN: Position-aware tracking
  • We track each trader's known open positions (coin → direction).
  • Fills that add to an existing position (TWAP) are SKIPPED — we already entered.
  • We only fire entry signals on genuinely NEW positions (coin not in tracker).
  • On full close: remove from tracker, fire exit signal.
  • On direction flip: fire exit for old side, then entry for new side.
  This eliminates the 10-min dedup race that caused 16x HYPE entries in one day.
"""
import asyncio
import os
import time
from collections import defaultdict
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

SKIP_PREFIXES = ("xyz:", "@", "km:", "k:")   # tokenized stocks / synthetic assets


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

        # Position state per trader: address → {coin → "long"|"short"}
        # Populated from API on startup and kept in sync by fill handler.
        # This is the core guard against TWAP re-entries.
        self._trader_positions: dict[str, dict[str, str]] = defaultdict(dict)

        # Account equity per trader for proportional sizing
        self._trader_acct_values: dict[str, float] = {}

        # Position notionals from API: (address, coin) → their_notional_usd
        # Seeded by _refresh_account_values, used by backfill for accurate sizing.
        self._trader_position_notionals: dict[tuple, float] = {}

        # Leverage per position: (address, coin) → leverage multiplier
        # e.g. 10.0 for 10x.  Used to convert margin-cap → notional in sizing.
        # Seeded from marginUsed / positionValue in _refresh_account_values.
        self._trader_position_leverages: dict[tuple, float] = {}

        # Exit dedup: coin → last_exit_timestamp
        # Prevents TWAP exits (N close fills for one position) from firing N exit signals.
        self._recent_exits: dict[str, float] = {}

        # Cache portfolio size — read once at init, not on every fill
        self._portfolio_usd: float = float(os.getenv("PORTFOLIO_USD", 1000))

        # Event set by _refresh_account_values() when all traders are fetched.
        # backfill_existing_positions() waits on this instead of a fixed sleep,
        # so it never fires before all _trader_positions are populated.
        self._refresh_done: asyncio.Event = asyncio.Event()

    # ── Leaderboard polling ────────────────────────────────────────────────────

    async def refresh_leaderboard(self):
        raw = await self._fetch_leaderboard()
        if not raw:
            logger.warning("[Leaderboard] No traders returned")
            return

        # Whitelist mode: only track explicitly approved traders
        if settings.COPY_TRADER_WHITELIST:
            raw = [t for t in raw if t.address.lower() in settings.COPY_TRADER_WHITELIST]
            logger.info(
                f"[Leaderboard] Whitelist — {len(raw)} traders: "
                + ", ".join(a[:10] + "..." for a in settings.COPY_TRADER_WHITELIST)
            )

        for t in raw:
            t.score = self._score(t)

        new_addresses = {t.address for t in raw}
        old_addresses = set(self._tracked.keys())

        for trader in raw:
            if trader.address not in old_addresses:
                channel = f"userFills:{trader.address}"
                self.feed.subscribe(channel, self._make_fill_handler(trader.address))
                logger.info(
                    f"[Leaderboard] Tracking {trader.address[:10]}... "
                    f"PnL=${trader.realized_pnl:,.0f} score={trader.score:.3f}"
                )

        for addr in old_addresses - new_addresses:
            logger.info(f"[Leaderboard] Dropping {addr[:10]}...")

        self._tracked = {t.address: t for t in raw}
        logger.info(f"[Leaderboard] Tracking {len(self._tracked)} traders")

        # Refresh account equity + position state for proportional sizing.
        # Reset the done-event first so backfill (on startup) waits for the new fetch.
        self._refresh_done.clear()
        asyncio.get_event_loop().create_task(self._refresh_account_values())

    async def _refresh_account_values(self):
        """
        Fetch each tracked trader's account equity and current positions.
        Runs after refresh_leaderboard(). Failure is non-fatal.

        Dual purpose:
          1. Cache account equity for proportional size calculation.
          2. Seed _trader_positions so backfill and fill handler know what
             the trader already holds — prevents duplicate entries.

        Sets self._refresh_done event when all traders have been fetched so
        backfill_existing_positions() can wait on it instead of a fixed sleep.
        """
        async with aiohttp.ClientSession() as session:
            for address in list(self._tracked.keys()):
                try:
                    async with session.post(
                        HL_REST,
                        json={"type": "clearinghouseState", "user": address},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        state = await resp.json(content_type=None)

                    ms       = state.get("marginSummary", {})
                    acct_val = float(ms.get("accountValue", 0))
                    if acct_val > 0:
                        self._trader_acct_values[address] = acct_val

                    # Seed position tracker, notional cache, and leverage cache from live state
                    known = self._trader_positions[address]
                    for ap in state.get("assetPositions", []):
                        pos  = ap.get("position", {})
                        szi  = float(pos.get("szi", 0))
                        coin = pos.get("coin", "")
                        if coin.startswith(SKIP_PREFIXES):
                            continue
                        ep = float(pos.get("entryPx") or 0)
                        notional = abs(szi) * ep
                        if notional < 50:   # dust — ignore
                            continue
                        if szi != 0:
                            known[coin] = "long" if szi > 0 else "short"
                            self._trader_position_notionals[(address, coin)] = notional
                            # Cache leverage: notional / marginUsed
                            margin_used = float(pos.get("marginUsed") or 0)
                            if margin_used > 0:
                                lev = min(notional / margin_used, settings.COPY_MAX_COPY_LEVERAGE)
                                self._trader_position_leverages[(address, coin)] = round(lev, 1)

                    logger.debug(
                        f"[Leaderboard] {address[:10]}... acct=${acct_val:,.0f} "
                        f"known_pos={list(known.keys())}"
                    )
                    await asyncio.sleep(0.4)

                except Exception as e:
                    logger.debug(f"[Leaderboard] acct refresh error {address[:10]}: {e}")

        # Signal that all traders have been fetched — backfill can now proceed safely
        self._refresh_done.set()
        logger.info("[Leaderboard] Account refresh complete — backfill unlocked")

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

    # ── Sizing helper ──────────────────────────────────────────────────────────

    def _compute_size(
        self,
        address: str,
        coin: str,
        their_notional: float,
        their_acct_val: float,
        trader: "TrackedTrader",
    ) -> tuple[float, float]:
        """
        Margin-based proportional sizing.

        Old (notional-based):
          our_notional = portfolio × (their_notional / their_acct)
          → capped by risk manager at portfolio × 15% = ~$168 notional
          → at 10x leverage that's only $16.8 of real margin

        New (margin-based):
          their_leverage  = cached notional / marginUsed  (or trader avg, max COPY_MAX_COPY_LEVERAGE)
          their_margin    = their_notional / their_leverage
          their_margin_pct = their_margin / their_acct
          our_margin      = portfolio × their_margin_pct        (same margin % of our account)
          our_notional    = our_margin × their_leverage         (scale back up with leverage)
          → risk manager caps at (portfolio × 15%) × leverage   = ~$168 × lev notional
          → actual margin committed stays ≤ $168 regardless of leverage

        Returns (our_notional_usd, their_leverage) — leverage goes into signal.meta
        so the risk manager can compute the correct margin-based cap.
        """
        if their_acct_val <= 0:
            return 0.0, 1.0  # fallback: risk manager uses max_size

        # Leverage: cached value or trader average (capped)
        their_lev = self._trader_position_leverages.get(
            (address, coin),
            min(float(trader.avg_leverage), settings.COPY_MAX_COPY_LEVERAGE),
        )
        their_lev = max(min(their_lev, settings.COPY_MAX_COPY_LEVERAGE), 1.0)

        # Their margin for this position
        their_margin     = their_notional / their_lev
        their_margin_pct = their_margin / their_acct_val

        # Proportional notional — no floor, no fake inflation.
        # If proportional size is below MIN_POSITION_NOTIONAL, return 0 to signal SKIP.
        # Flooring to a minimum wastes a slot on a coin the trader barely holds.
        # (e.g. fc667's XRP = 0.13% of $21M → $29 for us → not worth a slot)
        our_margin   = self._portfolio_usd * their_margin_pct
        our_notional = our_margin * their_lev

        from config import settings as _s
        if our_notional < _s.MIN_POSITION_NOTIONAL:
            return 0.0, their_lev   # notional too small — skip

        # Margin floor: catch high-leverage dust that passes the notional check.
        # e.g. a9b95f 20x ETH: our_notional=$165 (passes $50 floor) but our_margin=$8
        # → $8 of real capital is not worth a position slot.
        # At COPY_MIN_MARGIN_PCT=1%, floor = $11.20 on $1120 portfolio.
        min_margin = self._portfolio_usd * _s.COPY_MIN_MARGIN_PCT
        if our_margin < min_margin:
            return 0.0, their_lev   # margin too small — skip

        return our_notional, their_lev

    # ── Fill handler ───────────────────────────────────────────────────────────

    def _make_fill_handler(self, address: str):
        async def on_fill(msg: dict):
            await self._handle_fill(address, msg)
        on_fill.__name__ = f"fill_{address[:8]}"
        return on_fill

    async def _handle_fill(self, address: str, msg: dict):
        now   = time.time()
        fills = msg.get("data", {}).get("fills", [])
        if not fills:
            return

        trader = self._tracked.get(address)
        if not trader:
            return

        for fill in fills:
            try:
                fill_ts    = float(fill.get("time", now * 1000)) / 1000
                lag_ms     = (now - fill_ts) * 1000
                coin       = fill.get("coin", "")
                side       = fill.get("dir", fill.get("side", ""))
                sz         = float(fill.get("sz", 0))
                px         = float(fill.get("px", 0))
                closed_pnl = float(fill.get("closedPnl", 0))

                # Skip tokenized stocks / synthetics
                if coin.startswith(SKIP_PREFIXES):
                    continue

                # Parse direction
                if "Long" in side or side == "B":
                    direction = "long"
                elif "Short" in side or side == "A":
                    direction = "short"
                else:
                    continue

                is_close = closed_pnl != 0 or "Close" in side

                # ── EXIT: trader closed their position ────────────────────────
                if is_close:
                    # Guard against replayed fills on WebSocket reconnect.
                    # HL replays recent fills when we (re)subscribe to userFills.
                    # If the coin isn't in our tracker, the trader already closed
                    # BEFORE this session started — it's a stale replay, not a
                    # live close. Ignore it; our synced positions stay open until
                    # the trader closes again live (or guardian fires at 72h).
                    was_tracking = coin in self._trader_positions[address]
                    # Remove from position tracker so next open is treated as new
                    self._trader_positions[address].pop(coin, None)

                    if not was_tracking:
                        logger.debug(
                            f"[Leaderboard] Stale close ignored {coin} "
                            f"(not tracked this session — WS reconnect replay?)"
                        )
                        continue

                    # Dedup: TWAP exits fire many close fills; only relay once per 60s
                    last_exit = self._recent_exits.get(coin, 0)
                    if now - last_exit < 60:
                        logger.debug(
                            f"[Leaderboard] Exit dedup skip {coin} "
                            f"({now - last_exit:.0f}s ago)"
                        )
                        continue

                    self._recent_exits[coin] = now
                    logger.info(
                        f"[Leaderboard] COPY EXIT {coin} | lag={lag_ms:.0f}ms | "
                        f"from {address[:10]}..."
                    )
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

                # ── ENTRY: trader opened a position ───────────────────────────

                # Lag filter: entries need to be fast (exits can be slow — we'll follow)
                if lag_ms > settings.COPY_MAX_LAG_MS:
                    logger.debug(f"[Leaderboard] Entry lag {lag_ms:.0f}ms > {settings.COPY_MAX_LAG_MS}ms — skip")
                    continue

                # ── Position-aware dedup ──────────────────────────────────────
                # This is the core fix: if trader already holds this coin, skip.
                # Prevents copying every TWAP fill of a position build.
                existing_dir = self._trader_positions[address].get(coin)

                if existing_dir is not None:
                    if existing_dir == direction:
                        # Same-direction TWAP add — we already entered this one
                        logger.debug(
                            f"[Leaderboard] TWAP add skip {coin} {direction} "
                            f"from {address[:10]} (already tracked)"
                        )
                        continue
                    else:
                        # Direction FLIP: exit old side first, then enter new
                        logger.info(
                            f"[Leaderboard] Direction flip {coin} "
                            f"{existing_dir}->{direction} from {address[:10]}"
                        )
                        # Close signal direction = the direction WE ARE CLOSING.
                        # existing_dir is what they had (and what we copied), so
                        # we emit an exit for that direction.
                        if self._signal_queue:
                            await self._signal_queue.put(TradeSignal(
                                strategy="leaderboard",
                                coin=coin,
                                direction=existing_dir,   # close the position we copied
                                size_usd=0,
                                confidence=1.0,
                                meta={"action": "exit", "reason": "trader_flipped"},
                            ))
                        self._trader_positions[address].pop(coin, None)
                        # Fall through to register and enter the new direction

                # ── Per-trader slot cap ───────────────────────────────────────
                n_open = len(self._trader_positions[address])
                if n_open >= settings.COPY_MAX_POSITIONS_PER_TRADER:
                    logger.debug(
                        f"[Leaderboard] Trader cap {address[:10]} "
                        f"({n_open}/{settings.COPY_MAX_POSITIONS_PER_TRADER})"
                    )
                    continue

                # ── Margin-based proportional sizing ─────────────────────────
                their_notional = sz * px
                their_acct_val = self._trader_acct_values.get(address, 0)
                our_size, their_lev = self._compute_size(
                    address, coin, their_notional, their_acct_val, trader
                )

                # Skip coins where proportional size is below minimum threshold.
                # _compute_size returns 0.0 to signal "don't trade this coin".
                if our_size == 0:
                    logger.info(
                        f"[Leaderboard] SKIP {coin} — proportional size below "
                        f"${settings.MIN_POSITION_NOTIONAL} min "
                        f"(their={their_notional:,.0f}/acct={their_acct_val:,.0f})"
                    )
                    self._trader_positions[address].pop(coin, None)  # undo registration
                    continue

                # Register position in tracker (after size check — don't claim the slot for skipped coins)
                self._trader_positions[address][coin] = direction

                logger.info(
                    f"[Leaderboard] COPY {direction.upper()} {coin} | "
                    f"their=${their_notional:,.0f}/acct=${their_acct_val:,.0f} "
                    f"lev={their_lev:.0f}x -> us=${our_size:,.0f} "
                    f"(margin≈${our_size/their_lev:,.0f}) | "
                    f"lag={lag_ms:.0f}ms | {address[:10]}..."
                )

                signal = TradeSignal(
                    strategy="leaderboard",
                    coin=coin,
                    direction=direction,
                    size_usd=our_size,
                    confidence=1.0,
                    meta={
                        "source":         address,
                        "lag_ms":         lag_ms,
                        "their_size_usd": their_notional,
                        "leverage":       their_lev,
                        "action":         "enter",
                    },
                )
                if self._signal_queue:
                    await self._signal_queue.put(signal)

            except Exception as e:
                logger.error(f"[Leaderboard] Fill parse error: {e}")

    # ── Backfill on startup ────────────────────────────────────────────────────

    async def backfill_existing_positions(self):
        """
        Called once after refresh_leaderboard() + _refresh_account_values().
        Emits entry signals for positions traders hold NOW that we don't already have.

        Waits for _refresh_done event (set by _refresh_account_values when ALL traders
        have been fetched) before firing — eliminates the race where backfill ran before
        the last trader's positions were seeded, causing signals to be silently dropped.
        """
        if not self._tracked:
            return
        # Wait up to 60s for all account values to be fetched
        try:
            await asyncio.wait_for(self._refresh_done.wait(), timeout=60)
        except asyncio.TimeoutError:
            logger.warning("[Leaderboard] Backfill: refresh timed out after 60s — proceeding anyway")
        logger.info(f"[Leaderboard] Backfilling from {len(self._tracked)} traders...")
        total = 0

        for address, positions in self._trader_positions.items():
            if address not in self._tracked:
                continue
            their_acct_val = self._trader_acct_values.get(address, 0)

            for coin, direction in list(positions.items()):
                # Use cached position notional + leverage for margin-based sizing
                their_notional = self._trader_position_notionals.get((address, coin), 0)
                trader = self._tracked.get(address)
                if trader and their_acct_val > 0 and their_notional > 0:
                    our_size, their_lev = self._compute_size(
                        address, coin, their_notional, their_acct_val, trader
                    )
                    # Skip coins where proportional size is too small
                    if our_size == 0:
                        logger.info(
                            f"[Leaderboard] BACKFILL SKIP {coin} — "
                            f"proportional size below ${settings.MIN_POSITION_NOTIONAL} min "
                            f"(their={their_notional:,.0f}/acct={their_acct_val:,.0f})"
                        )
                        continue
                else:
                    our_size, their_lev = 0.0, 1.0   # risk manager uses max_size

                logger.info(
                    f"[Leaderboard] BACKFILL {direction.upper()} {coin} "
                    f"their=${their_notional:,.0f}/acct=${their_acct_val:,.0f} "
                    f"lev={their_lev:.0f}x -> us=${our_size:,.0f} "
                    f"(margin≈${our_size/max(their_lev,1):,.0f}) | from {address[:10]}..."
                )

                if self._signal_queue:
                    await self._signal_queue.put(TradeSignal(
                        strategy="leaderboard",
                        coin=coin,
                        direction=direction,
                        size_usd=our_size,
                        confidence=1.0,
                        meta={
                            "source":         address,
                            "action":         "enter",
                            "backfill":       True,
                            "their_size_usd": their_notional,
                            "leverage":       their_lev,
                        },
                    ))
                    total += 1

        logger.info(f"[Leaderboard] Backfill complete — {total} signals queued")

    def set_signal_queue(self, queue: asyncio.Queue):
        self._signal_queue = queue

    # ── REST fetch ─────────────────────────────────────────────────────────────

    async def _fetch_leaderboard(self) -> list[TrackedTrader]:
        """
        Load traders from config/traders.json — curated whitelist with per-trader metadata.
        Falls back to hardcoded list if file not found.
        """
        import json, os
        traders_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "config", "traders.json"
        )
        if os.path.exists(traders_file):
            with open(traders_file) as f:
                data = json.load(f)
            traders = []
            for entry in data:
                traders.append(TrackedTrader(
                    address=entry["address"],
                    realized_pnl=float(entry.get("alltime_pnl", 100_000)),
                    win_rate=float(entry.get("win_rate", 0.60)),
                    max_drawdown=float(entry.get("max_drawdown", 0.20)),
                    avg_leverage=float(entry.get("avg_leverage", 10)),
                    trade_count=int(entry.get("trade_count", 500)),
                    account_age_days=int(entry.get("account_age_days", 60)),
                ))
            logger.info(f"[Leaderboard] Loaded {len(traders)} traders from {traders_file}")
            return traders

        logger.warning("[Leaderboard] config/traders.json not found — using hardcoded fallback")

        # Curated conviction traders — macro accounts, long hold times, clean thesis
        # Updated: 2026-05-24
        # WHITELIST in .env overrides this list entirely
        KNOWN_TRADERS = [
            # a9b95f | $7M account | BTC SHORT + ETH SHORT + HYPE LONG | macro conviction
            ("0xa9b95f2a2e7ef219021efc5c04c32761b8553bbd", 2_000_000, 0.65, 0.15, 8, 500, 120),
            # 42b6d9 | $3.1M account | ZEC SHORT specialist | clean, single-thesis
            ("0x42b6d907f36255d48f70db8b4a2684088a162634", 1_000_000, 0.70, 0.12, 8, 500, 90),
            # fc667  | $20M account | BTC/ETH/SOL SHORT + HYPE LONG | macro bears
            ("0xfc667adba8d4837586078f4fdcdc29804337ca06", 900_000,   0.62, 0.16, 8, 2000, 100),
        ]

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
        rows = data if isinstance(data, list) else data.get("leaderboardRows", [])

        for entry in rows:
            try:
                perfs = entry.get("windowPerformances", [])
                stats = {}
                for window, s in perfs:
                    if window == "allTime":
                        stats = s
                        break
                if not stats and perfs:
                    stats = perfs[0][1] if isinstance(perfs[0], list) else {}

                pnl      = float(stats.get("pnl", entry.get("pnl", 0)))
                win_rate = float(stats.get("winRate", 0.5))
                drawdown = float(stats.get("maxDrawdown", entry.get("maxDrawdown", 0.5)))
                leverage = float(stats.get("avgLeverage", entry.get("avgLeverage", 5)))
                trades   = int(stats.get("tradeCount", entry.get("tradeCount", 0)))
                age      = int(entry.get("accountAgeDays", 30))
                address  = entry.get("ethAddress", entry.get("user", ""))

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
