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
        self._exchange = Exchange(wallet, base_url)
        self._info = Info(base_url, skip_ws=True)
        logger.info(
            f"Executor initialised | wallet={settings.HL_WALLET_ADDRESS[:10]}… "
            f"({'TESTNET' if settings.HL_TESTNET else 'MAINNET'})"
        )

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

        # Convert USD size to coin size
        size_coin = round(size_usd / mid, 6)
        is_buy = direction == "long"

        # Use limit order at slight offset for maker rebates when urgency is low
        # Use market order for cascade (time-sensitive)
        if signal.strategy == "cascade":
            result = await self._place_market(coin, is_buy, size_coin)
        else:
            result = await self._place_limit(coin, is_buy, size_coin, mid)

        if result and result.get("status") == "ok":
            position_id = self.risk.register_fill(signal, size_usd, mid)
            logger.success(
                f"[Executor] FILLED {direction.upper()} {size_coin} {coin} @ ${mid:,.2f} "
                f"(${size_usd:,.0f}) pos#{position_id} [{signal.strategy}]"
            )
            await self.risk.store.log_trade(signal.strategy, coin, direction, size_usd, mid)
        else:
            logger.error(f"[Executor] Order failed: {result}")

    async def _close_position(self, signal: TradeSignal):
        # Find matching open position
        pos = next(
            (p for p in self.risk.open_positions if p.coin == signal.coin and p.strategy == signal.strategy),
            None,
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

    # ── SDK wrappers ───────────────────────────────────────────────────────────

    async def _place_market(self, coin: str, is_buy: bool, size: float) -> dict | None:
        if not self._exchange:
            logger.error("Exchange not initialised — call init_client() first")
            return None
        try:
            result = self._exchange.market_open(coin, is_buy, size)
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

    async def _get_mid_price(self, coin: str) -> float | None:
        if not self._info:
            return None
        try:
            mids = self._info.all_mids()
            return float(mids.get(coin, 0)) or None
        except Exception as e:
            logger.error(f"[Executor] get_mid_price failed: {e}")
            return None
