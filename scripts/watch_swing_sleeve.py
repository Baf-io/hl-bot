#!/usr/bin/env python3
"""
SWING COPY SLEEVE — generalized, runs on a dedicated walled-off HL SUBACCOUNT.

Mirrors ONE durable multi-day SWING trader's DIRECTION on a configured coin set, in isolated
margin, fresh-entry-only. Built for copy-lag-tolerant swing sources (multi-day holds) — NOT for
scalpers (the 45s/poll lag would eat an intraday edge). One script, parametrized by env so each
seat is its own systemd service.

Per coin, each poll:
  ENTRY  — on the source's NEXT fresh directional move (size increase while WE are flat, or a flip).
           We baseline-seed his CURRENT position on first tick and do NOT adopt it (no stale entry
           at a worse price — the copy-lag leak). State persisted per sleeve.
  EXIT   — when the source goes FLAT or FLIPS on that coin. We ride; we ignore his trims.
  SIZE   — target notional = SUB_equity × MARGIN_PCT × LEV, in his direction (scales with the sub).
  SL     — dead-man reduce-only stop at STOP_PCT from our entry (eyes-on-book confirmed). Backstop;
           normally we exit on his flip/flat within one poll.
  Guards — daily realized kill-switch (HALT_USD, across all coins) → flatten+idle to next UTC day;
           clean-read-only (a failed read skips the tick); isolated margin walls max loss to the sub.

Config (env): SLEEVE_NAME, SLEEVE_SOURCE, SLEEVE_SUB, SLEEVE_COINS(csv), SLEEVE_MARGIN_PCT,
              SLEEVE_LEV, SLEEVE_STOP_PCT, SLEEVE_HALT_USD, SLEEVE_POLL_S.
Manage: systemctl {status,stop} hl-<name>-sleeve
"""
import sys, os, json, time, math, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger
from config import settings

NAME       = os.getenv("SLEEVE_NAME", "swing")
SOURCE     = os.getenv("SLEEVE_SOURCE", "").lower()
SUB        = os.getenv("SLEEVE_SUB", "").lower()
COINS      = [c.strip().upper() for c in os.getenv("SLEEVE_COINS", "BTC").split(",") if c.strip()]
MARGIN_PCT = float(os.getenv("SLEEVE_MARGIN_PCT", "0.40"))   # per-coin margin frac of sub equity
LEV        = int(os.getenv("SLEEVE_LEV", "3"))
MAX_CONCURRENT = int(os.getenv("SLEEVE_MAX_CONCURRENT", "0"))  # 0=unlimited; 1=one-slot (concentrate)
STOP_PCT   = float(os.getenv("SLEEVE_STOP_PCT", "0.08"))
HALT_USD   = float(os.getenv("SLEEVE_HALT_USD", "40"))
POLL_S     = int(os.getenv("SLEEVE_POLL_S", "20"))
EPS_HIS    = float(os.getenv("SLEEVE_EPS_HIS", "0.0"))       # set per-coin from szDecimals below
FLAT_EPS   = 1e-9
STATE      = f"/root/hl-bot/data/{NAME}_sleeve_state.json"

_SZDEC = {}   # coin -> szDecimals (set at startup from meta)

def _round_px(p):
    if p <= 0: return p
    digits = 5 - int(math.floor(math.log10(abs(p)))) - 1   # 5 significant figures
    return round(p, max(0, digits))

def _round_sz(coin, sz):
    return round(sz, _SZDEC.get(coin, 4))

def _eps(coin):
    return max(10 ** (-_SZDEC.get(coin, 4)), 1e-9)          # 1 unit at the coin's size precision

def _pos(info, addr, coin):
    """(signed_size, avg_entry). Raises on API failure (clean-read). ONE user_state call."""
    return _pos_from(info.user_state(addr), coin)

def _pos_from(state, coin):
    """Parse (signed_size, avg_entry) for coin out of an already-fetched user_state dict."""
    for ap in state.get("assetPositions", []):
        p = ap["position"]
        if p.get("coin") == coin:
            return float(p.get("szi", 0)), float(p.get("entryPx") or 0)
    return 0.0, 0.0

def _equity(info, addr):
    return float(info.user_state(addr)["marginSummary"]["accountValue"])

