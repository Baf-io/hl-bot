#!/usr/bin/env python3
"""
0x78aa BTC exit-watcher — standalone, runs on the VPS as `hl-78aa-tracker.service`.

Purpose: our manual BTC tracker long (opened 2026-05-25, $300 isolated margin @ 3x,
hard stop @ $73k) follows the SOURCE trader's DIRECTION but has NO auto take-profit.
This watcher closes OUR BTC the instant the source flattens or flips short — so profit
is banked even with the user's machine off and no interactive session running.

It is INTENTIONALLY independent of hl-bot.service (which is walled off from this
position). The only autonomous actors on this trade are: this watcher (exit-on-his-exit)
and the native $73k stop on the exchange (downside backstop).

Safety:
- Acts ONLY on a SUCCESSFUL read showing the source no longer net-long (transient API
  errors skip the tick — never close on a failed read).
- Closes via reduce-only market_close, verifies the fill, retries.
- If our BTC is already flat (stop hit / manual close), it cleans up and exits.
- Exits 0 once the job is done; systemd won't restart a clean exit.
"""
import os, sys, time

sys.path.insert(0, "/root/hl-bot")
sys.path.insert(0, "/root/hl-bot/src")

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger
from config import settings

SOURCE   = "0x78aa6328eae8028a089c35d2819f79c78de2a7e5"
COIN     = "BTC"
POLL_S   = 15
EXIT_EPS = 0.01          # source long <= this (or negative) => treat as flattened/flipped


def _szi(info: Info, addr: str) -> float:
    """Net signed BTC size for `addr` (long>0, short<0, 0=flat). Raises on API failure."""
    st = info.user_state(addr)
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        if p.get("coin") == COIN:
            return float(p.get("szi", 0))
    return 0.0


def _close_ours(ex: Exchange, info: Info, addr: str) -> bool:
    for attempt in range(4):
        try:
            r = ex.market_close(COIN)
            st = r["response"]["data"]["statuses"][0]
            if "filled" in st:
                f = st["filled"]
                logger.success(f"[78aa-watch] CLOSED our {COIN}: {f['totalSz']} @ ${f['avgPx']}")
                return True
            logger.warning(f"[78aa-watch] close attempt {attempt} status: {st}")
        except Exception as e:
            logger.error(f"[78aa-watch] close attempt {attempt} EXC: {e}")
        time.sleep(2)
    return False


def _cancel_our_stops(ex: Exchange, info: Info, addr: str):
    try:
        for o in info.open_orders(addr):
            if o.get("coin") == COIN:
                ex.cancel(COIN, o["oid"])
                logger.info(f"[78aa-watch] cancelled leftover {COIN} order oid={o['oid']}")
    except Exception as e:
        logger.warning(f"[78aa-watch] stop-cancel skipped: {e}")


def main():
    wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
    ex   = Exchange(wallet, constants.MAINNET_API_URL, account_address=settings.HL_WALLET_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    me   = settings.HL_WALLET_ADDRESS

    logger.info(f"[78aa-watch] start | source={SOURCE[:10]}… coin={COIN} poll={POLL_S}s "
                f"trigger: source long <= {EXIT_EPS} (flat/flip)")

    # Bail early if we hold nothing to track.
    try:
        ours = _szi(info, me)
    except Exception as e:
        logger.error(f"[78aa-watch] initial our-state read failed: {e}; will retry in loop")
        ours = None
    if ours is not None and abs(ours) < EXIT_EPS:
        logger.info("[78aa-watch] our BTC is already flat — nothing to track. Cleaning up + exit.")
        _cancel_our_stops(ex, info, me)
        return

    while True:
        try:
            his  = _szi(info, SOURCE)
            ours = _szi(info, me)
        except Exception as e:
            logger.warning(f"[78aa-watch] read failed (skipping tick, NOT closing): {e}")
            time.sleep(POLL_S)
            continue

        if abs(ours) < EXIT_EPS:
            logger.info("[78aa-watch] our BTC went flat (stop hit / manual close). Cleanup + exit.")
            _cancel_our_stops(ex, info, me)
            return

        if his <= EXIT_EPS:
            logger.success(f"[78aa-watch] SOURCE exited (his BTC szi={his}) → closing ours ({ours}).")
            if _close_ours(ex, info, me):
                _cancel_our_stops(ex, info, me)
                logger.success("[78aa-watch] done — banked on his exit. Exiting service.")
                return
            logger.error("[78aa-watch] close FAILED after retries — leaving $73k stop in place, will retry next tick.")
        else:
            logger.debug(f"[78aa-watch] holding: his={his} ours={ours}")
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
