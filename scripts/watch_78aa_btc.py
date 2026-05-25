#!/usr/bin/env python3
"""
0x78aa BTC PROPORTIONAL MIRROR — standalone, runs on the VPS as `hl-78aa-tracker.service`.

Continuously sizes OUR isolated BTC position to a FIXED FRACTION of the source trader's
BTC position: he scales in → we scale in; he trims → we trim; he flips → we flip; he goes
flat → we go flat. Runs 24/7 independent of any interactive session or the user's machine.

PEG: our_target_size = PEG_FRAC × his_size   (PEG_FRAC set from the seed state below:
     our 0.01165 BTC ↔ his 1.5 BTC ⇒ ~0.78% of his book, signed → follows shorts too).

Guardrails (real money, unattended):
- NOTIONAL CAP — our position is capped at MAX_NOTIONAL regardless of how big he goes.
- PER-LEG STOP — every leg carries a reduce-only native stop at STOP_PCT from our avg
  entry (= ~$73k at the seed long entry), resized on every rebalance, eyes-on-book confirmed.
- DAILY KILL-SWITCH — if this sleeve's realized BTC PnL today (UTC) ≤ -HALT_USD, it flattens
  and idles until the next UTC day.
- CLEAN-READ ONLY — a failed API read skips the tick (never trades on bad data).
- REBALANCE DEADBAND — only acts when the gap to target ≥ MIN_REBAL_USD (no fee churn).
"""
import sys, time, math, datetime as dt

sys.path.insert(0, "/root/hl-bot")
sys.path.insert(0, "/root/hl-bot/src")

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger
from config import settings

SOURCE        = "0x78aa6328eae8028a089c35d2819f79c78de2a7e5"
COIN          = "BTC"
POLL_S        = 15
PEG_FRAC      = 0.01165 / 1.5     # our seed size ÷ his seed size → fraction of his book we hold
MAX_NOTIONAL  = 1500.0           # hard cap on OUR exposure ($) — runaway protection
MIN_REBAL_USD = 40.0             # deadband: ignore gaps smaller than this (anti-churn)
STOP_PCT      = 0.0553           # protective stop distance from avg entry (= ~$73k at seed entry)
HALT_USD      = 150.0            # daily realized-loss kill-switch on this sleeve
LEV           = 3
SZ_DEC        = 5                # BTC lot precision
FLAT_EPS      = 0.0002           # |size| below this = flat


def _rnd(sz: float) -> float:
    return round(sz, SZ_DEC)


def _pos(info: Info, addr: str):
    """(signed_size, avg_entry) for COIN. Raises on API failure."""
    st = info.user_state(addr)
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        if p.get("coin") == COIN:
            return float(p.get("szi", 0)), float(p.get("entryPx") or 0)
    return 0.0, 0.0


def _today_realized(info: Info, addr: str) -> float:
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ms = int(midnight.timestamp() * 1000)
    pnl = 0.0
    for f in info.user_fills(addr):
        if f.get("coin") == COIN and f.get("time", 0) >= ms:
            pnl += float(f.get("closedPnl", 0))
    return pnl


def _cancel_btc_orders(ex: Exchange, info: Info, addr: str):
    try:
        for o in info.open_orders(addr):
            if o.get("coin") == COIN:
                ex.cancel(COIN, o["oid"])
    except Exception as e:
        logger.warning(f"[mirror] cancel skipped: {e}")


def _has_btc_stop(info: Info, addr: str) -> bool:
    try:
        return any(o.get("coin") == COIN for o in info.open_orders(addr))
    except Exception:
        return True   # assume yes on read failure → don't double-place


def _ensure_stop(ex: Exchange, info: Info, addr: str, is_long: bool, size: float, entry: float) -> bool:
    """Cancel existing BTC orders, place ONE reduce-only stop sized to `size`, confirm resting."""
    _cancel_btc_orders(ex, info, addr)
    stop_px = round(entry * (1 - STOP_PCT)) if is_long else round(entry * (1 + STOP_PCT))
    close_is_buy = not is_long
    try:
        r = ex.order(COIN, close_is_buy, _rnd(size), stop_px,
                     {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}},
                     reduce_only=True)
        st = r["response"]["data"]["statuses"][0]
        oid = (st.get("resting") or {}).get("oid")
        if "error" in st or oid is None:
            logger.error(f"[mirror] stop place failed: {st}")
            return False
        time.sleep(1.0)
        resting = any(o.get("oid") == oid for o in info.open_orders(addr))
        logger.info(f"[mirror] stop {'LONG' if is_long else 'SHORT'} {_rnd(size)} {COIN} @ ${stop_px:,} "
                    f"{'CONFIRMED' if resting else 'NOT RESTING'} oid={oid}")
        return resting
    except Exception as e:
        logger.error(f"[mirror] stop EXC: {e}")
        return False


