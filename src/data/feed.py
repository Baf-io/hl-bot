"""
WebSocket feed — connects to Hyperliquid and routes messages
to registered handlers. Single connection, multiple subscribers.
"""
import asyncio
import json
from loguru import logger
from hyperliquid.websocket_manager import WebsocketManager
from config import settings


class HyperliquidFeed:
    """
    Wraps the HL WebSocket. Handlers register for specific channels.

    Usage:
        feed = HyperliquidFeed()
        feed.subscribe("allMids", my_handler)
        await feed.run()
    """

    def __init__(self):
        self._handlers: dict[str, list] = {}
        self._ws = None

    def subscribe(self, channel: str, handler):
        """Register an async callback for a channel."""
        self._handlers.setdefault(channel, []).append(handler)
        logger.debug(f"Subscribed {handler.__name__} to {channel}")

    async def run(self):
        """Start the WebSocket — runs forever (use asyncio.create_task)."""
        base_url = (
            "https://api.hyperliquid-testnet.xyz"
            if settings.HL_TESTNET
            else "https://api.hyperliquid.xyz"
        )
        logger.info(f"Connecting to HL feed ({'TESTNET' if settings.HL_TESTNET else 'MAINNET'})")

        # hyperliquid-python-sdk's WebsocketManager handles reconnect
        self._ws = WebsocketManager(base_url)
        self._ws.start()

        # Subscribe to channels we have handlers for
        for channel in self._handlers:
            self._subscribe_channel(channel)

        # Keep alive — real message routing happens via callbacks
        while True:
            await asyncio.sleep(1)

    def _subscribe_channel(self, channel: str):
        """Map channel name → SDK subscription call."""
        ws = self._ws

        if channel == "allMids":
            ws.subscribe({"type": "allMids"}, self._make_dispatcher(channel))

        elif channel == "funding":
            # Subscribe to funding updates for all coins
            ws.subscribe({"type": "activeAssetCtx"}, self._make_dispatcher(channel))

        elif channel == "trades":
            ws.subscribe({"type": "trades", "coin": "BTC"}, self._make_dispatcher(channel))

        elif channel.startswith("userFills:"):
            address = channel.split(":")[1]
            ws.subscribe(
                {"type": "userFills", "user": address},
                self._make_dispatcher(channel),
            )

        elif channel == "orderbook":
            # Subscribe to L2 book for major coins
            for coin in ["BTC", "ETH", "SOL"]:
                ws.subscribe(
                    {"type": "l2Book", "coin": coin},
                    self._make_dispatcher(channel),
                )

    def _make_dispatcher(self, channel: str):
        """Returns a callback that fans out to all handlers for this channel."""
        handlers = self._handlers[channel]

        def dispatch(msg):
            for handler in handlers:
                asyncio.create_task(handler(msg))

        return dispatch
