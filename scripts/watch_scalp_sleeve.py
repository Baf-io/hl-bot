#!/usr/bin/env python3
"""
SCALP COPY SLEEVE — generalized, WS-driven, bounded-adds discipline.

For SCALE-TRADER sources (e.g., 0xbbf82c80) whose PnL comes from many in-trip adds, not
single-leg directional bets. Strict fresh-entry-only would capture only ~25-30% of the
edge. This engine implements the v2.3 SCALP-SOURCE RULEBOOK: 4-megaleg bounded-adds,
hard $-pegged size caps (NOT proportional to source), mirror exit, narrow cooldown.

────────────────────────────────────────────────────────────────────────────────
RULEBOOK (deterministic decision table on every WS event from source)
────────────────────────────────────────────────────────────────────────────────
DETECT WHAT SOURCE DID (his_net_prev → his_net):
  A. FRESH_OPEN   |prev|≈0 ∧ |new|>0
  B. ADD          |new|>|prev| ∧ sign(new)==sign(prev)
  C. TRIM         |new|<|prev| ∧ |new|>0
  D. CLOSE        |prev|>0 ∧ |new|≈0
  E. FLIP         sign(new)≠sign(prev) ∧ both nonzero

ACTIONS:
  A FRESH_OPEN  → if our_net==0 ∧ not in cooldown: OUR_OPEN(LEG_SIZE, his_dir)
  B ADD         → if |our_notional|+LEG_SIZE ≤ TOTAL_BUDGET ∧ same-dir: OUR_ADD(LEG_SIZE)
  C TRIM        → IGNORE (he often re-adds; trimming with him desyncs the wave)
  D CLOSE       → if our_net≠0: OUR_CLOSE_ALL — no cooldown, ready for next fresh open
  E FLIP        → OUR_CLOSE_ALL + OUR_OPEN(LEG_SIZE, new_dir) in same event — no cooldown

SAFETY OVERRIDES (run on every WS mid/fill tick, independent of source):
  S1 our_uPnL_pct < -SL_PCT          → CLOSE_ALL, cooldown COOLDOWN_S
  S2 position_age_s > TIME_STOP_S    → CLOSE_ALL, cooldown COOLDOWN_S
  S3 daily_realized < -DAILY_HALT    → CLOSE_ALL, FULL_HALT until next UTC day
  S4 WS disconnect > 30s             → CLOSE_ALL (flying blind)
  S5 spread > 5bps at open-time      → skip new opens this tick (thin-book guard)

COOLDOWN: only on S1/S2 (involuntary exits). Source-driven exits (D/E) get ZERO cooldown.
────────────────────────────────────────────────────────────────────────────────

Config (env): SCALP_NAME, SCALP_SOURCE, SCALP_SUB, SCALP_COIN, SCALP_TOTAL_BUDGET,
              SCALP_LEG_SIZE, SCALP_LEV, SCALP_SL_PCT, SCALP_TIME_STOP_S,
              SCALP_DAILY_HALT, SCALP_COOLDOWN_S, SCALP_LIVE ('true' for real orders).
"""
import sys, os, json, time, math, threading, datetime as dt
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
import eth_account
import requests
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.websocket_manager import WebsocketManager
from loguru import logger
from config import settings

# ── env config ────────────────────────────────────────────────────────────────
NAME           = os.getenv("SCALP_NAME", "scalp")
SOURCE         = os.getenv("SCALP_SOURCE", "").lower()
SUB            = os.getenv("SCALP_SUB", "").lower()
COIN           = os.getenv("SCALP_COIN", "HYPE").upper()
TOTAL_BUDGET   = float(os.getenv("SCALP_TOTAL_BUDGET", "1000"))   # $ notional cap
LEG_SIZE       = float(os.getenv("SCALP_LEG_SIZE", "250"))        # $ per megaleg
LEV            = int(os.getenv("SCALP_LEV", "5"))
SL_PCT         = float(os.getenv("SCALP_SL_PCT", "1.5"))          # % adverse on weighted avg
TIME_STOP_S    = int(os.getenv("SCALP_TIME_STOP_S", "900"))       # 15min default
DAILY_HALT     = float(os.getenv("SCALP_DAILY_HALT", "30"))       # $ realized loss/day
COOLDOWN_S     = int(os.getenv("SCALP_COOLDOWN_S", "60"))         # post-involuntary-exit only
LIVE           = os.getenv("SCALP_LIVE", "false").lower() == "true"
SPREAD_GUARD_BPS = float(os.getenv("SCALP_SPREAD_GUARD_BPS", "5"))