def _today_realized(info, addr):
    midnight = dt.datetime.now(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    ms = int(midnight.timestamp() * 1000); pnl = 0.0
    for f in info.user_fills(addr):
        if f.get("coin") in COINS and f.get("time", 0) >= ms:
            pnl += float(f.get("closedPnl", 0))
    return pnl

def _cancel(ex, info, addr, coin):
    try:
        for o in info.open_orders(addr):
            if o.get("coin") == coin: ex.cancel(coin, o["oid"])
    except Exception as e: logger.warning(f"[{NAME}] cancel skip {coin}: {e}")

def _has_stop(info, addr, coin):
    try: return any(o.get("coin") == coin for o in info.open_orders(addr))
    except Exception: return True   # fail-safe: assume present, don't spam orders

def _ensure_stop(ex, info, addr, coin, is_long, size, entry):
    _cancel(ex, info, addr, coin)
    stop_px = _round_px(entry * (1 - STOP_PCT)) if is_long else _round_px(entry * (1 + STOP_PCT))
    try:
        r = ex.order(coin, (not is_long), _round_sz(coin, size), stop_px,
                     {"trigger": {"triggerPx": stop_px, "isMarket": True, "tpsl": "sl"}}, reduce_only=True)
        st = r["response"]["data"]["statuses"][0]; oid = (st.get("resting") or {}).get("oid")
        if "error" in st or oid is None:
            logger.error(f"[{NAME}] {coin} stop fail: {st}"); return False
        time.sleep(1.0)
        resting = any(o.get("oid") == oid for o in info.open_orders(addr))
        logger.info(f"[{NAME}] {coin} stop {'LONG' if is_long else 'SHORT'} {_round_sz(coin,size)} @ {stop_px} {'OK' if resting else 'NOT-RESTING'}")
        return resting
    except Exception as e: logger.error(f"[{NAME}] {coin} stop EXC: {e}"); return False

def _close(ex, info, addr, coin):
    for _ in range(4):
        try:
            r = ex.market_close(coin); st = r["response"]["data"]["statuses"][0]
            if "filled" in st:
                logger.success(f"[{NAME}] CLOSED {coin} @ ${st['filled']['avgPx']}"); _cancel(ex, info, addr, coin); return True
        except Exception as e: logger.error(f"[{NAME}] {coin} close EXC: {e}")
        time.sleep(2)
    return False

def _load():
    try: return json.load(open(STATE))
    except Exception: return {}
def _save(d): json.dump(d, open(STATE, "w"))


def main():
    if not SOURCE or not SUB:
        logger.error(f"[{NAME}] SLEEVE_SOURCE/SLEEVE_SUB unset"); sys.exit(1)
    wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
    ex = Exchange(wallet, constants.MAINNET_API_URL, vault_address=SUB)   # trade ON the SUB (orders route to vault_address, NOT account_address — see CLAUDE.md rule #10)
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    # per-coin size precision from meta
    for a in info.meta()["universe"]:
        _SZDEC[a["name"]] = a.get("szDecimals", 4)
    for c in COINS:
        try: ex.update_leverage(LEV, c, False)   # isolated
        except Exception as e: logger.warning(f"[{NAME}] set lev {c}: {e}")
    logger.info(f"[{NAME}] start | src={SOURCE[:10]}… sub={SUB[:10]}… coins={COINS} | "
                f"margin {MARGIN_PCT:.0%}×eq @ {LEV}x | stop {STOP_PCT:.0%} | halt ${HALT_USD:.0f}/day "
                f"| fresh-entry-only, exit-on-his-flip/flat")
    state = _load()

    while True:
        try:
            src_state = info.user_state(SOURCE)          # ONE call → all source coins
            sub_state = info.user_state(SUB)             # ONE call → all our coins + equity
            mids      = info.all_mids()                  # ONE call
            realized  = _today_realized(info, SUB)       # ONE call (fills)
            eq        = float(sub_state["marginSummary"]["accountValue"])
            snap      = {c: (_pos_from(src_state, c)[0], _pos_from(sub_state, c)) for c in COINS}
        except Exception as e:
            logger.warning(f"[{NAME}] read failed (skip): {e}"); time.sleep(POLL_S); continue

        # daily kill-switch — flatten everything, idle to next UTC day
        if realized <= -HALT_USD:
            for c in COINS:
                if abs(snap[c][1][0]) > FLAT_EPS:
                    logger.error(f"[{NAME}] DAILY HALT realized ${realized:.0f} → flatten {c}"); _close(ex, info, SUB, c)
            time.sleep(POLL_S); continue

        opened_now = 0                                    # positions opened within THIS poll (snap is start-of-poll)
        for coin in COINS:
            his = snap[coin][0]; ours, entry = snap[coin][1]
            try: px = float(mids[coin])
            except Exception: continue
            eps = max(_eps(coin), EPS_HIS)
            key = f"his_prev_{coin}"

            if key not in state:                              # baseline-seed (never adopt)
                state[key] = his; _save(state)
                logger.info(f"[{NAME}] baseline {coin} his={his} (no adopt); awaiting his next move")
                continue
            prev = state[key]

            his_inc  = abs(his) > abs(prev) + eps and (prev == 0 or math.copysign(1, his) == math.copysign(1, prev))
            his_flip = prev != 0 and abs(his) > eps and (his > 0) != (prev > 0)
            his_flat = abs(his) < eps and abs(prev) >= eps
            we_flat  = abs(ours) < eps

            if we_flat:
                if his_inc or his_flip:                       # ENTER in his direction at our scaled size
                    held_count = sum(1 for cc in COINS if abs(snap[cc][1][0]) > FLAT_EPS) + opened_now
                    if MAX_CONCURRENT and held_count >= MAX_CONCURRENT:
                        logger.info(f"[{NAME}] skip {coin} fresh-open (one-slot full: {held_count}/{MAX_CONCURRENT} held)")
                        state[key] = his; continue            # concentrate: ignore extra fresh opens while slot filled
                    notional = eq * MARGIN_PCT * LEV
                    size = _round_sz(coin, notional / px)
                    if size > 0:
                        side_buy = his > 0
                        logger.success(f"[{NAME}] ENTER {coin} {'long' if side_buy else 'short'} {size} (his={his}, ${notional:,.0f})")
                        try: ex.market_open(coin, side_buy, size)
                        except Exception as e: logger.error(f"[{NAME}] {coin} open EXC: {e}"); continue
                        time.sleep(1.0); ns, ne = _pos(info, SUB, coin)
                        if abs(ns) > FLAT_EPS: _ensure_stop(ex, info, SUB, coin, ns > 0, abs(ns), ne); opened_now += 1
            else:
                if his_flat or his_flip:                      # EXIT (he left / flipped)
                    logger.success(f"[{NAME}] source {'flat' if his_flat else 'flipped'} on {coin} (his={his}) → close ours ({ours})")
                    _close(ex, info, SUB, coin)
                elif not _has_stop(info, SUB, coin):          # holding → keep the dead-man stop resting
                    _ensure_stop(ex, info, SUB, coin, ours > 0, abs(ours), entry)

            state[key] = his
        _save(state)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
