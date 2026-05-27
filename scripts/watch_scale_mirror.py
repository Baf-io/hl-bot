#!/usr/bin/env python3
"""
PROPORTIONAL SCALE-MIRROR — multi-coin, runs on a walled-off SUBACCOUNT.

For a source that BUILDS/SCALES positions (adds + trims, holds for days) rather than one-shotting,
a fresh-entry direction-copy misses his real edge. This mirrors his SIGNED size per coin as a fixed
fraction of his book, every poll: he scales in → we scale in; he trims → we trim; he flips → we flip;
he flat → we flat. Same model as the 78aa BTC tracker, generalized to a coin allowlist on a sub.

  target_per_coin = PEG_FRAC × his_signed_size, capped at MAX_NOTIONAL_PER (per leg) and the book
  capped at MAX_NOTIONAL_TOTAL. Only coins in MIRROR_COINS are mirrored (e.g. his liquid majors).

NOTE on a small account: at a $100 sub the per-leg cap binds on his large legs, so we hold capped
mini-versions and faithfully follow his flat/flip/large-trim signals (the peg is exact only below cap).

Guards: per-leg reduce-only dead-man stop at STOP_PCT, daily realized kill-switch (HALT_USD, all
coins) → flatten+idle to next UTC day, clean-read-only, rebalance deadband (MIN_REBAL_USD), isolated.
Config (env): MIRROR_NAME, MIRROR_SOURCE, MIRROR_SUB, MIRROR_COINS(csv), MIRROR_PEG_FRAC,
              MIRROR_MAX_PER, MIRROR_MAX_TOTAL, MIRROR_LEV, MIRROR_STOP_PCT, MIRROR_HALT_USD,
              MIRROR_MIN_REBAL, MIRROR_POLL_S.
Manage: systemctl {status,stop} hl-<name>-mirror
"""
import sys, os, math, time, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger
from config import settings

NAME      = os.getenv("MIRROR_NAME", "mirror")
SOURCE    = os.getenv("MIRROR_SOURCE", "").lower()
SUB       = os.getenv("MIRROR_SUB", "").lower()
COINS     = [c.strip().upper() for c in os.getenv("MIRROR_COINS", "BTC").split(",") if c.strip()]
PEG_FRAC  = float(os.getenv("MIRROR_PEG_FRAC", "0.009"))
MAX_PER   = float(os.getenv("MIRROR_MAX_PER", "120"))      # per-leg notional cap ($)
MAX_TOTAL = float(os.getenv("MIRROR_MAX_TOTAL", "270"))    # whole-book notional cap ($)
LEV       = int(os.getenv("MIRROR_LEV", "3"))
STOP_PCT  = float(os.getenv("MIRROR_STOP_PCT", "0.08"))
HALT_USD  = float(os.getenv("MIRROR_HALT_USD", "25"))
MIN_REBAL = float(os.getenv("MIRROR_MIN_REBAL", "12"))     # anti-churn deadband ($)
POLL_S    = int(os.getenv("MIRROR_POLL_S", "20"))
FLAT_EPS  = 1e-9
_SZDEC    = {}

def _rnd(coin, sz): return round(sz, _SZDEC.get(coin, 4))
def _round_px(p):
    if p <= 0: return p
    return round(p, max(0, 5 - int(math.floor(math.log10(abs(p)))) - 1))

def _pos(info, addr, coin):
    st = info.user_state(addr)
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        if p.get("coin") == coin: return float(p.get("szi", 0)), float(p.get("entryPx") or 0)
    return 0.0, 0.0

def _today_realized(info, addr):
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ms = int(midnight.timestamp() * 1000); pnl = 0.0
    for f in info.user_fills(addr):
        if f.get("coin") in COINS and f.get("time", 0) >= ms: pnl += float(f.get("closedPnl", 0))
    return pnl

def _cancel(ex, info, addr, coin):
    try:
        for o in info.open_orders(addr):
            if o.get("coin") == coin: ex.cancel(coin, o["oid"])
    except Exception as e: logger.warning(f"[{NAME}] cancel skip {coin}: {e}")

def _has_stop(info, addr, coin):
    try: return any(o.get("coin") == coin for o in info.open_orders(addr))
    except Exception: return True