if not SOURCE or not SUB:
    logger.error("SCALP_SOURCE and SCALP_SUB env required"); sys.exit(1)

API_REST = constants.MAINNET_API_URL
WS_BASE  = "https://api.hyperliquid.xyz"
STATE_FILE = f"/root/hl-bot/data/{NAME}_scalp_state.json"
FLAT_EPS = 1e-9

# ── shared state (lock-guarded) ───────────────────────────────────────────────
state_lock = threading.Lock()
state = {
    # source-side
    "source_net": 0.0,         # signed pos in COIN
    "source_dir": 0,           # 1 / -1 / 0
    # our-side (paper or live, depending on LIVE flag)
    "our_net": 0.0,
    "our_notional": 0.0,       # $ at avg-entry
    "our_avg_entry": 0.0,
    "our_dir": 0,
    "our_opened_t": 0,         # unix s of FIRST leg in current position
    # control
    "cooldown_until": 0,
    "daily_halted": False,
    "daily_realized": 0.0,
    "today_ymd": "",
    # WS health
    "last_msg_t": 0,
    # bookkeeping
    "log": [],                 # list of {coin, dir, entry, exit, ret_pct, opened, closed}
    "cum_ret_pct": 0.0,
    "n_trips": 0,
    "wins": 0,
    "seen_fills_source": [],
    "seen_fills_sub": [],
}
mids = {}                       # coin -> latest mid
spreads_bps = {}                # coin -> latest spread bps
startup_ts_ms = int(time.time() * 1000)
SZDEC = {}                      # coin -> sz decimals

# Thread pool for order placement so WS handler returns instantly
order_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scalp-order")

# ── SDK setup ─────────────────────────────────────────────────────────────────
wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
info_client = Info(API_REST, skip_ws=True)
ex_client   = Exchange(wallet, API_REST, vault_address=SUB)


# ── helpers ───────────────────────────────────────────────────────────────────
def _rest_post(t, **k):
    return requests.post(API_REST, json={"type": t, **k}, timeout=15).json()


def _round_sz(coin, sz):
    return round(sz, SZDEC.get(coin, 2))


def _round_px(p):
    if p <= 0: return p
    d = 5 - int(math.floor(math.log10(abs(p)))) - 1
    return round(p, max(0, d))


def _load_meta():
    """Populate SZDEC from HL meta — required for valid order sizes."""
    meta = info_client.meta()
    for u in meta.get("universe", []):
        SZDEC[u["name"]] = int(u.get("szDecimals", 2))
    logger.info(f"[{NAME}] meta loaded, {COIN} szDecimals={SZDEC.get(COIN, '?')}")


def _save_state():
    with state_lock:
        snap = {k: v for k, v in state.items() if k != "seen_fills_source"}
        snap["seen_fills_source"] = state["seen_fills_source"][-2000:]
        snap["seen_fills_sub"] = state["seen_fills_sub"][-2000:]
    tmp = STATE_FILE + ".tmp"
    json.dump(snap, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE_FILE)


def _load_state():
    if not os.path.exists(STATE_FILE):
        return
    try:
        snap = json.load(open(STATE_FILE))
        with state_lock:
            for k, v in snap.items():
                if k in state:
                    state[k] = v
        logger.info(f"[{NAME}] state resumed: n_trips={state['n_trips']} cum={state['cum_ret_pct']:+.1f}%")
    except Exception as e:
        logger.warning(f"[{NAME}] state load failed: {e}")


def _utc_ymd():
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def _check_daily_rollover():
    today = _utc_ymd()
    with state_lock:
        if state["today_ymd"] != today:
            state["today_ymd"] = today
            state["daily_realized"] = 0.0
            state["daily_halted"] = False
            logger.info(f"[{NAME}] daily rollover → reset halt + realized")


