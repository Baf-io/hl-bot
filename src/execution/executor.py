"""
Execution layer — takes approved signals from RiskManager and places orders.
Uses hyperliquid-python-sdk. Handles retries, maker/taker routing, position tracking.
"""
import asyncio
import time
from loguru import logger
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from config import settings
from data.store import TradeSignal
from risk.manager import RiskManager


class Executor:
    def __init__(self, risk: RiskManager):
        self.risk = risk
        self._exchange: Exchange | None = None
        self._info: Info | None = None
        self._signal_queue: asyncio.Queue = asyncio.Queue()
        self.squeeze_guard = None   # injected by main.py after init
        self._last_stop = None      # {coin,oid,px} from the most recent confirmed stop
        self._signal_status: dict = {}   # signal_id → fill+stop verification (brain polls GET /status)

    def init_client(self):
        """
        Initialise Hyperliquid SDK clients.
        Call once at startup after loading .env.
        """
        import eth_account
        from hyperliquid.utils import constants

        wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
        base_url = (
            constants.TESTNET_API_URL
            if settings.HL_TESTNET
            else constants.MAINNET_API_URL
        )
        # account_address tells the SDK to trade on behalf of the main wallet
        # using the agent key for signing — critical for agent key setup
        self._exchange = Exchange(
            wallet,
            base_url,
            account_address=settings.HL_WALLET_ADDRESS,
        )
        self._info = Info(base_url, skip_ws=True)
        # Pre-warm: load coin metadata so first trade has no cold-start delay
        try:
            self._meta = self._info.meta()
        except Exception:
            self._meta = {}
        logger.info(
            f"Executor initialised | wallet={settings.HL_WALLET_ADDRESS[:10]}… "
            f"({'TESTNET' if settings.HL_TESTNET else 'MAINNET'})"
        )
        # Sync open positions from HL so risk manager is accurate after restarts
        self._sync_positions_from_hl()

    # Positions smaller than this are considered undersized from an old PORTFOLIO_USD
    # setting and will be closed on startup so backfill can re-enter them correctly.
    _MIN_SYNC_SIZE_USD = 50.0

    def _sync_positions_from_hl(self):
        """
        On startup: fetch real open positions from Hyperliquid and populate
        the risk manager. Prevents opening duplicate positions after bot restarts.

        Dust positions (< $30 notional) are closed immediately — they are artefacts
        of a previous low PORTFOLIO_USD setting and would block correctly-sized
        backfill entries for the same coin.
        """
        try:
            state = self._info.user_state(settings.HL_WALLET_ADDRESS)
            positions = state.get("assetPositions", [])
            count = 0
            dust_closed = []
            for p in positions:
                pos = p.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi == 0:
                    continue
                coin      = pos.get("coin", "")
                if coin in settings.COPIER_SKIP_COINS:
                    logger.info(
                        f"[Executor] Skip sync of {coin} — tracker sleeve or user-manual coin, "
                        f"not copy-managed (won't adopt or auto-close it)"
                    )
                    continue
                direction = "long" if szi > 0 else "short"
                entry_px  = float(pos.get("entryPx") or 0)
                size_usd  = abs(szi) * entry_px

                # Close dust positions so backfill can re-enter at correct size
                if size_usd < self._MIN_SYNC_SIZE_USD:
                    logger.warning(
                        f"[Executor] Dust position {direction} {coin} ${size_usd:.0f} "
                        f"< ${self._MIN_SYNC_SIZE_USD} — closing for re-entry at correct size"
                    )
                    closed = False
                    try:
                        self._exchange.market_close(coin)
                        dust_closed.append(coin)
                        logger.info(f"[Executor] Dust closed: {coin}")
                        closed = True
                    except Exception as e:
                        logger.warning(
                            f"[Executor] Could not close dust {coin}: {e} "
                            f"— registering so guardian/exit can clean it up"
                        )
                    if closed:
                        continue  # successfully closed — skip registration
                    # Fall through: register the position so the bot knows about it.
                    # It will be closed when the trader exits or the guardian fires.

                from data.store import TradeSignal
                # Leverage from the live position (notional / marginUsed) so the risk
                # manager's margin-equiv delta check is correct. Without this, synced
                # positions defaulted to lev=1.0 → their FULL notional counted as delta
                # → the delta limit spuriously blocked new entries/flips after restarts.
                margin_used = float(pos.get("marginUsed") or 0)
                synced_lev  = (min(size_usd / margin_used, float(settings.MAX_LEVERAGE))
                               if margin_used > 0 else 1.0)
                fake_signal = TradeSignal(
                    # Tag as "synced" — not "leaderboard" — so these don't eat strategy slots.
                    # Close signals will still find them via coin-fallback in _close_position().
                    strategy="synced",
                    coin=coin, direction=direction,
                    size_usd=size_usd, confidence=1.0,
                    meta={"action": "enter", "leverage": round(synced_lev, 1)},
                )
                self.risk.register_fill(fake_signal, size_usd, entry_px)
                count += 1
                logger.info(f"[Executor] Synced existing position: {direction} {coin} ${size_usd:,.0f}")

            if count == 0 and not dust_closed:
                logger.info("[Executor] No existing HL positions — clean start")
            else:
                logger.warning(
                    f"[Executor] Synced {count} positions from HL — slots pre-filled"
                    + (f" | Closed {len(dust_closed)} dust: {dust_closed}" if dust_closed else "")
                )
        except Exception as e:
            logger.warning(f"[Executor] Position sync failed: {e} — starting fresh")

    async def enqueue(self, signal: TradeSignal):
        """Signal engines push here; executor processes sequentially."""
        await self._signal_queue.put(signal)

    async def run(self):
        """Main execution loop — process signals from queue."""
        logger.info("Executor loop started")
        while True:
            signal: TradeSignal = await self._signal_queue.get()
            try:
                await self._process(signal)
            except Exception as e:
                logger.error(f"[Executor] Error processing {signal.coin}: {e}")
            finally:
                self._signal_queue.task_done()

    async def _process(self, signal: TradeSignal):
        approved, reason, size_usd = self.risk.approve(signal)
        if not approved:
            logger.info(f"[Executor] Signal rejected: {reason}")
            return

        action = signal.meta.get("action", "enter")
        if action == "exit":
            await self._close_position(signal)
        elif action == "add":
            await self._add_to_position(signal, size_usd)
        else:
            await self._open_position(signal, size_usd)

    # ── Order placement ────────────────────────────────────────────────────────

    async def _open_position(self, signal: TradeSignal, size_usd: float):
        coin = signal.coin
        direction = signal.direction

        # Get current price
        mid = await self._get_mid_price(coin)
        if mid is None:
            logger.error(f"[Executor] No price for {coin}, skipping")
            return

        # Convert USD size to coin size, respecting HL's per-coin lot size (szDecimals)
        sz_decimals = self._get_sz_decimals(coin)
        size_coin = round(size_usd / mid, sz_decimals)
        if size_coin <= 0:
            logger.warning(f"[Executor] {coin} size rounds to 0 at ${size_usd} — skipping")
            return
        is_buy    = direction == "long"
        is_copy   = signal.strategy == "leaderboard"

        # For copy trades: set leverage to match the trader's leverage (capped by COPY_MAX_COPY_LEVERAGE).
        # Signal meta["leverage"] is already capped at settings.COPY_MAX_COPY_LEVERAGE (10x).
        # Cross margin — shares collateral across all positions.
        if is_copy or signal.strategy == "brain":
            copy_lev = int(min(
                max(float(signal.meta.get("leverage", 5)), 1),
                settings.COPY_MAX_COPY_LEVERAGE,
            ))
            try:
                self._exchange.update_leverage(copy_lev, coin, True)   # cross margin
                logger.debug(f"[Executor] Set {coin} leverage to {copy_lev}x cross")
            except Exception as e:
                logger.warning(f"[Executor] Could not set leverage for {coin}: {e}")

        # All strategies use market orders — latency matters, maker rebate is tiny vs missed entry
        result = await self._place_market(coin, is_buy, size_coin)

        filled, fill_px, err = self._parse_fill(result)
        if filled:
            actual_px   = fill_px or mid
            position_id = self.risk.register_fill(signal, size_usd, actual_px)
            logger.success(
                f"[Executor] FILLED {direction.upper()} {size_coin} {coin} @ ${actual_px:,.4f} "
                f"(${size_usd:,.0f}) pos#{position_id} [{signal.strategy}]"
            )
            await self.risk.store.log_trade(signal.strategy, coin, direction, size_usd, actual_px)
            # Notify squeeze guard — start tracking MAE/MFE
            if self.squeeze_guard:
                source = signal.meta.get("source", signal.strategy)
                self.squeeze_guard.on_position_opened(
                    position_id, coin, direction, actual_px, source, size_usd
                )
            # SL/TP: copy trades trust the trader's exit signal + guardian.
            # Native SL/TP killed positions that ran +50-80% for weeks.
            # Own-signal strategies (cascade, funding) still get SL/TP protection.
            # ── CONTRACT (B1): EVERY order MUST carry a native exchange stop. Place it
            # immediately after the fill; if it can't be confirmed, REJECT = close the
            # position right away (never hold an unstopped position).
            stop_ok = await self._place_protective_stop(coin, is_buy, size_coin, actual_px,
                                                        stop_px=signal.meta.get("stop_px"))
            sid = signal.meta.get("signal_id")
            if not stop_ok:
                logger.error(
                    f"[Executor] ⛔ NO STOP confirmed for {coin} pos#{position_id} — "
                    f"CLOSING per risk contract (reject if absent)"
                )
                await self._place_market_close(coin)
                self.risk.close_position(position_id, actual_px)
                if sid:
                    self._signal_status[sid] = {
                        "signal_id": sid, "coin": coin, "filled": True, "fill_px": actual_px,
                        "size": size_coin, "stop_resting": False, "stop_oid": None, "stop_px": None,
                        "outcome": "FAIL-SAFE CLOSED (stop unconfirmed)", "ts": time.time(),
                    }
                if getattr(self, "alerter", None):
                    try: await self.alerter.send(f"⛔ {coin} opened but stop FAILED → force-closed (contract: no unstopped position)")
                    except Exception: pass
                return
            if sid:
                ls = self._last_stop or {}
                self._signal_status[sid] = {
                    "signal_id": sid, "coin": coin, "filled": True, "fill_px": actual_px,
                    "size": size_coin, "stop_resting": True,
                    "stop_oid": ls.get("oid"), "stop_px": ls.get("px"),
                    "outcome": "OPEN (stop confirmed on book)", "ts": time.time(),
                }
            # The B1 protective stop above is the ONLY stop. The legacy _place_native_sltp
            # (6%/12% SL/TP) is superseded — it must NOT run (it added an unwanted TP to the
            # brain probe; contract exit = stop OR TTL, no TP).
        else:
            logger.warning(f"[Executor] Order not filled for {coin}: {err}")

    async def _add_to_position(self, signal: TradeSignal, size_usd: float):
        """Add-mirroring: grow an EXISTING same-direction position (the trader added in a
        confirmed trend). Market_open more in the same direction at the existing leverage,
        then update the tracked position's size + weighted-avg entry. Never flips/opens new."""
        coin, direction = signal.coin, signal.direction
        pos = next((p for p in self.risk.open_positions
                    if p.coin == coin and p.direction == direction), None)
        if not pos:
            logger.warning(f"[Executor] ADD {coin} {direction} but no matching held position — skip")
            return
        mid = await self._get_mid_price(coin)
        if mid is None:
            logger.error(f"[Executor] No price for {coin} add — skip")
            return
        size_coin = round(size_usd / mid, self._get_sz_decimals(coin))
        if size_coin <= 0:
            logger.warning(f"[Executor] {coin} add rounds to 0 at ${size_usd} — skip")
            return
        result = await self._place_market(coin, direction == "long", size_coin)
        filled, fill_px, err = self._parse_fill(result)
        if not filled:
            logger.warning(f"[Executor] ADD not filled for {coin}: {err}")
            return
        actual_px = fill_px or mid
        self.risk.add_to_position(pos.id, size_usd, actual_px)
        logger.success(
            f"[Executor] ADDED {direction.upper()} +{size_coin} {coin} @ ${actual_px:,.4f} "
            f"(+${size_usd:,.0f}) pos#{pos.id}"
        )

    async def _close_position(self, signal: TradeSignal):
        # The signal.direction for exit signals = the direction WE ARE CLOSING.
        # e.g. "long" means "close a LONG position in this coin".
        # Matching by direction prevents cross-trader conflicts: if trader A closes
        # their LONG but we're SHORT (from trader B), we correctly ignore it.
        close_dir = signal.direction   # "long" or "short"

        # 1. Exact match: coin + strategy + direction
        pos = next(
            (p for p in self.risk.open_positions
             if p.coin == signal.coin
             and p.strategy == signal.strategy
             and p.direction == close_dir),
            None,
        )
        # 2. Direction match: coin + direction (covers "synced" strategy positions)
        if pos is None:
            pos = next(
                (p for p in self.risk.open_positions
                 if p.coin == signal.coin and p.direction == close_dir),
                None,
            )
            if pos:
                logger.debug(
                    f"[Executor] Direction-match close {signal.coin} {close_dir} "
                    f"(strategy {pos.strategy!r})"
                )
        # 3. Last resort: coin-only (handles guardian/zombie closes where direction may vary)
        if pos is None and signal.meta.get("reason") in ("zombie", "nuclear", "stop_loss", "ride_trail", "ttl"):
            pos = next(
                (p for p in self.risk.open_positions if p.coin == signal.coin),
                None,
            )
        if not pos:
            logger.warning(
                f"[Executor] Close signal for {signal.coin} {close_dir} "
                f"but no matching position found (have: "
                + str([(p.coin, p.direction) for p in self.risk.open_positions if p.coin == signal.coin])
                + ")"
            )
            return

        # Partial close (scale-out): fraction in (0,1) trims that share of the position
        # and leaves a runner. fraction>=1 (or absent) = full close.
        fraction = float(signal.meta.get("fraction", 1.0))
        partial  = 0.0 < fraction < 1.0

        # market_close is reduce-only by nature and derives szDecimals from the live
        # on-exchange position. For a partial we pass an explicit sz (the trimmed coin
        # amount); for a full close we pass sz=None so the SDK closes the whole position.
        # This avoids the flip-into-a-new-position risk of an offsetting market_open
        # (e.g. if a native SL/TP already trimmed the size).
        trim_sz = None
        if partial:
            mid = await self._get_mid_price(signal.coin) or pos.entry_price
            sz_decimals = self._get_sz_decimals(signal.coin)
            trim_sz = round((pos.size_usd * fraction) / mid, sz_decimals)
            if trim_sz <= 0:
                logger.warning(
                    f"[Executor] {signal.coin} trim size rounds to 0 "
                    f"(${pos.size_usd * fraction:,.0f} @ {mid}) — skipping trim"
                )
                return

        result = await self._place_market_close(signal.coin, sz=trim_sz)

        # The SDK returns {"status":"ok"} even on rejects — must parse the inner status.
        # If the close didn't actually fill, LEAVE the position in the tracker so the
        # guardian/next signal retries, rather than orphaning a still-open HL position.
        filled, fill_px, err = self._parse_fill(result)
        if not filled:
            logger.error(
                f"[Executor] CLOSE FAILED {signal.coin} pos#{pos.id}: {err} "
                f"— position still open on HL, leaving in tracker"
            )
            return

        exit_px = fill_px or await self._get_mid_price(signal.coin) or pos.entry_price

        if partial:
            # Book the banked tranche; the runner stays open (and is marked scaled_out
            # so reconcile's RESIZE won't re-buy it). No squeeze-guard close — still open.
            self.risk.reduce_position(pos.id, fraction, exit_px)
            logger.success(
                f"[Executor] TRIMMED {fraction:.0%} {signal.coin} pos#{pos.id} "
                f"@ ${exit_px:,.4f} ({signal.meta.get('reason', 'scaleout')})"
            )
            return

        self.risk.close_position(pos.id, exit_px)
        logger.success(f"[Executor] CLOSED {signal.coin} pos#{pos.id} @ ${exit_px:,.4f}")
        # Notify squeeze guard with the real exit reason (trader_closed / zombie / nuclear)
        if self.squeeze_guard:
            reason = signal.meta.get("reason", "trader_closed")
            kind   = reason if reason in ("zombie", "nuclear") else "trader_closed"
            self.squeeze_guard.on_position_closed(pos.id, exit_px, kind)

    # ── SDK wrappers ───────────────────────────────────────────────────────────

    async def _place_market(self, coin: str, is_buy: bool, size: float) -> dict | None:
        if not self._exchange:
            logger.error("Exchange not initialised — call init_client() first")
            return None
        try:
            # slippage=0.001 = 0.1% max slippage, fast IOC fill
            result = self._exchange.market_open(coin, is_buy, size, slippage=0.001)
            return result
        except Exception as e:
            logger.error(f"[Executor] market_open failed: {e}")
            return None

    async def _place_market_close(self, coin: str, sz: float | None = None) -> dict | None:
        """
        Close the live position for `coin`. Reduce-only by nature — the SDK reads the
        on-exchange size/side and submits an aggressive IOC reduce-only order, so it can
        never flip into a new position and never mis-rounds the lot size.
        `sz` closes only that many coins (partial scale-out); sz=None closes the full
        position. Returns None if there is no open position for the coin.
        """
        if not self._exchange:
            logger.error("Exchange not initialised — call init_client() first")
            return None
        try:
            # slippage=0.01 (1%) — a close prioritises getting filled over price
            return self._exchange.market_close(coin, sz=sz, slippage=0.01)
        except Exception as e:
            logger.error(f"[Executor] market_close failed: {e}")
            return None

    async def _place_limit(
        self, coin: str, is_buy: bool, size: float, mid: float, offset_pct: float = 0.0002
    ) -> dict | None:
        """Place limit order slightly inside the spread for maker rebate."""
        if not self._exchange:
            return None
        px = mid * (1 - offset_pct) if is_buy else mid * (1 + offset_pct)
        px = round(px, 2)
        try:
            result = self._exchange.order(coin, is_buy, size, px, {"limit": {"tif": "Gtc"}})
            return result
        except Exception as e:
            logger.error(f"[Executor] limit order failed: {e}")
            return None

    def _get_sz_decimals(self, coin: str) -> int:
        """Return the number of decimal places allowed for a coin's size on HL."""
        try:
            universe = self._meta.get("universe", [])
            for asset in universe:
                if asset.get("name") == coin:
                    return int(asset.get("szDecimals", 4))
        except Exception:
            pass
        return 4  # safe fallback

    @staticmethod
    def _parse_fill(result: dict) -> tuple[bool, float | None, str]:
        """
        Parse the HL SDK response properly.
        Returns (filled, avg_price, error_msg).
        SDK always returns {"status":"ok"} even on errors — must check inside.
        """
        if not result:
            return False, None, "no result"
        if result.get("status") != "ok":
            return False, None, str(result)
        try:
            statuses = result["response"]["data"]["statuses"]
            s = statuses[0]
            if "filled" in s:
                px = float(s["filled"].get("avgPx", 0)) or None
                return True, px, ""
            if "resting" in s:
                # Limit order sitting — treat as not filled yet
                return False, None, "order resting (limit not filled)"
            if "error" in s:
                return False, None, s["error"]
        except (KeyError, IndexError, TypeError) as e:
            return False, None, f"parse error: {e}"
        return False, None, f"unknown status: {result}"

    @staticmethod
    def _round_px(px: float) -> float:
        """Round to 5 significant figures (HL price precision rule)."""
        if px <= 0:
            return px
        from math import floor, log10
        d = max(0, min(6, 5 - int(floor(log10(abs(px)))) - 1))
        return round(px, d)

    async def _place_protective_stop(self, coin: str, entry_is_buy: bool, size: float,
                                     entry_px: float, stop_px: float | None = None) -> bool:
        """CONTRACT (B1): place a native, reduce-only exchange STOP. Uses the EXPLICIT stop
        price when provided (brain signals carry their own stop), else COPY_SL_PCT from entry.
        Returns True only if the exchange confirms it; caller closes the position on False —
        no unstopped position is allowed to ride."""
        if not self._exchange:
            return False
        close_is_buy = not entry_is_buy                    # long→SELL stop, short→BUY stop
        if stop_px and float(stop_px) > 0:
            sl_px = self._round_px(float(stop_px))         # brain's explicit stop
            # sanity: must be on the protective side of entry
            if entry_is_buy and not sl_px < entry_px:
                logger.error(f"[Executor] stop {sl_px} not below long entry {entry_px} — reject"); return False
            if not entry_is_buy and not sl_px > entry_px:
                logger.error(f"[Executor] stop {sl_px} not above short entry {entry_px} — reject"); return False
        else:
            sl = settings.COPY_SL_PCT
            sl_px = self._round_px(entry_px * (1 - sl)) if entry_is_buy else self._round_px(entry_px * (1 + sl))
        self._last_stop = None
        try:
            r = self._exchange.order(
                coin, close_is_buy, size, sl_px,
                {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            if not (r and r.get("status") == "ok"):
                logger.error(f"[Executor] stop bad response {coin}: {r}")
                return False
            st = r["response"]["data"]["statuses"][0]
            if "error" in st:
                logger.error(f"[Executor] stop rejected {coin}: {st['error']}")
                return False
            # The ack is NOT trust. Pull the resting oid and CONFIRM it is actually on the
            # book via a fresh openOrders read (eyes-on-book, defense in depth). A stop that
            # acked but isn't resting is treated as a FAILURE → caller force-closes.
            oid = (st.get("resting") or {}).get("oid")
            if oid is None:
                logger.error(f"[Executor] stop {coin}: no resting oid in ack ({st}) — treat as unstopped")
                return False
            resting = False
            for attempt in range(3):
                try:
                    oo = self._info.open_orders(settings.HL_WALLET_ADDRESS)
                    if any(o.get("oid") == oid for o in oo):
                        resting = True; break
                except Exception as e:
                    logger.warning(f"[Executor] stop re-query {coin} attempt {attempt}: {e}")
                await asyncio.sleep(0.4)
            if not resting:
                logger.error(f"[Executor] stop {coin} oid={oid} ACKed but NOT resting on HL — treat as unstopped")
                return False
            dist = abs(sl_px - entry_px) / entry_px if entry_px else 0
            self._last_stop = {"coin": coin, "oid": oid, "px": sl_px}
            logger.success(f"[Executor] 🛡️ native STOP {coin} @ ${sl_px} (-{dist:.1%} from ${entry_px}) oid={oid} CONFIRMED on book")
            return True
        except Exception as e:
            logger.error(f"[Executor] stop placement EXC {coin}: {e}")
        return False

    async def _place_native_sltp(self, coin: str, is_buy: bool, size: float, entry_px: float):
        """
        Place native stop-loss + take-profit orders directly on Hyperliquid.
        These are exchange-side orders — they execute even if the bot is offline.

        is_buy=True means we entered LONG → SL is a sell below, TP is a sell above.
        is_buy=False means we entered SHORT → SL is a buy above, TP is a buy below.
        """
        SL_PCT = 0.06   # -6% stop loss (widened from -3% — traders hold through dips)
        TP_PCT = 0.12   # +12% take profit (widened from 8% — let winners breathe)

        if is_buy:  # long position
            sl_px = round(entry_px * (1 - SL_PCT), 4)
            tp_px = round(entry_px * (1 + TP_PCT), 4)
            close_is_buy = False   # sell to close long
        else:       # short position
            sl_px = round(entry_px * (1 + SL_PCT), 4)
            tp_px = round(entry_px * (1 - TP_PCT), 4)
            close_is_buy = True    # buy to close short

        try:
            # Stop loss
            sl_result = self._exchange.order(
                coin, close_is_buy, size, sl_px,
                {"trigger": {"triggerPx": sl_px, "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
            # Take profit
            tp_result = self._exchange.order(
                coin, close_is_buy, size, tp_px,
                {"trigger": {"triggerPx": tp_px, "isMarket": True, "tpsl": "tp"}},
                reduce_only=True,
            )
            sl_ok = sl_result and sl_result.get("status") == "ok"
            tp_ok = tp_result and tp_result.get("status") == "ok"
            logger.info(
                f"[Executor] Native SL/TP placed for {coin} | "
                f"SL=${sl_px:,.4f} {'✅' if sl_ok else '❌'} | "
                f"TP=${tp_px:,.4f} {'✅' if tp_ok else '❌'}"
            )
        except Exception as e:
            logger.warning(f"[Executor] SL/TP placement failed for {coin}: {e} — guardian will cover")

    async def _get_mid_price(self, coin: str) -> float | None:
        # ms-tier: prefer the WS-streamed cached mid (updated every allMids tick) over a
        # synchronous all_mids() HTTP fetch (~80-150ms HL round-trip). Falls back to the
        # API only if WS hasn't seen a tick for this coin yet (rare; happens within
        # seconds of startup or for very illiquid coins).
        store = getattr(getattr(self, "risk", None), "store", None)
        if store is not None:
            cached = store.latest_mid(coin)
            if cached is not None and cached > 0:
                return float(cached)
        if not self._info:
            return None
        try:
            mids = self._info.all_mids()
            return float(mids.get(coin, 0)) or None
        except Exception as e:
            logger.error(f"[Executor] get_mid_price failed: {e}")
            return None
