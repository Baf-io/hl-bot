"""
Execution layer — takes approved signals from RiskManager and places orders.
Uses hyperliquid-python-sdk. Handles retries, maker/taker routing, position tracking.
"""
import asyncio
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

    def _sync_positions_from_hl(self):
        """
        On startup: fetch real open positions from Hyperliquid and populate
        the risk manager. Prevents opening duplicate positions after bot restarts.
        """
        try:
            state = self._info.user_state(settings.HL_WALLET_ADDRESS)
            positions = state.get("assetPositions", [])
            count = 0
            for p in positions:
                pos = p.get("position", {})
                szi = float(pos.get("szi", 0))
                if szi == 0:
                    continue
                coin      = pos.get("coin", "")
                direction = "long" if szi > 0 else "short"
                entry_px  = float(pos.get("entryPx") or 0)
                size_usd  = abs(szi) * entry_px

                from data.store import TradeSignal
                fake_signal = TradeSignal(
                    # Tag as "synced" — not "leaderboard" — so these don't eat strategy slots.
                    # Close signals will still find them via coin-fallback in _close_position().
                    strategy="synced",
                    coin=coin, direction=direction,
                    size_usd=size_usd, confidence=1.0, meta={"action": "enter"},
                )
                self.risk.register_fill(fake_signal, size_usd, entry_px)
                count += 1
                logger.info(f"[Executor] Synced existing position: {direction} {coin} ${size_usd:,.0f}")

            if count == 0:
                logger.info("[Executor] No existing HL positions — clean start")
            else:
                logger.warning(f"[Executor] Synced {count} existing positions from HL — slots pre-filled")
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

        # For copy trades: set leverage to 5x cross before opening.
        # Traders use 3–40x on their $10M+ portfolios — we cap at 5x to protect
        # our $1K account. Cross margin shares collateral across all positions.
        if is_copy:
            try:
                self._exchange.update_leverage(5, coin, True)   # 5x cross
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
            if not is_copy:
                await self._place_native_sltp(coin, is_buy, size_coin, actual_px)
            else:
                logger.debug(f"[Executor] Leaderboard copy — no native SL/TP, trusting trader exit")
        else:
            logger.warning(f"[Executor] Order not filled for {coin}: {err}")

    async def _close_position(self, signal: TradeSignal):
        # Try exact match first (coin + strategy)
        pos = next(
            (p for p in self.risk.open_positions if p.coin == signal.coin and p.strategy == signal.strategy),
            None,
        )
        # Fallback: coin-only match — covers positions synced from HL on startup
        # (those are tagged "synced" but close signals arrive as "leaderboard")
        if pos is None:
            pos = next(
                (p for p in self.risk.open_positions if p.coin == signal.coin),
                None,
            )
            if pos:
                logger.debug(
                    f"[Executor] Coin-only match for {signal.coin} close "
                    f"(strategy {pos.strategy!r} ≠ {signal.strategy!r})"
                )
        if not pos:
            logger.warning(f"[Executor] Close signal for {signal.coin} but no open position found")
            return

        mid = await self._get_mid_price(signal.coin)
        if mid is None:
            return

        size_coin = round(pos.size_usd / pos.entry_price, 6)
        is_buy = pos.direction == "short"   # closing short → buy; closing long → sell

        result = await self._place_market(signal.coin, is_buy, size_coin)
        if result and result.get("status") == "ok":
            self.risk.close_position(pos.id, mid)
            logger.success(f"[Executor] CLOSED {signal.coin} pos#{pos.id} @ ${mid:,.2f}")
            # Notify squeeze guard — trader closed (not SL/TP/guardian)
            if self.squeeze_guard:
                self.squeeze_guard.on_position_closed(pos.id, mid, "trader_closed")

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
        if not self._info:
            return None
        try:
            mids = self._info.all_mids()
            return float(mids.get(coin, 0)) or None
        except Exception as e:
            logger.error(f"[Executor] get_mid_price failed: {e}")
            return None