# ── source-side WS handler ────────────────────────────────────────────────────
def _on_source_fills(msg):
    """Receive source's fills, detect A/B/C/D/E, dispatch action."""
    try:
        with state_lock:
            state["last_msg_t"] = int(time.time())
        data = msg.get("data") or {}
        fills = data.get("fills") or []
        if not fills:
            return
        new_fills = []
        with state_lock:
            seen = set(state["seen_fills_source"])
            for f in fills:
                k = f"{f.get('time')}:{f.get('oid')}:{f.get('tid')}"
                if k in seen: continue
                seen.add(k); new_fills.append((k, f))
            state["seen_fills_source"] = list(seen)[-2000:]
        if not new_fills:
            return
        new_fills.sort(key=lambda kf: kf[1].get("time", 0))
        for _, f in new_fills:
            _process_source_fill(f)
    except Exception as e:
        logger.exception(f"[{NAME}] source-fill handler error: {e}")


def _process_source_fill(f):
    coin = f.get("coin"); side = f.get("side")
    if coin != COIN: return        # coin-fenced — never trade outside config
    try:
        sz = float(f.get("sz", 0)); px = float(f.get("px", 0))
        t_ms = int(f.get("time", 0))
    except: return
    if sz <= 0 or px <= 0 or side not in ("B", "A"): return
    # ignore pre-startup history (baked into our baseline)
    if t_ms <= startup_ts_ms: return

    d = sz if side == "B" else -sz
    with state_lock:
        prev = state["source_net"]
        new_net = round(prev + d, 8)
        state["source_net"] = new_net
        state["source_dir"] = 1 if new_net > 0 else (-1 if new_net < 0 else 0)

    # Detect transition class
    fresh_open = abs(prev) <= FLAT_EPS and abs(new_net) > FLAT_EPS
    full_close = abs(prev) > FLAT_EPS and abs(new_net) <= FLAT_EPS
    flipped    = abs(new_net) > FLAT_EPS and abs(prev) > FLAT_EPS and (prev > 0) != (new_net > 0)
    add_event  = abs(new_net) > abs(prev) and not flipped and not fresh_open
    trim_event = abs(new_net) < abs(prev) and not full_close and not flipped

    his_new_dir = 1 if new_net > 0 else (-1 if new_net < 0 else 0)
    his_old_dir = 1 if prev > 0 else (-1 if prev < 0 else 0)

    log_prefix = f"[{NAME}] SRC fill {coin} side={side} sz={sz} px={px:.4g} net {prev:.4g}→{new_net:.4g}"

    if fresh_open:
        logger.info(f"{log_prefix} → FRESH_OPEN dir={his_new_dir}")
        _action_open(his_new_dir, source_px=px)
    elif flipped:
        logger.info(f"{log_prefix} → FLIP {his_old_dir}→{his_new_dir}")
        _action_close_then_open(his_new_dir, source_px=px)
    elif full_close:
        logger.info(f"{log_prefix} → CLOSE")
        _action_close_all(reason="source_close")
    elif add_event:
        logger.info(f"{log_prefix} → ADD")
        _action_add(his_new_dir, source_px=px)
    elif trim_event:
        logger.info(f"{log_prefix} → TRIM (ignored — we never trim)")


# ── sub-side WS handler (our own fills, for state reconciliation) ─────────────
def _on_sub_fills(msg):
    try:
        data = msg.get("data") or {}
        fills = data.get("fills") or []
        if not fills: return
        with state_lock:
            seen = set(state["seen_fills_sub"])
            new = []
            for f in fills:
                k = f"{f.get('time')}:{f.get('oid')}:{f.get('tid')}"
                if k in seen: continue
                seen.add(k); new.append(f)
            state["seen_fills_sub"] = list(seen)[-2000:]
        if not new: return
        # Don't process pre-startup; baseline our state from REST at start
        for f in new:
            t_ms = int(f.get("time", 0))
            if t_ms <= startup_ts_ms: continue
            if f.get("coin") != COIN: continue
            # update realized pnl
            with state_lock:
                state["daily_realized"] += float(f.get("closedPnl", 0))
    except Exception as e:
        logger.exception(f"[{NAME}] sub-fill handler error: {e}")


def _on_mids(msg):
    try:
        data = msg.get("data") or {}
        for c, p in (data.get("mids") or {}).items():
            try: mids[c] = float(p)
            except: continue
        with state_lock:
            state["last_msg_t"] = int(time.time())
    except Exception as e:
        logger.exception(f"[{NAME}] mids handler error: {e}")


# ── action dispatchers (queue to thread pool, return immediately) ─────────────
def _in_cooldown():
    with state_lock:
        return state["cooldown_until"] > time.time()


