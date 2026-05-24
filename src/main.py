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
from monitoring.squeeze_guard import SqueezeGuard

# Conditionally import enabled strategies
if settings.STRATEGY_FUNDING_CARRY:
    from signals.funding_carry import FundingCarryScanner
if settings.STRATEGY_LEADERBOARD_COPY:
    from signals.leaderboard_copy import LeaderboardCopier
if settings.STRATEGY_CASCADE:
    from signals.cascade_detector import CascadeDetector
if settings.STRATEGY_OI_SQUEEZE:
    from signals.oi_funding_squeeze import OIFundingSqueeze
if settings.STRATEGY_STAT_ARB:
    from signals.stat_arb import StatArbScanner
if settings.STRATEGY_MOMENTUM:
    from signals.momentum_ignition import MomentumIgnition


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
                f"cascade={settings.STRATEGY_CASCADE} "
                f"oi_squeeze={settings.STRATEGY_OI_SQUEEZE} "
                f"stat_arb={settings.STRATEGY_STAT_ARB} "
                f"momentum={settings.STRATEGY_MOMENTUM}")
    logger.info("=" * 60)

    # ── Init components ────────────────────────────────────────────────────────
    store    = MarketStore()
    await store.init_db()

    risk     = RiskManager(portfolio_value_usd=float(os.getenv("PORTFOLIO_USD", 1000)))
    risk.store = store  # allow executor to log trades

    executor = Executor(risk)
    executor.init_client()

    alerter  = TelegramAlerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID)
    squeeze  = SqueezeGuard(store, alerter)
    executor.squeeze_guard = squeeze   # give executor access to fire lifecycle events
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
                price = float(price_str)
                store.update_mid(coin, price)
                squeeze.update_price(coin, price)   # feed price ticks to squeeze guard
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

        # Run immediately on startup, then refresh list every 5 min
        scheduler.add_job(copier.refresh_leaderboard, "interval", minutes=5, id="lb_refresh")
        scheduler.add_job(relay_signals, "interval", seconds=1, id="lb_relay")
        # Trigger immediately so we don't wait 5 min for first subscription
        scheduler.add_job(copier.refresh_leaderboard, "date", id="lb_startup")
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

    if settings.STRATEGY_OI_SQUEEZE:
        oi_squeeze = OIFundingSqueeze(store)

        async def run_oi_squeeze():
            signals = await oi_squeeze.scan()
            for sig in signals:
                await executor.enqueue(sig)

        scheduler.add_job(run_oi_squeeze, "interval", seconds=30, id="oi_squeeze")
        logger.info("Strategy OI/FUNDING SQUEEZE enabled")

    if settings.STRATEGY_STAT_ARB:
        stat_arb = StatArbScanner(store)

        async def run_stat_arb():
            signals = await stat_arb.scan()
            for sig in signals:
                await executor.enqueue(sig)

        scheduler.add_job(run_stat_arb, "interval", seconds=15, id="stat_arb")
        logger.info("Strategy STAT ARB enabled")

    if settings.STRATEGY_MOMENTUM:
        momentum = MomentumIgnition(store)

        async def on_price_tick_for_momentum(msg):
            mids = msg.get("data", {}).get("mids", {})
            for coin, price_str in mids.items():
                sigs = await momentum.on_price_update(coin, float(price_str))
                for sig in sigs:
                    await executor.enqueue(sig)

        feed.subscribe("allMids", on_price_tick_for_momentum)
        logger.info("Strategy MOMENTUM IGNITION enabled")

    # ── Position guardian ─────────────────────────────────────────────────────
    # Simple rules while we find the right traders to whitelist:
    #
    #   RED  (pnl < 0)   → close immediately, every tick. No holding losers.
    #   GREEN (pnl >= 0) → hands off. Native -3% SL on HL protects them.
    #                      Copy trader's close signal will exit them too.
    #   ZOMBIE (>8h old) → close regardless, just in case.

    MAX_HOLD_HOURS = 8.0

    async def position_guardian():
        from datetime import datetime
        while True:
            await asyncio.sleep(30)
            now = datetime.utcnow()
            for pos in list(risk.open_positions):
                current_price = store.latest_mid(pos.coin)
                if current_price:
                    risk.update_unrealized(pos.coin, current_price)

                pnl_pct = pos.unrealized_pnl / pos.size_usd if pos.size_usd else 0
                age_h   = (now - pos.opened_at).total_seconds() / 3600

                reason = None
                if pnl_pct < 0:
                    reason = f"red {pnl_pct:.1%}"
                elif age_h >= MAX_HOLD_HOURS:
                    reason = f"max hold {age_h:.1f}h"
                # green and <8h → do nothing, native SL on HL handles -3% reversal

                if reason:
                    logger.info(f"[Guardian] 🔴 CLOSING {pos.coin} pos#{pos.id} — {reason}")
                    exit_kind = "stop_loss" if "red" in reason else "max_hold"
                    squeeze.on_position_closed(pos.id, current_price or pos.entry_price, exit_kind)
                    await executor.enqueue(TradeSignal(
                        strategy=pos.strategy,
                        coin=pos.coin,
                        direction="long" if pos.direction == "short" else "short",
                        size_usd=0,
                        confidence=1.0,
                        meta={"action": "exit", "reason": reason},
                    ))

    # Daily summary at 23:55 UTC
    async def daily_summary():
        await alerter.daily_summary(risk.status())
        await alerter.send(squeeze.summary())

    scheduler.add_job(daily_summary, "cron", hour=23, minute=55, id="daily_summary")
    scheduler.start()

    # ── Run ────────────────────────────────────────────────────────────────────
    await alerter.send("🤖 *HL-Bot started*")
    await asyncio.gather(
        feed.run(),
        executor.run(),
        position_guardian(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down…")
