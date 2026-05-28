#!/usr/bin/env python3
"""
0x26fe122c BTC COPY SLEEVE — runs on the dedicated HL SUBACCOUNT ("Sub Baf"), walled off from
the master book (its own position space, collision-free with the 78aa-BTC tracker on master).

Strategy (as designed with user):
  ENTRY  — on the source's NEXT BTC add/open (size increase while WE are flat), at fresh price.
           We do NOT adopt his existing/stale position (baseline-seeded on first tick + persisted).
  EXIT   — when the source FLIPS direction or goes FLAT. We ride his position; we IGNORE his trims.
  SIZE   — scalable: target notional = SUB_equity × MARGIN_PCT × LEV (grows with the sub).
  SL     — dead-man protective stop at STOP_PCT from our entry (reduce-only, eyes-on-book confirmed).
           Backstop only — normally we exit on his flip/flat within one poll.
  TP     — none (exit = his exit).
  Guards — daily realized kill-switch (HALT_USD), clean-read-only, walled-off isolated margin.

Source is a thin-edge BTC round-tripper (May backtest ~breakeven) — this is a controlled live test
of the copy machinery on a capped sub. Swap SOURCE/COIN to retarget. Manage: systemctl {status,stop} hl-26fe-sleeve
"""
import sys, json, os, time, math, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger
from config import settings

SOURCE      = "0x26fe122cc2322fbef86a68d19218500fb3e7b7e5"
SUB         = "0xdac952c2ff2dfd0275baf6c972a7cb6b50142246"   # "Sub Baf"
COIN        = "BTC"
POLL_S      = 20
MARGIN_PCT  = 0.45            # of SUB equity → margin; notional = margin × LEV (scalable)
LEV         = 3
STOP_PCT    = 0.08            # dead-man protective stop distance from entry
HALT_USD    = 40.0           # daily realized-loss kill-switch on this sleeve
EPS_HIS     = 0.05           # his BTC add/open detection threshold (BTC units)
FLAT_EPS    = 0.0002         # our position considered flat below this
SZ_DEC      = 5
STATE       = "/root/hl-bot/data/26fe_sub_state.json"


def _rnd(x): return round(x, SZ_DEC)

def _pos(info, addr):
    """(signed_size, avg_entry) for COIN on addr. Raises on API failure (clean-read)."""
    st = info.user_state(addr)
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        if p.get("coin") == COIN:
            return float(p.get("szi", 0)), float(p.get("entryPx") or 0)
    return 0.0, 0.0

def _equity(info, addr):
    return float(info.user_state(addr)["marginSummary"]["accountValue"])

def _today_realized(info, addr):
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ms = int(midnight.timestamp()*1000); pnl = 0.0
    for f in info.user_fills(addr):
        if f.get("coin") == COIN and f.get("time", 0) >= ms:
            pnl += float(f.get("closedPnl", 0))
    return pnl

def _cancel(ex, info, addr):
    try:
        for o in info.open_orders(addr):
            if o.get("coin") == COIN: ex.cancel(COIN, o["oid"])
    except Exception as e: logger.warning(f"[26fe] cancel skip: {e}")

def _has_stop(info, addr):
    try: return any(o.get("coin") == COIN for o in info.open_orders(addr))
    except Exception: return True