def _action_open(his_dir, source_px):
    if _in_cooldown():
        logger.info(f"[{NAME}] OPEN skipped — in cooldown ({state['cooldown_until']-int(time.time())}s left)")
        return
    with state_lock:
        if state["daily_halted"]:
            logger.info(f"[{NAME}] OPEN skipped — daily halt active"); return
        if abs(state["our_net"]) > FLAT_EPS:
            logger.info(f"[{NAME}] OPEN skipped — already in position"); return
    order_pool.submit(_place_leg, dir_=his_dir, dollars=LEG_SIZE, kind="OPEN", source_px=source_px)


def _action_add(his_dir, source_px):
    with state_lock:
        if abs(state["our_net"]) < FLAT_EPS:
            logger.info(f"[{NAME}] ADD skipped — we're flat, waiting for next fresh open"); return
        if state["our_dir"] != his_dir:
            logger.warning(f"[{NAME}] ADD skipped — direction mismatch our={state['our_dir']} his={his_dir}"); return
        if state["our_notional"] + LEG_SIZE > TOTAL_BUDGET + 0.5:
            logger.info(f"[{NAME}] ADD skipped — would exceed budget (${state['our_notional']:.0f}+${LEG_SIZE:.0f}>${TOTAL_BUDGET:.0f})"); return
    order_pool.submit(_place_leg, dir_=his_dir, dollars=LEG_SIZE, kind="ADD", source_px=source_px)


def _action_close_all(reason):
    with state_lock:
        if abs(state["our_net"]) < FLAT_EPS:
            return
    order_pool.submit(_place_close, reason=reason)


def _action_close_then_open(new_dir, source_px):
    """FLIP handling — close + open in same dispatch."""
    order_pool.submit(_place_close_then_open, new_dir=new_dir, source_px=source_px)


# ── order placement (runs in thread pool) ─────────────────────────────────────
def _place_leg(dir_, dollars, kind, source_px):
    """Open or add a leg. dir_=1 long, dir_=-1 short. dollars=$ notional to add."""
    px = mids.get(COIN) or source_px
    if px <= 0:
        logger.warning(f"[{NAME}] {kind} aborted — no mid for {COIN}"); return
    # Spread guard
    sp = spreads_bps.get(COIN, 0)
    if sp > SPREAD_GUARD_BPS:
        logger.warning(f"[{NAME}] {kind} aborted — spread {sp:.1f}bps > {SPREAD_GUARD_BPS}bps"); return

    sz = _round_sz(COIN, dollars / px)
    if sz <= 0:
        logger.warning(f"[{NAME}] {kind} aborted — sz rounded to 0 (px={px}, dollars={dollars})"); return
    is_buy = dir_ > 0

    log_line = f"[{NAME}] {'LIVE ' if LIVE else 'PAPER'}{kind} {COIN} {'BUY' if is_buy else 'SELL'} sz={sz} (~${dollars:.0f} @ ${px:.4g})"
    logger.info(log_line)

    fill_px = px
    if LIVE:
        try:
            r = ex_client.market_open(name=COIN, is_buy=is_buy, sz=sz, slippage=0.005)
            if r.get("status") == "ok":
                statuses = r.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    fill_px = float(statuses[0]["filled"]["avgPx"])
                    logger.success(f"[{NAME}] {kind} FILLED @ {fill_px:.4g}")
                else:
                    logger.warning(f"[{NAME}] {kind} ambiguous response: {statuses}"); return
            else:
                logger.error(f"[{NAME}] {kind} REJECTED: {r}"); return
        except Exception as e:
            logger.exception(f"[{NAME}] {kind} exception: {e}"); return

    # Update internal state (works for both live and paper)
    with state_lock:
        prev_net = state["our_net"]
        leg_signed = sz if is_buy else -sz
        new_net = prev_net + leg_signed
        # Update weighted avg entry
        if abs(prev_net) < FLAT_EPS:
            state["our_avg_entry"] = fill_px
            state["our_opened_t"] = int(time.time())
        else:
            state["our_avg_entry"] = (
                (state["our_avg_entry"] * abs(prev_net) + fill_px * abs(leg_signed))
                / abs(new_net)
            ) if abs(new_net) > FLAT_EPS else 0
        state["our_net"] = new_net
        state["our_dir"] = 1 if new_net > 0 else (-1 if new_net < 0 else 0)
        state["our_notional"] = abs(new_net) * state["our_avg_entry"]
    _save_state()


