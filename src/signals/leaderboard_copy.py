"""
Strategy 2: Leaderboard Alpha Capture  (STATE-BASED reconcile)
───────────────────────────────────────────────────────────────
Polls each whitelisted trader's NET position (clearinghouseState) on a fixed
interval and mirrors only *real* position changes. We do NOT react to the fill
stream any more.

WHY (the bug this replaces):
  The old engine subscribed to userFills and acted on every fill. These traders
  TWAP enormously (60–285 fills per position episode) and mix in trims/scalps,
  so every partial-close fill (closedPnl≠0) was misread as a full exit → we
  closed → their next add re-opened → thrash. Result: 238 fills / 48h, fees
  ($18.85) larger than the gross trading loss. We captured none of their
  multi-day conviction holds.

HOW (now):
  Every COPY_RECONCILE_INTERVAL_S we:
    1. Rebuild each trader's net positions from clearinghouseState (fresh — closed
       coins disappear), with cached notional/leverage/acct-value.
    2. Build a DESIRED portfolio:
         • specialist coins (ZEC→42b6d9, SOL→6bea81, LIT→a4dedd) route to that
           trader only;
         • generalist coins require all holders to agree on direction, else SKIP
           (contested, e.g. fc667 BTC-long vs a9b95f BTC-short);
         • pick the highest-conviction holder (margin as % of their account);
         • size via margin-based proportional _compute_size (skips dust).
    3. Diff DESIRED vs what we actually hold (leaderboard/synced positions) and
       emit entry / exit / flip signals only on net changes. Holding a position
       the trader still holds = do nothing (capture the trend).

  Declarative + idempotent: a missed/failed action is simply re-applied next cycle.
"""
import asyncio
import os
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

SKIP_PREFIXES = ("xyz:", "@", "km:", "k:")   # tokenized stocks / synthetic / spot assets


@dataclass
class TrackedTrader:
    address: str
    realized_pnl: float
    win_rate: float
    max_drawdown: float
    avg_leverage: float
    trade_count: int
    account_age_days: int
    specialty: Optional[str] = None   # coin this trader is the designated specialist for


