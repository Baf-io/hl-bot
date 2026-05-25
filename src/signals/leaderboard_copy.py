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
    score: float = 0.0


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

        # Coins we trail-exited → locked from re-entry until the trader's position resets.
        # coin -> the direction we were in when we trail-exited.
        self._trail_locked: dict[str, str] = {}

        # Entry debounce: coin -> consecutive reconcile ticks it's been desired-but-not-held.
        # We only open once the streak clears COPY_ENTRY_DEBOUNCE_TICKS (kills fleeting copies).
        self._pending_entry: dict[str, int] = defaultdict(int)

        # Zero-copy-lag (Stage 2) fresh-entry state:
        #   _prev_trader_positions = last poll's {addr -> {coin -> dir}}, to detect transitions
        #   _fresh_opens           = coin -> {"px": observed open price, "ts": monotonic}
        #   _seeded                = first poll establishes the baseline (adopts nothing stale)
        self._prev_trader_positions: dict[str, dict[str, str]] = {}
        self._fresh_opens: dict[str, dict] = {}
        self._seeded: bool = False

        # Daily-ATR cache for scale-out levels: coin -> (atr_pct, monotonic_ts).
        # atr_pct is ATR(ATR_PERIOD) over daily candles divided by last close (a fraction).
        self._atr_cache: dict[str, tuple[float, float]] = {}

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

        for t in raw:
            t.score = self._score(t)

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

    # ── Filtering / scoring (unchanged) ─────────────────────────────────────────

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

    # ── Sizing helpers ───────────────────────────────────────────────────────────

    def _lev_for(self, address: str, coin: str) -> float:
        """Capped per-position leverage: cached notional/marginUsed, else trader avg."""
        lev = self._trader_position_leverages.get(
            (address, coin),
            min(float(self._tracked[address].avg_leverage), settings.COPY_MAX_COPY_LEVERAGE),
        )
        return max(min(lev, settings.COPY_MAX_COPY_LEVERAGE), 1.0)

    def _conviction(self, address: str, coin: str, notional: float, acct: float) -> float:
        """Their committed margin as a fraction of their account — gates + ranks holders."""
        if acct <= 0:
            return 0.0
        return (notional / self._lev_for(address, coin)) / acct

    def _atr_cached(self, coin: str) -> Optional[float]:
        """Synchronous read of the ATR cache (no fetch). Pre-warmed in reconcile via
        _atr_pct before _build_desired so the vol-scaled-leverage step can stay sync."""
        c = self._atr_cache.get(coin)
        return c[0] if c else None

    def _detect_fresh_opens(self):
        """
        Zero-copy-lag: diff each trader's current positions vs last poll. A coin that newly
        appears (flat→position) or flips direction = a FRESH open — record the observed price
        and timestamp so an entry can fire near the trader's own entry. The FIRST call only
        seeds the baseline (so a restart never adopts positions the trader already held).
        """
        now = time.monotonic()
        for c in list(self._fresh_opens):                       # expire stale opportunities
            if now - self._fresh_opens[c]["ts"] > settings.FRESH_ENTRY_EXPIRE_S:
                self._fresh_opens.pop(c, None)
        if not self._seeded:
            self._prev_trader_positions = {a: dict(p) for a, p in self._trader_positions.items()}
            self._seeded = True
            logger.info("[Reconcile] fresh-entry baseline seeded — existing trader holds are NOT adopted")
            return
        for addr, positions in self._trader_positions.items():
            if addr not in self._tracked:
                continue
            spec = self._tracked[addr].specialty
            prev = self._prev_trader_positions.get(addr, {})
            for coin, direction in positions.items():
                if coin in settings.TRACKER_COINS:
                    continue
                if spec and coin.upper() != spec.upper():
                    continue
                if prev.get(coin) != direction:                 # newly opened OR flipped
                    px = self.store.latest_mid(coin) if self.store else None
                    if px:
                        self._fresh_opens[coin] = {"px": px, "ts": now}
                        logger.info(
                            f"[Reconcile] 🆕 FRESH open {addr[:6]} {direction} {coin} @ ${px} "
                            f"— entry window open (±{settings.FRESH_ENTRY_MAX_ATR}×ATR)"
                        )
        self._prev_trader_positions = {a: dict(p) for a, p in self._trader_positions.items()}

    def _is_fresh_entry(self, coin: str) -> bool:
        """True only if `coin` was freshly opened by the trader AND current price is still within
        FRESH_ENTRY_MAX_ATR of their observed open — i.e. we can enter near their price, no lag."""
        fo = self._fresh_opens.get(coin)
        if not fo:
            return False
        px = self.store.latest_mid(coin) if self.store else None
        atr = self._atr_cached(coin)
        if not px or not atr or atr <= 0:
            return False
        return abs(px - fo["px"]) <= settings.FRESH_ENTRY_MAX_ATR * atr * fo["px"]

    def _vol_capped_lev(self, coin: str, base_lev: float) -> float:
        """Tune leverage to the coin's ATR so the hard -STOP_LOSS_MARGIN_PCT stop lands
        >= STOP_NOISE_ATR daily-ATRs away (a real move, not noise). Caps below base_lev on
        volatile coins; leaves low-vol coins at base. Falls back to base_lev if no ATR."""
        if not settings.VOL_SCALED_LEV:
            return base_lev
        atr = self._atr_cached(coin)
        if not atr or atr <= 0:
            return base_lev
        lev_cap = settings.STOP_LOSS_MARGIN_PCT / (settings.STOP_NOISE_ATR * atr)
        return max(1.0, min(base_lev, lev_cap))

    # ── Daily-ATR for volatility-normalized scale-out ────────────────────────────

    async def _atr_pct(self, coin: str) -> Optional[float]:
        """
        Daily ATR(ATR_PERIOD) as a fraction of the last close, e.g. 0.06 = 6%/day.
        Cached per coin for ATR_REFRESH_S (ATR moves slowly). Returns None on any
        failure or insufficient history — callers must SKIP scale-out rather than act
        on a missing volatility estimate.
        """
        now = time.monotonic()
        cached = self._atr_cache.get(coin)
        if cached and (now - cached[1]) < settings.ATR_REFRESH_S:
            return cached[0]

        period = settings.ATR_PERIOD
        end = int(time.time() * 1000)
        start = end - (period + 5) * 86_400_000   # a few extra days of headroom
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    HL_REST,
                    json={"type": "candleSnapshot", "req": {
                        "coin": coin, "interval": "1d",
                        "startTime": start, "endTime": end,
                    }},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    candles = await resp.json(content_type=None)
            if not isinstance(candles, list) or len(candles) < period + 1:
                return None
            # True range per day: max(h-l, |h-prevClose|, |l-prevClose|)
            trs = []
            for i in range(1, len(candles)):
                h = float(candles[i]["h"]); l = float(candles[i]["l"])
                pc = float(candles[i - 1]["c"])
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr = sum(trs[-period:]) / period
            last_close = float(candles[-1]["c"])
            if last_close <= 0:
                return None
            atr_pct = atr / last_close
            self._atr_cache[coin] = (atr_pct, now)
            return atr_pct
        except Exception as e:
            logger.debug(f"[Reconcile] ATR fetch failed for {coin}: {e}")
            return None

    # ── Desired portfolio (routing + conflict resolution) ────────────────────────

    def _build_desired(self) -> dict[str, dict]:
        """
        Decide the portfolio we WANT to hold, in two stages.

        1. ROUTING (which coins / which direction):
           • Only positions that are a real conviction bet FOR THE TRADER count
             (their margin on the coin >= COPY_MIN_CONVICTION_PCT of their account).
             Dabbles are ignored — not copied AND not allowed to vote on direction.
           • specialist coin → only that trader's (gated) position counts.
           • generalist coin → gated holders must agree on direction, else SKIP (contested).
           • highest-conviction eligible holder wins the coin.

        2. SIZING (how big — DECOUPLED from the trader's account size):
           • equal-weight OUR capital to COPY_TARGET_DEPLOY across the chosen coins,
             capped at MAX_POSITION_SIZE_PCT each; notional = margin × their leverage.
           This is what lets us copy big funds whose bets are a small % of their account.
        """
        # ── Stage 1: conviction-gated holders per coin ───────────────────────────
        holders: dict[str, list] = defaultdict(list)   # coin -> [(addr, dir, conviction)]
        for address, positions in self._trader_positions.items():
            if address not in self._tracked:
                continue
            acct = self._trader_acct_values.get(address, 0)
            if acct <= 0:
                continue
            # Specialists are PINNED to their one coin: if a trader has a `specialty`, we only
            # copy that coin from them (their other positions are deliberately ignored). This
            # is how a9b95f→HYPE-only and feec88→SOL-only is enforced.
            spec = self._tracked[address].specialty
            for coin, direction in positions.items():
                if coin in settings.TRACKER_COINS:
                    continue   # reserved for the manual lev-tracker sleeve — never copy it
                if spec and coin.upper() != spec.upper():
                    continue   # specialist: only their designated coin counts
                notional = self._trader_position_notionals.get((address, coin), 0)
                if notional <= 0:
                    continue
                cv = self._conviction(address, coin, notional, acct)
                if cv < settings.COPY_MIN_CONVICTION_PCT:
                    continue   # not a real bet for them — ignore (and it can't vote)
                holders[coin].append((address, direction, cv))

        chosen: dict[str, tuple] = {}   # coin -> (addr, dir, lev)
        contested_now: set[str] = set()
        for coin, hs in holders.items():
            spec_addr = self._specialist.get(coin.upper())
            if spec_addr:
                cand = [h for h in hs if h[0] == spec_addr]
                if not cand:
                    continue   # specialist holds no conviction position here
            else:
                if len({h[1] for h in hs}) > 1:
                    contested_now.add(coin)
                    if coin not in self._prev_contested:   # log only when newly contested
                        logger.info(
                            f"[Reconcile] {coin} CONTESTED "
                            f"{[(a[:6], d) for a, d, _ in hs]} — skip"
                        )
                    continue
                cand = hs
            addr, direction, _ = max(cand, key=lambda h: h[2])
            chosen[coin] = (addr, direction, self._vol_capped_lev(coin, self._lev_for(addr, coin)))
        self._prev_contested = contested_now

        # ── Stage 2: equal-weight sizing to target deployment ────────────────────
        n = len(chosen)
        if n == 0:
            return {}
        per_margin = min(
            self._portfolio_usd * settings.COPY_TARGET_DEPLOY / n,
            self._portfolio_usd * settings.MAX_POSITION_SIZE_PCT,
        )
        desired: dict[str, dict] = {}
        for coin, (addr, direction, lev) in chosen.items():
            our_notional = per_margin * lev
            if our_notional < settings.MIN_POSITION_NOTIONAL:
                continue   # sanity floor (shouldn't trigger at target deployment)
            desired[coin] = {
                "dir": direction, "source": addr,
                "their_notional": self._trader_position_notionals.get((addr, coin), 0),
                "their_acct": self._trader_acct_values.get(addr, 0),
                "lev": lev, "size": our_notional,
            }
        return desired

    # Re-enter a held position if it's outside this band around its capped target size.
    # Bidirectional: grows under-sized (e.g. startup-synced) AND trims over-sized (e.g.
    # legacy proportional sizing) so the book converges to equal-weight. Wide band so it
    # rebalances once and doesn't oscillate on small equity/position-count drift.
    _RESIZE_MIN_RATIO = 0.6
    _RESIZE_MAX_RATIO = 1.6

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
            # outside the bot (manual close, liquidation, native SL/TP). Also auto-compound
            # sizing off live equity. Both come from one fetch of our wallet state.
            actual, equity = await self._fetch_our_state()
            if actual is not None:
                self.risk.drop_phantoms(actual)
            if equity and settings.PORTFOLIO_COMPOUND:
                self._portfolio_usd = equity
                self.risk.portfolio_value = equity

            # Pre-warm the ATR cache for every coin a trader holds, so the sync vol-scaled-
            # leverage step (_build_desired) and the stop/trail (_ride_winners) can read it
            # without awaiting. _atr_pct caches for ATR_REFRESH_S, so this is ~1 call/coin/hr.
            for c in {coin for pos in self._trader_positions.values() for coin in pos
                      if coin not in settings.TRACKER_COINS}:
                await self._atr_pct(c)

            # Zero-copy-lag: detect fresh trader opens this poll (gates new entries below)
            self._detect_fresh_opens()

            desired = self._build_desired()

            # What we actually hold from this strategy (copy + startup-synced positions).
            held = {
                p.coin: p for p in self.risk.open_positions
                if p.strategy in ("leaderboard", "synced")
            }

            entries = exits = flips = resizes = trails = 0

            # ── Profit-taking on copied holds ─────────────────────────────────────
            # The traders only buy-and-hold; this banks gains they'd otherwise give back.
            # Preferred path: SCALE-OUT (2 tranches, ATR-normalized, leverage-aware) —
            # see _scale_out. Falls back to the flat TRAIL_* logic when scale-out is off.
            if settings.RIDE_WINNERS_ENABLED and self.store:
                trails += await self._ride_winners(held)
            elif settings.SCALEOUT_ENABLED and self.store:
                trails += await self._scale_out(held)
            elif settings.TRAIL_ENABLED and self.store:
                for coin, p in list(held.items()):
                    px = self.store.latest_mid(coin)
                    if not px or not p.entry_price:
                        continue
                    exc = ((px - p.entry_price) / p.entry_price) if p.direction == "long" \
                          else ((p.entry_price - px) / p.entry_price)
                    if exc > p.peak_price_pct:
                        p.peak_price_pct = exc
                    if (p.peak_price_pct >= settings.TRAIL_ARM_PCT
                            and exc <= p.peak_price_pct * (1 - settings.TRAIL_GIVEBACK)):
                        logger.info(
                            f"[Reconcile] TRAIL EXIT {coin} {p.direction} | "
                            f"peak +{p.peak_price_pct:.1%} -> now +{exc:.1%}"
                        )
                        await self._emit_exit(coin, p.direction, "trail")
                        self._trail_locked[coin] = p.direction   # lock until trader resets
                        del held[coin]
                        trails += 1

            # ── Entries / flips / resizes (respecting trail-lock suppression) ─────
            for coin, d in desired.items():
                if coin in self._trail_locked:
                    if self._trail_locked[coin] == d["dir"]:
                        continue                       # still locked — trader hasn't reset
                    del self._trail_locked[coin]       # trader flipped — re-arm
                cur = held.get(coin)
                if cur is None:
                    if settings.FRESH_ENTRY_ONLY:
                        # Zero-copy-lag: ONLY open on a fresh trader open near their price.
                        # A position the trader already held (stale) is never adopted.
                        if not self._is_fresh_entry(coin):
                            logger.debug(f"[Reconcile] {coin} desired but not a fresh open near price — skip (no late adoption)")
                            continue
                        self._fresh_opens.pop(coin, None)   # consumed
                        logger.info(f"[Reconcile] ✅ fresh entry {coin} near trader's open — copy-lag avoided")
                        await self._emit_entry(coin, d)
                        entries += 1
                    else:
                        # Legacy debounce path: trader must hold a NEW coin COPY_ENTRY_DEBOUNCE_TICKS
                        # reconciles before we copy it (kills fleeting/scalp positions).
                        self._pending_entry[coin] += 1
                        if self._pending_entry[coin] < settings.COPY_ENTRY_DEBOUNCE_TICKS:
                            logger.debug(
                                f"[Reconcile] {coin} debounce {self._pending_entry[coin]}/"
                                f"{settings.COPY_ENTRY_DEBOUNCE_TICKS} — waiting"
                            )
                            continue
                        self._pending_entry.pop(coin, None)
                        await self._emit_entry(coin, d)
                        entries += 1
                elif cur.direction != d["dir"]:
                    # Trader flipped: close our side, then open the new side.
                    await self._emit_exit(coin, cur.direction, "trader_flipped")
                    await self._emit_entry(coin, d)
                    flips += 1
                elif settings.RESIZE_ENABLED and not cur.scaled_out and not (
                        self._RESIZE_MIN_RATIO * self._capped_target(d)
                        <= cur.size_usd
                        <= self._RESIZE_MAX_RATIO * self._capped_target(d)):
                    # Materially off target — close & re-enter at the equal-weight target size.
                    # DISABLED by default (RESIZE_ENABLED=false): the close-and-reopen locked
                    # running losses + paid double fees (−$79 of the first −$133). Positions now
                    # ride at entry size until a real exit/flip/stop. (Skip scaled-out runners.)
                    tgt = self._capped_target(d)
                    logger.info(
                        f"[Reconcile] RESIZE {coin} ${cur.size_usd:,.0f} -> ~${tgt:,.0f} "
                        f"({'under' if cur.size_usd < tgt else 'over'}-sized)"
                    )
                    await self._emit_exit(coin, cur.direction, "resize")
                    await self._emit_entry(coin, d)
                    resizes += 1
                # else: correct coin/direction → HOLD (this is the whole point)

            # Exits: we hold it but no trader wants it any more
            for coin, p in held.items():
                if coin not in desired:
                    await self._emit_exit(coin, p.direction, "trader_closed")
                    exits += 1

            # Re-arm trail-locks once the trader no longer holds the coin
            for coin in list(self._trail_locked):
                if coin not in desired:
                    del self._trail_locked[coin]

            # Reset entry-debounce streaks for coins we no longer desire OR already hold
            # (so a coin must build a fresh streak each time it reappears as a new entry).
            for coin in list(self._pending_entry):
                if coin not in desired or coin in held:
                    self._pending_entry.pop(coin, None)

            if entries or exits or flips or resizes or trails:
                logger.info(
                    f"[Reconcile] desired={len(desired)} held={len(held)} | "
                    f"+{entries} entries, {exits} exits, {flips} flips, "
                    f"{resizes} resizes, {trails} trails"
                )
            else:
                logger.debug(
                    f"[Reconcile] in sync — desired={len(desired)} held={len(held)}"
                )

    async def _ride_winners(self, held: dict) -> int:
        """
        HARD STOP + BANK-AND-RIDE exit engine (see docs/ENTRY_EXIT_PLAN.md). Per held pos:

          STOP — HARD full-exit the instant loss hits STOP_LOSS_MARGIN_PCT of margin
                 (price move = pct / leverage). NO ATR floor. Applies even when ATR is
                 unavailable — the dollar cap must always be enforced. ("cut losers fast")
          BANK — once not yet scaled and the gain clears +BANK_AT_MARGIN_RET margin return
                 (= +2R), bank BANK_FRACTION of the position and let the rest ride.
          RIDE — the runner trails: after the peak clears RIDE_ACTIVATE_ATR × dailyATR%,
                 exit on a RIDE_GIVEBACK_ATR × dailyATR% retrace from peak.

        A full exit trail-locks the coin until the trader's net position resets, so
        reconcile won't instantly re-buy. Skips a coin only if it has no live mid.
        """
        actions = 0
        for coin, p in list(held.items()):
            px = self.store.latest_mid(coin)
            if not px or not p.entry_price:
                continue
            exc = ((px - p.entry_price) / p.entry_price) if p.direction == "long" \
                  else ((p.entry_price - px) / p.entry_price)
            if exc > p.peak_price_pct:
                p.peak_price_pct = exc
            lev = max(p.leverage, 1.0)
            atr = await self._atr_pct(coin)   # may be None — only the trail needs it

            # ── STOP ───────────────────────────────────────────────────────────
            # Default: HARD -STOP_LOSS_MARGIN_PCT of margin (tight, deterministic).
            # Specialist conviction coins (a trader's `specialty`) ride WITH the trader —
            # widen to the larger of (-9% margin, SPECIALIST_STOP_ATR×ATR) so we don't get
            # noise-chopped out of an elite hold (e.g. feec88 SOL). bank + follow-exit control it.
            stop_px = settings.STOP_LOSS_MARGIN_PCT / lev
            is_spec = coin.upper() in self._specialist
            if is_spec and atr and atr > 0:
                stop_px = max(stop_px, settings.SPECIALIST_STOP_ATR * atr)
            if exc <= -stop_px:
                logger.info(
                    f"[Reconcile] 🛑 STOP {coin} {p.direction} | move {exc:+.1%} "
                    f"≤ -{stop_px:.1%} ({'SPECIALIST ' + str(settings.SPECIALIST_STOP_ATR) + '×ATR' if is_spec else 'hard -' + format(settings.STOP_LOSS_MARGIN_PCT, '.0%') + ' margin'} @ {lev:.0f}x)"
                )
                await self._emit_exit(coin, p.direction, "stop_loss")
                self._trail_locked[coin] = p.direction
                del held[coin]
                actions += 1
                continue

            # ── BANK at +2R (partial; no ATR needed) ──────────────────────────
            if not p.scaled_out:
                bank_px = settings.BANK_AT_MARGIN_RET / lev
                if exc >= bank_px:
                    logger.info(
                        f"[Reconcile] 💰 BANK {settings.BANK_FRACTION:.0%} {coin} {p.direction} | "
                        f"move +{exc:.1%} ≥ +{bank_px:.1%} (+{settings.BANK_AT_MARGIN_RET:.0%} margin "
                        f"= +2R @ {lev:.0f}x) — ride the rest"
                    )
                    await self._emit_exit(coin, p.direction, "bank_2r",
                                          settings.BANK_FRACTION)
                    p.scaled_out = True
                    actions += 1
                    # fall through: the runner can still trail/stop on later ticks

            # ── RIDE the runner (needs ATR) ───────────────────────────────────
            if atr and atr > 0 and p.peak_price_pct >= settings.RIDE_ACTIVATE_ATR * atr:
                giveback = settings.RIDE_GIVEBACK_ATR * atr
                if exc <= p.peak_price_pct - giveback:
                    logger.info(
                        f"[Reconcile] 🏃 RIDE EXIT {coin} {p.direction} | peak "
                        f"+{p.peak_price_pct:.1%} -> +{exc:.1%} (giveback {giveback:.1%} "
                        f"= {settings.RIDE_GIVEBACK_ATR}×ATR {atr:.1%})"
                    )
                    await self._emit_exit(coin, p.direction, "ride_trail")
                    self._trail_locked[coin] = p.direction
                    del held[coin]
                    actions += 1
        return actions

    async def _scale_out(self, held: dict) -> int:
        """
        Two-tranche, volatility-normalized, leverage-aware profit-taking.

          TP1 — bank SCALEOUT_TP1_FRACTION once the favorable price excursion clears
                max(MIN_ATR_MULT × dailyATR%, TP1_MARGIN_RET / leverage). The ATR term
                is a per-coin noise floor; the margin-return/leverage term banks sooner
                on higher-leverage positions. Trigger = the larger of the two.
          Runner — the rest rides an ATR trailing stop: exit on a
                RUNNER_TRAIL_ATR × dailyATR% retrace from peak. On exit the coin is
                trail-locked until the trader resets (as before).

        Returns the number of trim/exit actions emitted (for the reconcile counter).
        Positions with no live mid or no ATR estimate are skipped (never act blind).
        """
        actions = 0
        for coin, p in list(held.items()):
            px = self.store.latest_mid(coin)
            if not px or not p.entry_price:
                continue
            exc = ((px - p.entry_price) / p.entry_price) if p.direction == "long" \
                  else ((p.entry_price - px) / p.entry_price)
            if exc > p.peak_price_pct:
                p.peak_price_pct = exc

            atr = await self._atr_pct(coin)
            if not atr or atr <= 0:
                continue   # no volatility estimate → don't act
            lev = max(p.leverage, 1.0)

            if not p.scaled_out:
                trig = max(settings.SCALEOUT_MIN_ATR_MULT * atr,
                           settings.SCALEOUT_TP1_MARGIN_RET / lev)
                if exc >= trig:
                    logger.info(
                        f"[Reconcile] SCALE-OUT TP1 {coin} {p.direction} "
                        f"trim {settings.SCALEOUT_TP1_FRACTION:.0%} | move +{exc:.1%} "
                        f"≥ trig +{trig:.1%} (ATR {atr:.1%}, lev {lev:.0f}x)"
                    )
                    await self._emit_exit(coin, p.direction, "scaleout_tp1",
                                          settings.SCALEOUT_TP1_FRACTION)
                    # Mark scaled_out optimistically so a second tick can't re-trim before
                    # the partial close is processed (over-trim is worse than a missed one).
                    p.scaled_out = True
                    actions += 1
            else:
                giveback = settings.SCALEOUT_RUNNER_TRAIL_ATR * atr
                if exc <= p.peak_price_pct - giveback:
                    logger.info(
                        f"[Reconcile] SCALE-OUT RUNNER EXIT {coin} {p.direction} | "
                        f"peak +{p.peak_price_pct:.1%} -> +{exc:.1%} "
                        f"(giveback {giveback:.1%} = {settings.SCALEOUT_RUNNER_TRAIL_ATR}×ATR)"
                    )
                    await self._emit_exit(coin, p.direction, "scaleout_runner")
                    self._trail_locked[coin] = p.direction   # lock until trader resets
                    del held[coin]
                    actions += 1
        return actions

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

    async def _emit_exit(self, coin: str, held_direction: str, reason: str,
                         fraction: float = 1.0):
        # Exit signal direction = the side WE HOLD (the one being closed) — see
        # executor._close_position matching convention. fraction<1 = partial scale-out
        # (trim that share, keep a runner); fraction>=1 = full close.
        label = f"{fraction:.0%} " if fraction < 1.0 else ""
        logger.info(f"[Reconcile] EXIT {label}{coin} {held_direction} — {reason}")
        await self._signal_queue.put(TradeSignal(
            strategy="leaderboard",
            coin=coin,
            direction=held_direction,
            size_usd=0,
            confidence=1.0,
            meta={"action": "exit", "reason": reason, "fraction": fraction},
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
                    address=address, realized_pnl=pnl, win_rate=win_rate,
                    max_drawdown=abs(drawdown), avg_leverage=leverage,
                    trade_count=trades, account_age_days=age,
                ))
            except Exception as e:
                logger.debug(f"[Leaderboard] Parse skip: {e}")
                continue
        return traders