def _close_all(ex: Exchange, info: Info, addr: str) -> bool:
    for _ in range(4):
        try:
            r = ex.market_close(COIN)
            st = r["response"]["data"]["statuses"][0]
            if "filled" in st:
                logger.success(f"[mirror] CLOSED all {COIN} @ ${st['filled']['avgPx']}")
                _cancel_btc_orders(ex, info, addr)
                return True
        except Exception as e:
            logger.error(f"[mirror] close EXC: {e}")
        time.sleep(2)
    return False


def main():
    wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
    ex   = Exchange(wallet, constants.MAINNET_API_URL, account_address=settings.HL_WALLET_ADDRESS)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    me   = settings.HL_WALLET_ADDRESS
    try:
        ex.update_leverage(LEV, COIN, False)   # isolated, walled off from cross book
    except Exception as e:
        logger.warning(f"[mirror] set leverage: {e}")

    logger.info(f"[mirror] start | source={SOURCE[:10]}… peg={PEG_FRAC:.5f}×his | "
                f"cap ${MAX_NOTIONAL:.0f} | stop {STOP_PCT*100:.2f}% | halt ${HALT_USD:.0f}/day | poll {POLL_S}s")

    while True:
        try:
            his, _      = _pos(info, SOURCE)
            ours, entry = _pos(info, me)
            realized    = _today_realized(info, me)
            px          = float(info.all_mids()[COIN])
        except Exception as e:
            logger.warning(f"[mirror] read failed (skip tick): {e}")
            time.sleep(POLL_S); continue

        # ── daily kill-switch ──
        if realized <= -HALT_USD:
            if abs(ours) > FLAT_EPS:
                logger.error(f"[mirror] DAILY HALT: realized ${realized:.0f} ≤ -${HALT_USD:.0f} → flatten + idle")
                _close_all(ex, info, me)
            time.sleep(POLL_S); continue

        # ── target (signed), capped ──
        target = PEG_FRAC * his
        cap = MAX_NOTIONAL / px
        if abs(target) > cap:
            target = math.copysign(cap, target)
        target = _rnd(target)

        same_side = (target == 0) or (ours == 0) or (math.copysign(1, target) == math.copysign(1, ours))

        # ── flip: close current side first, re-read next tick ──
        if abs(ours) > FLAT_EPS and abs(target) > FLAT_EPS and not same_side:
            logger.success(f"[mirror] FLIP (his={his}) → close our {'long' if ours>0 else 'short'} first")
            _close_all(ex, info, me)
            time.sleep(POLL_S); continue

        delta = target - ours
        if abs(delta) * px < MIN_REBAL_USD:
            # within deadband — just make sure a stop is in place for whatever we hold
            if abs(ours) > FLAT_EPS and not _has_btc_stop(info, me):
                _ensure_stop(ex, info, me, ours > 0, abs(ours), entry)
            logger.debug(f"[mirror] hold his={his} ours={ours} target={target} (Δ${abs(delta)*px:.0f}<${MIN_REBAL_USD:.0f})")
            time.sleep(POLL_S); continue

        if abs(target) < FLAT_EPS:
            logger.success(f"[mirror] source flat (his={his}) → close ours ({ours})")
            _close_all(ex, info, me)
        elif abs(target) > abs(ours):                       # SCALE IN (or open from flat)
            side_buy = target > 0
            logger.success(f"[mirror] SCALE IN {'long' if side_buy else 'short'} +{_rnd(abs(delta))} "
                           f"→ target {target} (his={his}, ${abs(target)*px:,.0f})")
            try:
                ex.market_open(COIN, side_buy, _rnd(abs(delta)))
            except Exception as e:
                logger.error(f"[mirror] scale-in EXC: {e}"); time.sleep(POLL_S); continue
            time.sleep(1.0)
            new_sz, new_entry = _pos(info, me)
            _ensure_stop(ex, info, me, new_sz > 0, abs(new_sz), new_entry)
        else:                                               # SCALE OUT (reduce toward target)
            logger.success(f"[mirror] SCALE OUT {_rnd(abs(delta))} → target {target} (his={his})")
            try:
                ex.market_close(COIN, sz=_rnd(abs(delta)))   # reduce-only partial
            except Exception as e:
                logger.error(f"[mirror] scale-out EXC: {e}"); time.sleep(POLL_S); continue
            time.sleep(1.0)
            new_sz, new_entry = _pos(info, me)
            if abs(new_sz) > FLAT_EPS:
                _ensure_stop(ex, info, me, new_sz > 0, abs(new_sz), new_entry)
            else:
                _cancel_btc_orders(ex, info, me)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
