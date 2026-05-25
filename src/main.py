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
if settings.TRACKER_ENABLED:
    from signals.lev_tracker import LevTracker


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

    alerter  = TelegramAlerter(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_CHAT_ID,
                               ntfy_topic=settings.NTFY_TOPIC, ntfy_server=settings.NTFY_SERVER)
    squeeze  = SqueezeGuard(store, alerter)
    executor.squeeze_guard = squeeze   # give executor access to fire lifecycle events
    executor.alerter = alerter         # B1: alert on no-stop force-close
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
        from datetime import datetime as _dt, timedelta as _td
        copier = LeaderboardCopier(store, feed)
        copier.set_signal_queue(signal_queue)
        copier.risk = risk   # reconcile needs to see what we already hold
        copier.alerter = alerter   # notify on trend-confirmed add-mirror entries

        # Load the trader list + specialist routing once at startup, refresh every 5 min.
        scheduler.add_job(copier.refresh_leaderboard, "interval", minutes=5, id="lb_refresh")
        scheduler.add_job(copier.refresh_leaderboard, "date", id="lb_startup")

        # State-based reconcile: poll each trader's NET position and mirror only real
        # position changes. Replaces the fill-stream handler + one-shot backfill that
        # caused the open/close fee-bleed churn. First run a few seconds after startup
        # so the trader list and executor position-sync are both in place.
        async def run_reconcile():
            await copier.reconcile()

        scheduler.add_job(run_reconcile, "interval",
                          seconds=settings.COPY_RECONCILE_INTERVAL_S, id="lb_reconcile")
        scheduler.add_job(run_reconcile, "date",
                          run_date=_dt.now() + _td(seconds=8), id="lb_reconcile_startup")
        logger.info("Strategy LEADERBOARD COPY enabled (state-based reconcile)")

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
    # Philosophy: trust the traders we whitelisted.
    #
    # Primary exits (handled elsewhere):
    #   • Trader closes their position → leaderboard copier sends exit signal
    #   • Native SL at -3% on HL exchange → fires even if bot is offline
    #   • Native TP at +8% on HL exchange → locks in profit automatically
    #
    # Guardian only handles edge cases the above can't catch:
    #   ZOMBIE  (>12h open) → WebSocket probably missed the trader's close signal
    #   NUCLEAR (>-10% loss) → SL failed somehow (extreme gap / liquidation cascade)
    #
    # We do NOT close on small losses — these traders hold through dips.
    # Cutting them at -1% and watching them recover to +5% is the old mistake.

    ZOMBIE_HOURS        = 72.0   # macro traders hold for days — 72h before force-close
    # Nuclear trigger is on MARGIN lost, not raw price move. A 20% *price* move on a
    # 10x position is already past liquidation, so the old price-based threshold could
    # never fire on a leveraged copy. 70% margin loss fires before the ~100% (liquidation)
    # point at every leverage while staying clear of the dips these traders hold through.
    NUCLEAR_MARGIN_LOSS = 0.70

    async def position_guardian():
        from datetime import datetime, timezone
        was_halted = False   # track the trading-halt transition for a one-shot phone push
        while True:
            await asyncio.sleep(60)   # check every minute — less noise
            now = datetime.now(timezone.utc).replace(tzinfo=None)

            # Phone alert exactly once when the bot transitions into a trading halt
            # (daily loss limit hit → "we're really down and the bot stopped trading").
            if risk._trading_halted and not was_halted:
                await alerter.halt_alert(risk._daily_pnl / risk.portfolio_value)
            was_halted = risk._trading_halted
            for pos in list(risk.open_positions):
                current_price = store.latest_mid(pos.coin)
                if current_price:
                    risk.update_unrealized(pos.coin, current_price)

                # pnl as % of MARGIN committed (notional / leverage), not raw price move,
                # so the nuclear trigger is leverage-aware. pos.size_usd is NOTIONAL.
                margin_usd = pos.size_usd / max(pos.leverage, 1.0)
                pnl_pct = pos.unrealized_pnl / margin_usd if margin_usd else 0
                age_h   = (now - pos.opened_at).total_seconds() / 3600

                # Log status every hour so we can see what's running
                if int(age_h * 60) % 60 == 0 and age_h > 0:
                    logger.debug(
                        f"[Guardian] {pos.coin} pos#{pos.id} | "
                        f"age={age_h:.1f}h pnl={pnl_pct:+.1%} [{pos.strategy}]"
                    )

                exit_kind = None
                detail    = None
                if age_h >= ZOMBIE_HOURS:
                    exit_kind, detail = "zombie", f"zombie {age_h:.1f}h — missed close signal?"
                elif pnl_pct < -NUCLEAR_MARGIN_LOSS:
                    exit_kind, detail = "nuclear", f"nuclear loss {pnl_pct:.1%} of margin — SL failed"

                if exit_kind:
                    logger.warning(f"[Guardian] 🚨 FORCE CLOSE {pos.coin} pos#{pos.id} — {detail}")
                    # Phone alert: a bad action was taken (force-close). pnl is the live
                    # unrealized estimate — the exact realized figure lands in the logs.
                    await alerter.force_close_alert(pos.coin, exit_kind, detail, pos.unrealized_pnl)
                    # Exit signal direction = the direction WE HOLD (the side being closed).
                    # _close_position matches the held direction, not the offsetting side.
                    # `reason` carries the bare kind so the executor's coin-only fallback
                    # (step 3) recognises it. Squeeze notification now happens inside
                    # _close_position AFTER the close is confirmed filled — so we don't
                    # mark it closed here, where the close might still fail.
                    await executor.enqueue(TradeSignal(
                        strategy=pos.strategy,
                        coin=pos.coin,
                        direction=pos.direction,
                        size_usd=0,
                        confidence=1.0,
                        meta={"action": "exit", "reason": exit_kind, "detail": detail},
                    ))

    # Daily summary at 23:55 UTC
    async def daily_summary():
        await alerter.daily_summary(risk.status())
        await alerter.send(squeeze.summary())

    scheduler.add_job(daily_summary, "cron", hour=23, minute=55, id="daily_summary")
    scheduler.start()

    # ── Run ────────────────────────────────────────────────────────────────────
    # Event-driven relay: leaderboard signals forwarded to executor immediately.
    # Replaces the old 1-second scheduler poll that added up to 1000ms of exit latency.
    async def relay_signals():
        while True:
            sig = await signal_queue.get()
            await executor.enqueue(sig)

    # Walled-off lev-tracker sleeve (mirrors one trader on TRACKER_COINS, isolated).
    tasks = [feed.run(), executor.run(), position_guardian(), relay_signals()]
    if settings.TRACKER_ENABLED:
        tasks.append(LevTracker(alerter=alerter).run())

    await alerter.send("🤖 *HL-Bot started*")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down…")