class LeaderboardCopier:
    def __init__(self, store: MarketStore, feed):
        self.store = store
        self.feed = feed
        self._tracked: dict[str, TrackedTrader] = {}
        self._signal_queue: Optional[asyncio.Queue] = None

        # Read-only handle to the risk manager so reconcile can see what WE hold.
        # Injected by main.py after construction.
        self.risk = None

        # Per-trader NET state, fully rebuilt each poll from clearinghouseState:
        #   _trader_positions[addr]          = {coin -> "long"|"short"}
        #   _trader_position_notionals[k]    = their_notional_usd        (k = (addr, coin))
        #   _trader_position_leverages[k]    = leverage multiplier        (k = (addr, coin))
        #   _trader_acct_values[addr]        = account equity
        self._trader_positions: dict[str, dict[str, str]] = defaultdict(dict)
        self._trader_acct_values: dict[str, float] = {}
        self._trader_position_notionals: dict[tuple, float] = {}
        self._trader_position_leverages: dict[tuple, float] = {}

        # coin -> specialist address (built from traders.json `specialty`)
        self._specialist: dict[str, str] = {}

        # Cache portfolio size — read once at init
        self._portfolio_usd: float = float(os.getenv("PORTFOLIO_USD", 1000))

        # Set when the first account refresh completes (kept for compatibility / health).
        self._refresh_done: asyncio.Event = asyncio.Event()

        # Guards against overlapping reconcile runs if one is slow.
        self._reconcile_lock = asyncio.Lock()

        # Coins we've already logged as contested — so we log only on change, not every poll.
        self._prev_contested: set[str] = set()

    # ── Leaderboard / trader-list loading ───────────────────────────────────────

    async def refresh_leaderboard(self):
        """
        Load the whitelisted trader list (from traders.json) and rebuild the
        specialist routing map. No WebSocket subscriptions — reconcile() polls.
        """
        raw = await self._fetch_leaderboard()
        if not raw:
            logger.warning("[Leaderboard] No traders returned")
            return

        if settings.COPY_TRADER_WHITELIST:
            raw = [t for t in raw if t.address.lower() in settings.COPY_TRADER_WHITELIST]
            logger.info(
                f"[Leaderboard] Whitelist — {len(raw)} traders: "
                + ", ".join(a[:10] + "..." for a in settings.COPY_TRADER_WHITELIST)
            )

        self._tracked = {t.address: t for t in raw}

        # Rebuild specialist routing: coin -> address
        self._specialist = {
            t.specialty.upper(): t.address
            for t in raw if t.specialty
        }
        logger.info(
            f"[Leaderboard] Tracking {len(self._tracked)} traders | "
            f"specialists: {self._specialist}"
        )

    # ── Live state fetch ────────────────────────────────────────────────────────

    async def _refresh_account_values(self):
        """
        Rebuild every tracked trader's NET position snapshot from clearinghouseState.
        Fully replaces prior state so closed positions vanish. Failure is non-fatal —
        a trader that errors keeps its previous snapshot for this cycle.
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

                    acct_val = float(state.get("marginSummary", {}).get("accountValue", 0))
                    if acct_val > 0:
                        self._trader_acct_values[address] = acct_val

                    # Build a FRESH snapshot for this trader, then swap it in.
                    fresh: dict[str, str] = {}
                    for ap in state.get("assetPositions", []):
                        pos  = ap.get("position", {})
                        szi  = float(pos.get("szi", 0))
                        coin = pos.get("coin", "")
                        if szi == 0 or coin.startswith(SKIP_PREFIXES):
                            continue
                        notional = abs(float(pos.get("positionValue") or 0))
                        if notional < 50:   # dust — ignore
                            continue
                        fresh[coin] = "long" if szi > 0 else "short"
                        self._trader_position_notionals[(address, coin)] = notional
                        margin_used = float(pos.get("marginUsed") or 0)
                        if margin_used > 0:
                            lev = min(notional / margin_used, settings.COPY_MAX_COPY_LEVERAGE)
                            self._trader_position_leverages[(address, coin)] = round(lev, 1)

                    # Drop cached notionals/leverages for coins this trader no longer holds
                    for (a, c) in list(self._trader_position_notionals.keys()):
                        if a == address and c not in fresh:
                            self._trader_position_notionals.pop((a, c), None)
                            self._trader_position_leverages.pop((a, c), None)
                    self._trader_positions[address] = fresh

                    logger.debug(
                        f"[Leaderboard] {address[:10]}... acct=${acct_val:,.0f} "
                        f"net_pos={fresh}"
                    )
                    await asyncio.sleep(0.3)

                except Exception as e:
                    logger.debug(f"[Leaderboard] acct refresh error {address[:10]}: {e}")

        self._refresh_done.set()

    # ── Sizing helper (margin-based) ─────────────────────────────────────────────

    def _compute_size(
        self,
        address: str,
        coin: str,
        their_notional: float,
        their_acct_val: float,
        trader: "TrackedTrader",
    ) -> tuple[float, float]:
        """
        Margin-based proportional sizing. Returns (our_notional_usd, their_leverage).
        Returns (0.0, lev) to signal SKIP (notional or margin below floor).
        """
        if their_acct_val <= 0:
            return 0.0, 1.0

        their_lev = self._trader_position_leverages.get(
            (address, coin),
            min(float(trader.avg_leverage), settings.COPY_MAX_COPY_LEVERAGE),
        )
        their_lev = max(min(their_lev, settings.COPY_MAX_COPY_LEVERAGE), 1.0)

        their_margin     = their_notional / their_lev
        their_margin_pct = their_margin / their_acct_val

        our_margin   = self._portfolio_usd * their_margin_pct
        our_notional = our_margin * their_lev

        from config import settings as _s
        if our_notional < _s.MIN_POSITION_NOTIONAL:
            return 0.0, their_lev   # notional too small — skip

        min_margin = self._portfolio_usd * _s.COPY_MIN_MARGIN_PCT
        if our_margin < min_margin:
            return 0.0, their_lev   # margin too small — skip

        return our_notional, their_lev

    def _conviction(self, address: str, coin: str, notional: float, acct: float) -> float:
        """Their committed margin as a fraction of their account — used to rank holders."""
        if acct <= 0:
            return 0.0
        lev = self._trader_position_leverages.get(
            (address, coin),
            min(float(self._tracked[address].avg_leverage), settings.COPY_MAX_COPY_LEVERAGE),
        )
        lev = max(min(lev, settings.COPY_MAX_COPY_LEVERAGE), 1.0)
        return (notional / lev) / acct

    # ── Desired portfolio (routing + conflict resolution) ────────────────────────

    def _build_desired(self) -> dict[str, dict]:
        """
        Decide the portfolio we WANT to hold right now, one entry per coin:
            coin -> {dir, source, their_notional, their_acct, lev, size}

        Routing:
          • specialist coin → only that trader's position counts (skip if they're flat)
          • generalist coin → all holders must agree on direction, else SKIP (contested)
          • among eligible holders, pick the highest-conviction one
          • size via _compute_size; drop coins that size to 0 (dust)
        """
        # coin -> list of (addr, dir, notional, acct)
        holders: dict[str, list] = defaultdict(list)
        for address, positions in self._trader_positions.items():
            if address not in self._tracked:
                continue
            acct = self._trader_acct_values.get(address, 0)
            if acct <= 0:
                continue
            for coin, direction in positions.items():
                notional = self._trader_position_notionals.get((address, coin), 0)
                if notional > 0:
                    holders[coin].append((address, direction, notional, acct))

        desired: dict[str, dict] = {}
        contested_now: set[str] = set()
        for coin, hs in holders.items():
            spec_addr = self._specialist.get(coin.upper())
            if spec_addr:
                cand = [h for h in hs if h[0] == spec_addr]
                if not cand:
                    continue   # specialist is flat → we don't hold this coin
            else:
                directions = {h[1] for h in hs}
                if len(directions) > 1:
                    contested_now.add(coin)
                    if coin not in self._prev_contested:   # log only when newly contested
                        logger.info(
                            f"[Reconcile] {coin} CONTESTED "
                            f"{[(a[:6], d) for a, d, _, _ in hs]} — skip"
                        )
                    continue
                cand = hs

            # Highest-conviction holder wins the coin
            best = max(cand, key=lambda h: self._conviction(h[0], coin, h[2], h[3]))
            addr, direction, notional, acct = best
            trader = self._tracked.get(addr)
            our_size, lev = self._compute_size(addr, coin, notional, acct, trader)
            if our_size == 0:
                continue   # below dust floors — skip

            desired[coin] = {
                "dir": direction, "source": addr, "their_notional": notional,
                "their_acct": acct, "lev": lev, "size": our_size,
            }
        self._prev_contested = contested_now
        return desired

    # Re-enter a held position if it's below this fraction of its capped target size
    # (catches startup-synced positions the old engine left under-sized).
    _RESIZE_MIN_RATIO = 0.6

    def _capped_target(self, d: dict) -> float:
        """Desired notional AFTER the risk manager's per-position margin cap, so the
        resize check compares like-for-like and never oscillates."""
        cap = self._portfolio_usd * settings.MAX_POSITION_SIZE_PCT * max(d["lev"], 1.0)
        return min(d["size"], cap)

    # ── Reconcile loop ───────────────────────────────────────────────────────────

    async def reconcile(self):
        """
        Poll all traders, compute the desired portfolio, and emit entry/exit/flip
        signals to converge our actual positions toward it. Idempotent.
        """
        if self._reconcile_lock.locked():
            logger.debug("[Reconcile] previous run still in progress — skipping tick")
            return
        async with self._reconcile_lock:
            if not self._tracked or self._signal_queue is None or self.risk is None:
                return

            await self._refresh_account_values()

            # Sync our in-memory book with reality FIRST: drop any position closed
            # outside the bot (manual close, liquidation, native SL/TP). One fetch of our
            # wallet state gives both the open coins and live account equity.
            actual, equity = await self._fetch_our_state()
            if actual is not None:
                self.risk.drop_phantoms(actual)
            if equity:
                # Always feed live equity to the risk manager so the daily-loss halt is
                # drawdown-aware (it measures live equity vs the day-start baseline, which
                # captures UNREALIZED losses these buy-and-hold traders rarely realize).
                self.risk.update_equity(equity)
                # Auto-compound is a separate decision: only resize off live equity when
                # enabled. PORTFOLIO_USD stays the seed otherwise.
                if settings.PORTFOLIO_COMPOUND:
                    self._portfolio_usd = equity
                    self.risk.portfolio_value = equity

            desired = self._build_desired()

            # What we actually hold from this strategy (copy + startup-synced positions).
            held = {
                p.coin: p for p in self.risk.open_positions
                if p.strategy in ("leaderboard", "synced")
            }

            entries = exits = flips = resizes = 0

            # ── Entries / flips / resizes ─────────────────────────────────────────
            # We mirror the trader and HOLD as long as they hold — no profit-taking
            # overlay. These are slow macro holders; their edge is the multi-week move,
            # so banking a small gain and re-entering only churns fees and forfeits the
            # run. Exits come from the trader closing/flipping or the guardian backstop.
            for coin, d in desired.items():
                cur = held.get(coin)
                if cur is None:
                    await self._emit_entry(coin, d)
                    entries += 1
                elif cur.direction != d["dir"]:
                    # Trader flipped: close our side, then open the new side.
                    await self._emit_exit(coin, cur.direction, "trader_flipped")
                    await self._emit_entry(coin, d)
                    flips += 1
                elif cur.size_usd < self._RESIZE_MIN_RATIO * self._capped_target(d):
                    # Materially under-sized vs target — close & re-enter at correct size.
                    logger.info(
                        f"[Reconcile] RESIZE {coin} ${cur.size_usd:,.0f} -> "
                        f"~${self._capped_target(d):,.0f} (under-sized)"
                    )
                    await self._emit_exit(coin, cur.direction, "resize")
                    await self._emit_entry(coin, d)
                    resizes += 1
                # else: correct coin/direction/size → HOLD (this is the whole point)

            # Exits: we hold it but no trader wants it any more
            for coin, p in held.items():
                if coin not in desired:
                    await self._emit_exit(coin, p.direction, "trader_closed")
                    exits += 1

            if entries or exits or flips or resizes:
                logger.info(
                    f"[Reconcile] desired={len(desired)} held={len(held)} | "
                    f"+{entries} entries, {exits} exits, {flips} flips, "
                    f"{resizes} resizes"
                )
            else:
                logger.debug(
                    f"[Reconcile] in sync — desired={len(desired)} held={len(held)}"
                )

    async def _emit_entry(self, coin: str, d: dict):
        logger.info(
            f"[Reconcile] ENTER {d['dir'].upper()} {coin} ${d['size']:,.0f} "
            f"(margin≈${d['size'] / max(d['lev'], 1):,.0f}) lev={d['lev']:.0f}x | "
            f"their=${d['their_notional']:,.0f}/acct=${d['their_acct']:,.0f} | "
            f"src={d['source'][:10]}..."
        )
        await self._signal_queue.put(TradeSignal(
            strategy="leaderboard",
            coin=coin,
            direction=d["dir"],
            size_usd=d["size"],
            confidence=1.0,
            meta={
                "source":         d["source"],
                "leverage":       d["lev"],
                "their_size_usd": d["their_notional"],
                "action":         "enter",
            },
        ))

    async def _emit_exit(self, coin: str, held_direction: str, reason: str):
        # Exit signal direction = the side WE HOLD (the one being closed) — see
        # executor._close_position matching convention.
        logger.info(f"[Reconcile] EXIT {coin} {held_direction} — {reason}")
        await self._signal_queue.put(TradeSignal(
            strategy="leaderboard",
            coin=coin,
            direction=held_direction,
            size_usd=0,
            confidence=1.0,
            meta={"action": "exit", "reason": reason},
        ))

    async def _fetch_our_state(self) -> tuple[Optional[set], Optional[float]]:
        """
        Our wallet's currently-open perp coins AND account equity, straight from HL.
        Returns (None, None) on any error so callers skip pruning/compounding rather
        than acting on a bad read.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_REST,
                    json={"type": "clearinghouseState", "user": settings.HL_WALLET_ADDRESS},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    st = await resp.json(content_type=None)
            if "marginSummary" not in st:          # malformed/empty response — don't act
                return None, None
            coins = {
                ap["position"]["coin"]
                for ap in st.get("assetPositions", [])
                if float(ap["position"].get("szi", 0)) != 0
            }
            equity = float(st["marginSummary"].get("accountValue", 0)) or None
            return coins, equity
        except Exception as e:
            logger.debug(f"[Reconcile] our-state fetch failed: {e}")
            return None, None

    def set_signal_queue(self, queue: asyncio.Queue):
        self._signal_queue = queue

    # ── Trader-list source (unchanged) ───────────────────────────────────────────

    async def _fetch_leaderboard(self) -> list[TrackedTrader]:
        """Load traders from config/traders.json. Falls back to a hardcoded list."""
        import json
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
                    specialty=entry.get("specialty") or None,
                ))
            logger.info(f"[Leaderboard] Loaded {len(traders)} traders from {traders_file}")
            return traders

        logger.warning("[Leaderboard] config/traders.json not found — using hardcoded fallback")
        KNOWN_TRADERS = [
            ("0xa9b95f2a2e7ef219021efc5c04c32761b8553bbd", 2_000_000, 0.65, 0.15, 8, 500, 120, None),
            ("0x42b6d907f36255d48f70db8b4a2684088a162634", 1_000_000, 0.70, 0.12, 8, 500, 90, "ZEC"),
            ("0xfc667adba8d4837586078f4fdcdc29804337ca06", 900_000,   0.62, 0.16, 8, 2000, 100, None),
        ]
        traders = []
        for addr, pnl, wr, dd, lev, tc, age, spec in KNOWN_TRADERS:
            traders.append(TrackedTrader(
                address=addr, realized_pnl=pnl, win_rate=wr, max_drawdown=dd,
                avg_leverage=lev, trade_count=tc, account_age_days=age, specialty=spec,
            ))
        return traders