def _place_close(reason):
    with state_lock:
        sz_signed = state["our_net"]
        opened = state["our_opened_t"]; avg = state["our_avg_entry"]
    if abs(sz_signed) < FLAT_EPS:
        return
    px = mids.get(COIN) or avg
    is_buy = sz_signed < 0   # close short → buy
    sz = _round_sz(COIN, abs(sz_signed))

    log_line = f"[{NAME}] {'LIVE ' if LIVE else 'PAPER'}CLOSE {COIN} {'BUY' if is_buy else 'SELL'} sz={sz} reason={reason} @ ~${px:.4g}"
    logger.info(log_line)

    fill_px = px
    if LIVE:
        try:
            r = ex_client.market_close(coin=COIN)
            if r.get("status") == "ok":
                statuses = r.get("response", {}).get("data", {}).get("statuses", [])
                if statuses and "filled" in statuses[0]:
                    fill_px = float(statuses[0]["filled"]["avgPx"])
                    logger.success(f"[{NAME}] CLOSE FILLED @ {fill_px:.4g}")
            else:
                logger.error(f"[{NAME}] CLOSE REJECTED: {r}"); return
        except Exception as e:
            logger.exception(f"[{NAME}] CLOSE exception: {e}"); return

    # Compute trip P&L%, log, reset position state
    with state_lock:
        ret_pct = (1 if sz_signed > 0 else -1) * (fill_px - avg) / avg * 100 if avg > 0 else 0
        state["cum_ret_pct"] += ret_pct
        state["n_trips"] += 1
        if ret_pct > 0: state["wins"] += 1
        state["log"].append({
            "coin": COIN, "dir": "L" if sz_signed > 0 else "S",
            "avg_entry": avg, "exit": fill_px, "ret_pct": round(ret_pct, 3),
            "opened": opened, "closed": int(time.time()), "reason": reason,
        })
        state["log"] = state["log"][-200:]
        state["our_net"] = 0.0; state["our_avg_entry"] = 0.0
        state["our_dir"] = 0; state["our_notional"] = 0.0; state["our_opened_t"] = 0
        if reason in ("sl_hit", "time_stop"):
            state["cooldown_until"] = int(time.time()) + COOLDOWN_S
            logger.warning(f"[{NAME}] cooldown {COOLDOWN_S}s after involuntary exit ({reason})")
    logger.success(f"[{NAME}] TRIP closed: ret {ret_pct:+.2f}% (cum {state['cum_ret_pct']:+.1f}% n={state['n_trips']})")
    _save_state()


def _place_close_then_open(new_dir, source_px):
    _place_close(reason="source_flip")
    if not _in_cooldown():
        _place_leg(dir_=new_dir, dollars=LEG_SIZE, kind="OPEN_AFTER_FLIP", source_px=source_px)


# ── safety loop (S1-S5) ───────────────────────────────────────────────────────
def _safety_loop():
    while True:
        time.sleep(1)
        try:
            _check_daily_rollover()
            now = int(time.time())
            with state_lock:
                our_net = state["our_net"]; avg = state["our_avg_entry"]
                opened = state["our_opened_t"]; halted = state["daily_halted"]
                daily = state["daily_realized"]; last_msg = state["last_msg_t"]
            px = mids.get(COIN, 0)

            # S5: WS health
            if last_msg and now - last_msg > 30 and abs(our_net) > FLAT_EPS:
                logger.error(f"[{NAME}] S5 WS silence {now-last_msg}s — closing all")
                _action_close_all(reason="ws_silence"); continue

            # S3: daily halt
            if daily < -DAILY_HALT and not halted:
                logger.error(f"[{NAME}] S3 daily realized ${daily:.2f} < -${DAILY_HALT} — HALT")
                with state_lock: state["daily_halted"] = True
                _action_close_all(reason="daily_halt"); continue

            if abs(our_net) < FLAT_EPS or px <= 0 or avg <= 0:
                continue

            # S1: SL on weighted avg
            upnl_pct = (1 if our_net > 0 else -1) * (px - avg) / avg * 100
            if upnl_pct < -SL_PCT:
                logger.warning(f"[{NAME}] S1 SL hit: uPnL {upnl_pct:+.2f}% < -{SL_PCT}%")
                _action_close_all(reason="sl_hit"); continue

            # S2: time stop
            if opened and now - opened > TIME_STOP_S:
                logger.warning(f"[{NAME}] S2 time-stop: position age {now-opened}s > {TIME_STOP_S}s")
                _action_close_all(reason="time_stop"); continue
        except Exception as e:
            logger.exception(f"[{NAME}] safety loop error: {e}")