def _ensure_stop(ex, info, addr, coin, is_long, size, entry):
    _cancel(ex, info, addr, coin)
    stop_px = _round_px(entry * (1 - STOP_PCT)) if is_long else _round_px(entry * (1 + STOP_PCT))
    try:
        r = ex.order(coin, (not is_long), _rnd(coin, size), stop_px,
                     {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
        st = r["response"]["data"]["statuses"][0]; oid = (st.get("resting") or {}).get("oid")
        if "error" in st or oid is None: logger.error(f"[{NAME}] {coin} stop fail: {st}"); return False
        time.sleep(1.0)
        resting = any(o.get("oid") == oid for o in info.open_orders(addr))
        logger.info(f"[{NAME}] {coin} stop {'L' if is_long else 'S'} {_rnd(coin,size)} @ {stop_px} {'OK' if resting else 'NOT-RESTING'}")
        return resting
    except Exception as e: logger.error(f"[{NAME}] {coin} stop EXC: {e}"); return False

def _close(ex, info, addr, coin, sz=None):
    for _ in range(4):
        try:
            r = ex.market_close(coin) if sz is None else ex.market_close(coin, sz=_rnd(coin, sz))
            st = r["response"]["data"]["statuses"][0]
            if "filled" in st:
                logger.success(f"[{NAME}] {'CLOSE' if sz is None else 'REDUCE'} {coin} @ ${st['filled']['avgPx']}")
                if sz is None: _cancel(ex, info, addr, coin)
                return True
        except Exception as e: logger.error(f"[{NAME}] {coin} close EXC: {e}")
        time.sleep(2)
    return False

def main():
    if not SOURCE or not SUB: logger.error(f"[{NAME}] MIRROR_SOURCE/SUB unset"); sys.exit(1)
    wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
    ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=SUB)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    for a in info.meta()["universe"]: _SZDEC[a["name"]] = a.get("szDecimals", 4)
    for c in COINS:
        try: ex.update_leverage(LEV, c, False)
        except Exception as e: logger.warning(f"[{NAME}] set lev {c}: {e}")
    logger.info(f"[{NAME}] start | src={SOURCE[:10]}… sub={SUB[:10]}… coins={COINS} | "
                f"peg {PEG_FRAC:.4f}×his @ {LEV}x | cap ${MAX_PER:.0f}/leg ${MAX_TOTAL:.0f}/book | "
                f"stop {STOP_PCT:.0%} | halt ${HALT_USD:.0f}/d | PROPORTIONAL scale-in/out/flip/flat")

    while True:
        try:
            mids = info.all_mids(); realized = _today_realized(info, SUB)
            snap = {c: (_pos(info, SOURCE, c)[0], _pos(info, SUB, c)) for c in COINS}
        except Exception as e:
            logger.warning(f"[{NAME}] read failed (skip): {e}"); time.sleep(POLL_S); continue

        if realized <= -HALT_USD:
            for c in COINS:
                if abs(snap[c][1][0]) > FLAT_EPS:
                    logger.error(f"[{NAME}] DAILY HALT realized ${realized:.0f} → flatten {c}"); _close(ex, info, SUB, c)
            time.sleep(POLL_S); continue

        # current book notional (for total cap)
        book_usd = sum(abs(snap[c][1][0]) * float(mids.get(c, 0) or 0) for c in COINS)

        for coin in COINS:
            his = snap[coin][0]; ours, entry = snap[coin][1]
            try: px = float(mids[coin])
            except Exception: continue
            target = PEG_FRAC * his
            cap = MAX_PER / px
            if abs(target) > cap: target = math.copysign(cap, target)
            # whole-book cap: if adding would breach total, clamp this leg's growth
            if abs(target) > abs(ours):
                headroom = max(0.0, MAX_TOTAL - (book_usd - abs(ours) * px))
                if abs(target) * px > headroom: target = math.copysign(headroom / px, target)
            target = _rnd(coin, target)
            same_side = (target == 0) or (ours == 0) or (math.copysign(1, target) == math.copysign(1, ours))

            if abs(ours) > FLAT_EPS and abs(target) > FLAT_EPS and not same_side:
                logger.success(f"[{NAME}] FLIP {coin} (his={his}) → close our {'long' if ours>0 else 'short'} first")
                _close(ex, info, SUB, coin); continue
            delta = target - ours
            if abs(delta) * px < MIN_REBAL:
                if abs(ours) > FLAT_EPS and not _has_stop(info, SUB, coin):
                    _ensure_stop(ex, info, SUB, coin, ours > 0, abs(ours), entry)
                continue
            if abs(target) < FLAT_EPS:
                logger.success(f"[{NAME}] {coin} source flat (his={his}) → close ours ({ours})")
                _close(ex, info, SUB, coin)
            elif abs(target) > abs(ours):
                side_buy = target > 0
                logger.success(f"[{NAME}] SCALE IN {coin} {'long' if side_buy else 'short'} +{_rnd(coin,abs(delta))} "
                               f"→ tgt {target} (his={his}, ${abs(target)*px:,.0f})")
                try: ex.market_open(coin, side_buy, _rnd(coin, abs(delta)))
                except Exception as e: logger.error(f"[{NAME}] {coin} scale-in EXC: {e}"); continue
                time.sleep(1.0); ns, ne = _pos(info, SUB, coin)
                if abs(ns) > FLAT_EPS: _ensure_stop(ex, info, SUB, coin, ns > 0, abs(ns), ne)
            else:
                logger.success(f"[{NAME}] SCALE OUT {coin} {_rnd(coin,abs(delta))} → tgt {target} (his={his})")
                _close(ex, info, SUB, coin, sz=abs(delta))
                time.sleep(1.0); ns, ne = _pos(info, SUB, coin)
                if abs(ns) > FLAT_EPS: _ensure_stop(ex, info, SUB, coin, ns > 0, abs(ns), ne)
                else: _cancel(ex, info, SUB, coin)
        time.sleep(POLL_S)

if __name__ == "__main__": main()
