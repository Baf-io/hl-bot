"""
WebSocket feed + REST polling for funding data.
"""
import asyncio
import aiohttp
from loguru import logger
from hyperliquid.websocket_manager import WebsocketManager
from config import settings

HL_REST = (
    "https://api.hyperliquid-testnet.xyz/info"
    if settings.HL_TESTNET
    else "https://api.hyperliquid.xyz/info"
)

WATCHED_COINS = [
    "BTC", "ETH", "SOL", "HYPE", "WIF", "BONK", "ARB", "OP", "PURR"
]


class HyperliquidFeed:
    def __init__(self):
        self._handlers: dict[str, list] = {}
        self._ws = None
        self._loop = None

    def subscribe(self, channel: str, handler):
        """Register handler. If WS is already running, subscribe immediately."""
        self._handlers.setdefault(channel, []).append(handler)
        logger.debug(f"Subscribed {handler.__name__} to {channel}")
        # If WebSocket already running, subscribe this channel live
        if self._ws is not None and channel.startswith("userFills:"):
            self._subscribe_channel(channel)

    async def run(self):
        base_url = (
            "https://api.hyperliquid-testnet.xyz"
            if settings.HL_TESTNET
            else "https://api.hyperliquid.xyz"
        )
        logger.info(f"Connecting to HL feed ({'TESTNET' if settings.HL_TESTNET else 'MAINNET'})")
        self._loop = asyncio.get_event_loop()   # capture loop for thread-safe dispatch

        self._ws = WebsocketManager(base_url)
        self._ws.start()

        # Subscribe WebSocket channels
        for channel in list(self._handlers.keys()):
            self._subscribe_channel(channel)

        # Run funding poller alongside WebSocket
        await asyncio.gather(
            self._poll_funding_forever(),
            self._keepalive(),
        )

    def _subscribe_channel(self, channel: str):
        ws = self._ws
        if channel == "allMids":
            ws.subscribe({"type": "allMids"}, self._make_dispatcher(channel))

        elif channel == "orderbook":
            for coin in ["BTC", "ETH", "SOL"]:
                ws.subscribe(
                    {"type": "l2Book", "coin": coin},
                    self._make_dispatcher(channel),
                )

        elif channel.startswith("userFills:"):
            address = channel.split(":")[1]
            ws.subscribe(
                {"type": "userFills", "user": address},
                self._make_dispatcher(channel),
            )

        # "funding" channel handled via REST polling — not WebSocket

    def _make_dispatcher(self, channel: str):
        handlers = self._handlers.get(channel, [])

        def dispatch(msg):
            # WebSocket runs in a separate thread — must use threadsafe call
            loop = self._loop
            if loop and loop.is_running():
                for handler in handlers:
                    asyncio.run_coroutine_threadsafe(handler(msg), loop)

        return dispatch

    async def _poll_funding_forever(self):
        """
        Poll funding + OI via REST every 15s.
        More reliable than WebSocket for this data.
        """
        while True:
            try:
                await self._fetch_and_dispatch_funding()
            except Exception as e:
                logger.warning(f"[Feed] Funding poll error: {e}")
            await asyncio.sleep(15)

    async def _fetch_and_dispatch_funding(self):
        handlers = self._handlers.get("funding", [])
        if not handlers:
            return

        async with aiohttp.ClientSession() as session:
            async with session.post(
                HL_REST,
                json={"type": "metaAndAssetCtxs"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)

        # data = [meta, assetCtxs]
        if not isinstance(data, list) or len(data) < 2:
            return

        meta      = data[0]
        asset_ctxs = data[1]
        universe  = meta.get("universe", [])

        for i, ctx in enumerate(asset_ctxs):
            if i >= len(universe):
                break
            coin = universe[i].get("name", "")
            if not coin:
                continue

            msg = {
                "data": [{
                    "coin":          coin,
                    "funding":       ctx.get("funding", "0"),
                    "openInterest":  ctx.get("openInterest", "0"),
                    "markPx":        ctx.get("markPx", "0"),
                    "oraclePx":      ctx.get("oraclePx", "0"),
                }]
            }
            for handler in handlers:
                asyncio.create_task(handler(msg))

    async def _keepalive(self):
        """Prevent WebSocket timeout."""
        while True:
            await asyncio.sleep(30)