def _scoreboard_loop():
    while True:
        time.sleep(900)   # 15min
        try:
            with state_lock:
                n = state["n_trips"]; cum = state["cum_ret_pct"]
                wr = (state["wins"]/n*100) if n else 0
                live_flag = "LIVE" if LIVE else "PAPER"
            logger.info(f"[{NAME}] [{live_flag}] scoreboard: trips={n} WR={wr:.0f}% cumRet={cum:+.2f}% "
                        f"daily=${state['daily_realized']:.2f} halted={state['daily_halted']}")
        except Exception as e:
            logger.warning(f"[{NAME}] scoreboard error: {e}")


# ── startup ───────────────────────────────────────────────────────────────────
def _seed_baseline():
    """Read source + sub positions ONCE via REST to seed our state, then never poll."""
    try:
        src_state = info_client.user_state(SOURCE)
        for ap in src_state.get("assetPositions", []):
            if ap["position"].get("coin") == COIN:
                state["source_net"] = float(ap["position"].get("szi", 0))
                state["source_dir"] = 1 if state["source_net"] > 0 else (-1 if state["source_net"] < 0 else 0)
                logger.info(f"[{NAME}] source baseline: {state['source_net']:+.4f} {COIN}")
                break

        sub_state = info_client.user_state(SUB)
        for ap in sub_state.get("assetPositions", []):
            if ap["position"].get("coin") == COIN:
                state["our_net"] = float(ap["position"].get("szi", 0))
                state["our_avg_entry"] = float(ap["position"].get("entryPx", 0))
                state["our_dir"] = 1 if state["our_net"] > 0 else (-1 if state["our_net"] < 0 else 0)
                state["our_notional"] = abs(state["our_net"]) * state["our_avg_entry"]
                if state["our_net"] != 0 and state["our_opened_t"] == 0:
                    state["our_opened_t"] = int(time.time())
                logger.info(f"[{NAME}] our sub baseline: {state['our_net']:+.4f} {COIN} @ ${state['our_avg_entry']:.4g}")
                break
        eq = float(sub_state.get("marginSummary", {}).get("accountValue", 0))
        logger.info(f"[{NAME}] sub equity: ${eq:.2f}")
        if LIVE and eq < TOTAL_BUDGET / LEV:
            logger.error(f"[{NAME}] sub equity ${eq:.2f} insufficient for budget ${TOTAL_BUDGET}@{LEV}x — fund the sub")
    except Exception as e:
        logger.exception(f"[{NAME}] baseline read failed: {e}")


def _set_leverage():
    """Set isolated margin + leverage on the sub for this coin."""
    if not LIVE:
        logger.info(f"[{NAME}] PAPER mode — skipping leverage set"); return
    try:
        r = ex_client.update_leverage(LEV, COIN, is_cross=False)
        logger.info(f"[{NAME}] leverage set: {COIN} {LEV}x iso → {r.get('status')}")
    except Exception as e:
        logger.warning(f"[{NAME}] leverage set failed (continuing): {e}")


def main():
    logger.info(f"[{NAME}] start | source={SOURCE[:10]}… sub={SUB[:10]}… coin={COIN} "
                f"budget=${TOTAL_BUDGET} leg=${LEG_SIZE} lev={LEV}x sl={SL_PCT}% "
                f"time_stop={TIME_STOP_S}s daily_halt=${DAILY_HALT} cooldown={COOLDOWN_S}s "
                f"LIVE={LIVE}")
    _check_daily_rollover()
    _load_state()
    _load_meta()
    _seed_baseline()
    _set_leverage()

    ws = WebsocketManager(WS_BASE)
    ws.start()
    ws.subscribe({"type": "allMids"}, _on_mids)
    ws.subscribe({"type": "userFills", "user": SOURCE}, _on_source_fills)
    ws.subscribe({"type": "userFills", "user": SUB}, _on_sub_fills)
    logger.info(f"[{NAME}] WS subscribed: allMids + userFills:source + userFills:sub")

    threading.Thread(target=_safety_loop, daemon=True).start()
    threading.Thread(target=_scoreboard_loop, daemon=True).start()

    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