def _ensure_stop(ex, info, addr, is_long, size, entry):
    _cancel(ex, info, addr)
    stop_px = round(entry*(1-STOP_PCT)) if is_long else round(entry*(1+STOP_PCT))
    try:
        r = ex.order(COIN, (not is_long), _rnd(size), stop_px,
                     {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
        st = r["response"]["data"]["statuses"][0]; oid = (st.get("resting") or {}).get("oid")
        if "error" in st or oid is None: logger.error(f"[26fe] stop fail: {st}"); return False
        time.sleep(1.0)
        resting = any(o.get("oid") == oid for o in info.open_orders(addr))
        logger.info(f"[26fe] stop {'LONG' if is_long else 'SHORT'} {_rnd(size)} @ ${stop_px:,} {'OK' if resting else 'NOT RESTING'}")
        return resting
    except Exception as e: logger.error(f"[26fe] stop EXC: {e}"); return False

def _close_all(ex, info, addr):
    for _ in range(4):
        try:
            r = ex.market_close(COIN); st = r["response"]["data"]["statuses"][0]
            if "filled" in st:
                logger.success(f"[26fe] CLOSED {COIN} @ ${st['filled']['avgPx']}"); _cancel(ex, info, addr); return True
        except Exception as e: logger.error(f"[26fe] close EXC: {e}")
        time.sleep(2)
    return False

def _load_state():
    try: return json.load(open(STATE))
    except Exception: return {}
def _save_state(d): json.dump(d, open(STATE, "w"))


def main():
    wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
    ex = Exchange(wallet, constants.MAINNET_API_URL, account_address=SUB)   # trade on the SUB
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    try: ex.update_leverage(LEV, COIN, False)
    except Exception as e: logger.warning(f"[26fe] set lev: {e}")
    logger.info(f"[26fe] start | source={SOURCE[:10]}… sub={SUB[:10]}… {COIN} | margin {MARGIN_PCT:.0%}×eq @ {LEV}x "
                f"| stop {STOP_PCT:.0%} | halt ${HALT_USD:.0f}/day | enter-on-his-add, exit-on-flip/flat")
    state = _load_state()

    while True:
        try:
            his, _      = _pos(info, SOURCE)
            ours, entry = _pos(info, SUB)
            eq          = _equity(info, SUB)
            realized    = _today_realized(info, SUB)
            px          = float(info.all_mids()[COIN])
        except Exception as e:
            logger.warning(f"[26fe] read failed (skip): {e}"); time.sleep(POLL_S); continue

        # kill-switch
        if realized <= -HALT_USD:
            if abs(ours) > FLAT_EPS:
                logger.error(f"[26fe] DAILY HALT realized ${realized:.0f} → flatten+idle"); _close_all(ex, info, SUB)
            time.sleep(POLL_S); continue

        # baseline seed (never adopt his existing position)
        if "his_prev" not in state:
            state["his_prev"] = his; _save_state(state)
            logger.info(f"[26fe] baseline his={his} (no adopt); waiting for his next add/open")
            time.sleep(POLL_S); continue
        his_prev = state["his_prev"]

        his_inc  = abs(his) > abs(his_prev) + EPS_HIS and (his_prev == 0 or math.copysign(1, his) == math.copysign(1, his_prev))
        his_flip = his_prev != 0 and abs(his) > EPS_HIS and (his > 0) != (his_prev > 0)
        his_flat = abs(his) < EPS_HIS and abs(his_prev) >= EPS_HIS
        we_flat  = abs(ours) < FLAT_EPS

        if we_flat:
            if his_inc or his_flip:                       # ENTER at our scaled size in his direction
                notional = eq * MARGIN_PCT * LEV
                size = _rnd(notional / px)
                if size > 0:
                    side_buy = his > 0
                    logger.success(f"[26fe] ENTER {'long' if side_buy else 'short'} {size} {COIN} (his={his}, ${notional:,.0f})")
                    try: ex.market_open(COIN, side_buy, size)
                    except Exception as e: logger.error(f"[26fe] open EXC: {e}"); time.sleep(POLL_S); continue
                    time.sleep(1.0); ns, ne = _pos(info, SUB)
                    if abs(ns) > FLAT_EPS: _ensure_stop(ex, info, SUB, ns > 0, abs(ns), ne)
        else:
            if his_flat or his_flip:                      # EXIT (he left / flipped)
                logger.success(f"[26fe] source {'flat' if his_flat else 'flipped'} (his={his}) → close ours ({ours})")
                _close_all(ex, info, SUB)
            elif not _has_stop(info, SUB):                # holding → make sure the dead-man stop rests
                _ensure_stop(ex, info, SUB, ours > 0, abs(ours), entry)

        state["his_prev"] = his; _save_state(state)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
