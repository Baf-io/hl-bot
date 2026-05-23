"""
hl-bot — entry point
─────────────────────
Wires everything together:
  1. Feed (WebSocket)
  2. Market store
  3. Signal engines (enabled via .env toggles)
  4. Risk manager
  5. Executor
  6. Monitoring / alerts

Start with: python src/main.py
"""
import asyncio
import sys
import os

# Add src/ and project root to path
sys.path.insert(0, os.path.dirname(__file__))                        # src/
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))       # project root (for config/)

from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import settings
from data.feed import HyperliquidFeed
from data.store import MarketStore, FundingSnapshot, TradeSignal
from risk.manager import RiskManager
from execution.executor import Executor
from monitoring.alerts import TelegramAlerter

# Conditionally import enabled strategies
if settings.STRATEGY_FUNDING_CARRY:
    from signals.funding_carry import FundingCarryScanner
if settings.STRATEGY_LEADERBOARD_COPY:
    from signals.leaderboard_copy import LeaderboardCopier
if settings.STRATEGY_CASCADE:
    from signals.cascade_detector import CascadeDetector


# ── Logging setup ──────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level=settings.LOG_LEVEL, colorize=True,
           format="<green>{time:HH:mm:ss}</green> | <level>{level:<8}</level> | {message}")
logger.add("logs/bot.log", rotation="1 day", retention="30 days",
           level="DEBUG", compression="gz")


async def main():
    logger.info("=" * 60)
    logger.info("HL-BOT STARTING")
    logger.info(f"Testnet: {settings.HL_TESTNET}")
    logger.info(f"Strategies: funding={settings.STRATEGY_FUNDING_CARRY} "
                f"leaderboard={settings.STRATEGY_LEADERBOARD_COPY} "
                f"cascade={settings.STRATEGY_CASCADE}")
    logger.info("=" * 60)

    # ── Init components ────────────────────────────────────────────────────────
    store    = MarketStore()
    await store.init_db()

    risk     = RiskManager(portfolio_value_usd=float(os.getenv("PORTFOLIO_USD", 1000)))
    risk.store = store  # allow executor to log trades

    executor = Executor(risk)
    executor.init_client()

    alerter  = TelegramAlerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    feed     = HyperliquidFeed()

    signal_queue: asyncio.Queue[TradeSignal] = asyncio.Queue()

    # ── Wire feed → store ──────────────────────────────────────────────────────
    async def on_funding(msg):
        # Parse funding update from HL and push to store
        # Schema: {"coin": "BTC", "funding": "0.0005", "openInterest": "...", "markPx": "..."}
        try:
            ctx = msg.get("data", {})
            for item in (ctx if isinstance(ctx, list) else [ctx]):
                coin = item.get("coin") or item.get("name")
                if not coin:
                    continue
                snap = FundingSnapshot(
                    coin=coin,
                    rate_8h=float(item.get("funding", 0)),
                    open_interest=float(item.get("openInterest", 0)),
                    mark_price=float(item.get("markPx", item.get("oraclePx", 0))),
                )
                store.update_funding(snap)
        except Exception as e:
            logger.debug(f"on_funding parse error: {e}")

    async def on_mids(msg):
        try:
            mids = msg.get("data", {}).get("mids", {})
            for coin, price_str in mids.items():
                store.update_mid(coin, float(price_str))
        except Exception as e:
            logger.debug(f"on_mids parse error: {e}")

    async def on_orderbook(msg):
        try:
            data = msg.get("data", {})
            coin = data.get("coin")
            if coin:
                store.update_orderbook(coin, data)
        except Exception as e:
            logger.debug(f"on_orderbook parse error: {e}")

    feed.subscribe("funding", on_funding)
    feed.subscribe("allMids", on_mids)
    feed.subscribe("orderbook", on_orderbook)

    # ── Wire signal engines ────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    if settings.STRATEGY_FUNDING_CARRY:
        carry = FundingCarryScanner(store)

        async def run_carry_scan():
            signals = await carry.scan()
            for sig in signals:
                await executor.enqueue(sig)

        scheduler.add_job(run_carry_scan, "interval", seconds=30, id="funding_carry")
        logger.info("Strategy FUNDING CARRY enabled")

    if settings.STRATEGY_LEADERBOARD_COPY:
        copier = LeaderboardCopier(store, feed)
        copier.set_signal_queue(signal_queue)

        async def relay_signals():
            while not signal_queue.empty():
                sig = await signal_queue.get()
                await executor.enqueue(sig)

        scheduler.add_job(copier.refresh_leaderboard, "interval", seconds=60, id="lb_refresh")
        scheduler.add_job(relay_signals, "interval", seconds=1, id="lb_relay")
        logger.info("Strategy LEADERBOARD COPY enabled")

    if settings.STRATEGY_CASCADE:
        cascade = CascadeDetector(store)

        async def on_price_tick_for_cascade(msg):
            mids = msg.get("data", {}).get("mids", {})
            for coin, price_str in mids.items():
                sigs = await cascade.on_price_update(coin, float(price_str))
                for sig in sigs:
                    await executor.enqueue(sig)

        feed.subscribe("allMids", on_price_tick_for_cascade)
        logger.info("Strategy CASCADE enabled")

    # Daily summary at 23:55 UTC
    async def daily_summary():
        await alerter.daily_summary(risk.status())

    scheduler.add_job(daily_summary, "cron", hour=23, minute=55, id="daily_summary")
    scheduler.start()

    # ── Run ────────────────────────────────────────────────────────────────────
    await alerter.send("🤖 *HL-Bot started*")
    await asyncio.gather(
        feed.run(),
        executor.run(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down…")
