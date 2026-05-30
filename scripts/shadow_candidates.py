#!/usr/bin/env python3
"""
Shadow-validation of leaderboard scan candidates — PAPER ONLY via WebSocket push.

WS upgrade 2026-05-30: replaces the 300s REST poll with HL WebSocket subscriptions:
- one `allMids` subscription → ms-latency mark prices in memory
- one `userFills:<addr>` per candidate → fills pushed within ~10-50ms of on-chain confirm

This was sized to diagnose why 36f2_patient ran -2% cumRet on 70% WR under the
polled version: the 5min poll captured opens/closes ~150-300s after they happened
so the recorded entry/exit prices were drifted, eating his real edge. With WS push
we record at the mid AT THE MOMENT his fill confirms, so the paper PnL should
match what a 0-latency sleeve would have realized.

Discipline (unchanged from polled version):
- FRESH-ENTRY ONLY: positions held at startup are seeded as BASELINE and NOT credited.
- Equal-weight %/round-trip (leverage-agnostic), $100 nominal stake for display.
- Idempotent dedup via (time, oid, tid) per-fill — WS reconnect replays are safe.
- State at data/shadow_scan_state.json; schema is back-compat (adds `nets`,
  `seen_fills`, `startup_ts_ms` fields, preserves prior `log`/`cum_ret`/`n`/`wins`).

Runs as hl-shadow-scan.service. Read-only API; no keys needed.
"""
import sys, time, json, os, threading
sys.path.insert(0, "/root/hl-bot")
sys.path.insert(0, "/root/hl-bot/src")

import requests
from loguru import logger
from hyperliquid.websocket_manager import WebsocketManager

API_REST = "https://api.hyperliquid.xyz/info"
WS_BASE  = "https://api.hyperliquid.xyz"
STAKE    = 100.0
STATE    = "/root/hl-bot/data/shadow_scan_state.json"
SEEN_CAP = 2000               # per-candidate dedup ring (bounds disk growth)
SCOREBOARD_S = 1800           # 30 min

# Roster — see comments above for v2.1-batch additions.
CANDS = {
    "36f2_patient":         "0x36f26e2e5bed062968c17fc770863fd740713205",
    "da830d2d_HYPEmajors":  "0xda830d2d83a57cea255bcfd0cf89c3e94abde0fd",
    "c4ea203e_liquidmajor": "0xc4ea203e2eb096c4d949b9a64a5d49c0a8a1d8b3",
    "e6deb805_BTCSOLswing": "0xe6deb8055207cf89fd3111f581708705a1bd0c4f",
    "74dd1b67_ETHBNB":      "0x74dd1b672c1efbdd2559aa39e31cb56792a151bd",
    "8a820d3b_SOLswing":    "0x8a820d3b050bafc0a1f3156706f28038aa292dce",
    "186a0ede_ETHsharp":    "0x186a0ede279bb1e46fc383d990635d32dda655f2",
    "0526345b_HFT_lagtest": "0x0526345bf8e09eb32256008c2844c8949ee3bb9a",
    # 2026-05-30: WS-mode + v2.3 tier model unlocked sub-swing copying. Backfilled
    # 4 names that were Tier-1 in COPYABLE_DB or v2.1 batch but not yet shadowed:
    #   - 5c42b895 score 63.5 CLEAN HYPE intraday (DB Tier 1, payoff 1.68, $172k real)
    #   - 422c3cf3 score 50.8 CLEAN HYPE/ETH/XRP intraday (DB Tier 1, 170 trips/101d)
    #   - 5847fd14 score 46.2 CLEAN BTC swing/scalp (DB Tier 1, payoff 2.56, tiny trades)
    #   - 06e0602c score 42.5 CLEAN ETH grid (v2.1, 357 trips on 21,614 adds — cohort)
    "5c42b895_HYPEdaily":   "0x5c42b895c1a7f42fe0f72ed2ba1fe442376fb61d",
    "422c3cf3_intraday":    "0x422c3cf3457a85e9f369242340c023a72a4d6374",
    "5847fd14_BTCsharp":    "0x5847fd1490cbfb598116edd0e2901689aed65983",
    "06e0602c_ETHgrid":     "0x06e0602c9158ee8478365c74606346d90d06df67",
    # 2026-05-30 PM: v2.2 batch (44 survivors, all live-cscore-verified). Top 6 fresh:
    #   - bbf82c80 cscore 80.9 ultra-scalp HYPE — half of the cohort pair (n=24 co-opens
    #              w/ 9f3e77cb, 100% same-dir) — single biggest cohort signal in batch
    #   - 9f3e77cb cscore 73.7 intraday HYPE — other half of the cohort pair
    #   - 5d9d19a3 cscore 68.7 swing BTC 47h holds, $121k realized — prime solo pick
    #   - b19e0376 cscore 51.6 swing BTC 13h holds, **$546k realized** (largest in batch)
    #   - f2704e08 cscore 51.8 swing BTC 775 trips, payoff 2.04 — asymmetric trader
    #   - 1169a721 cscore 52.9 swing @142 (HL spot niche pair) — n=136 trips
    "bbf82c80_HYPEultra":   "0xbbf82c801652198aaac1d06dd974b4d607edf0c9",
    "9f3e77cb_HYPEintra":   "0x9f3e77cb89df964003053aa5b438e5697c77f4f9",
    "5d9d19a3_BTCswing":    "0x5d9d19a3e5005225f13780fa10c198f87816670d",
    "b19e0376_BTCwhale":    "0xb19e03768f72cb1b47fcd68f7fb99460b1e789b9",
    "f2704e08_BTCasym":     "0xf2704e08a4d989f76171c9389665e77c870345a7",
    "1169a721_142niche":    "0x1169a721e2e2052ac3768ca3a6e120b745247097",
    # 2026-05-30 PM: v2.3-patched batch (43 survivors). +5 fresh CLEAN solo-eligible
    # from the post-bugfix data. All live-cscore-verified exact-match. The first two
    # form a SECOND cohort pair (45f3 ↔ 38f2, partner n=2 each direction):
    "45f36597_BTCswing":    "0x45f3659785468c122609951014bc66a7c472f640",
    "38f2bc66_ETHintra":    "0x38f2bc667b46467e7c6d10348695f9ba804bcf02",
    "c2c060e3_BTCswing":    "0xc2c060e3309461ec7c4291b1aac6c89aa228727e",
    "ae332e9d_ETHswing":    "0xae332e9d33af93c0ee8362f61a8a5aa378bfd508",
    "66d1f704_BTCdurable":  "0x66d1f704749ced8c60df03cd0ccfce05c5d87fae",
    # 2026-05-30 PM cont'd: +2 from final v2.2 sweep (only unshadowed CLEAN with n≥30):
    #   - d205b029 cscore 34.5 BTC swing n=40 WR 78% $98k — high-WR low-sample
    #   - 901a6642 cscore 38.8 HYPE swing n=73 WR 38% — low-WR control / negative-edge probe
    "d205b029_BTChighwr":   "0xd205b02969b04af5a87112ca24d27573aea2ae46",
    "901a6642_HYPElowwr":   "0x901a664271fcda5065027edcd339ca50cd9d77f3",
}
FLAT_EPS = 1e-9

# Shared in-memory state. `state_lock` guards file writes + cross-thread reads.
state_lock = threading.Lock()
state: dict = {}
mids: dict = {}                # coin -> latest mid (float)
startup_ts_ms = int(time.time() * 1000)


def _rest_post(t, **k):
    return requests.post(API_REST, json={"type": t, **k}, timeout=15).json()


def _positions_rest(addr):
    """Initial baseline-positions via REST (one call per candidate at startup)."""
    cs = _rest_post("clearinghouseState", user=addr)
    out = {}
    for ap in cs.get("assetPositions", []):
        p = ap["position"]; szi = float(p.get("szi", 0))
        if abs(szi) > FLAT_EPS:
            out[p["coin"]] = szi
    return out


def _load_state():
    if os.path.exists(STATE):
        try: return json.load(open(STATE))
        except Exception: pass
    return {}


def _save_state_locked():
    """Atomic save; caller must hold state_lock."""
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def _seed_or_upgrade():
    """For each candidate: if new → REST-seed; if existing → re-baseline from REST so
    positions opened/closed during downtime are reflected, and any stale in-flight
    paper opens are dropped (we can't honestly score what we didn't witness). Keeps
    historical `cum_ret`/`wins`/`n`/`log` intact."""
    for label, addr in CANDS.items():
        try:
            held = _positions_rest(addr)
        except Exception as e:
            logger.warning(f"[shadow-ws] {label} baseline REST failed: {e}")
            held = {}
        if label not in state:
            state[label] = {
                "addr": addr,
                "nets": {c: sz for c, sz in held.items()},
                "open": {},
                "baseline": list(held.keys()),
                "cum_ret": 0.0, "wins": 0, "n": 0,
                "log": [],
                "seen_fills": [],
                "startup_ts_ms": startup_ts_ms,
            }
            logger.info(f"[shadow-ws] seed {label} baseline: {list(held.keys()) or 'flat'}")
        else:
            s = state[label]
            # back-compat: fill in fields missing from the old polled-version state
            s.setdefault("nets", {c: held.get(c, 0.0) for c in s.get("baseline", [])})
            s.setdefault("seen_fills", [])
            # re-baseline on every restart: nets ← REST truth, drop in-flight opens
            dropped = list(s.get("open", {}).keys())
            s["nets"] = {c: sz for c, sz in held.items()}
            s["baseline"] = list(held.keys())
            s["open"] = {}
            s["startup_ts_ms"] = startup_ts_ms
            if dropped:
                logger.warning(f"[shadow-ws] {label} restart: dropped {len(dropped)} "
                               f"in-flight paper open(s) {dropped} (can't score what we missed)")
            logger.info(f"[shadow-ws] resume {label}: baseline={list(held.keys()) or 'flat'} "
                        f"history n={s['n']} cum={s['cum_ret']:.1f}%")


def _make_fills_handler(label):
    """Closure: handles userFills WS pushes for one candidate."""
    def handler(msg):
        try:
            data = msg.get("data") or {}
            fills = data.get("fills") or []
            if not fills:
                return
            with state_lock:
                _process_fills(label, fills)
        except Exception as e:
            logger.exception(f"[shadow-ws] {label} fill handler error: {e}")
    return handler


def _process_fills(label, fills):
    """Caller must hold state_lock. Processes a batch of fills for one candidate."""
    s = state[label]
    seen = set(s.get("seen_fills") or [])
    new = []
    for f in fills:
        key = f"{f.get('time')}:{f.get('oid')}:{f.get('tid')}"
        if key in seen:
            continue
        seen.add(key)
        new.append((key, f))
    if not new:
        return

    # process in chronological order so net-position transitions resolve correctly
    new.sort(key=lambda kf: kf[1].get("time", 0))

    persist = False
    for key, f in new:
        coin = f.get("coin"); side = f.get("side")
        try:
            sz = float(f.get("sz", 0)); px = float(f.get("px", 0))
        except Exception:
            continue
        t_ms = int(f.get("time", 0))
        if not coin or sz <= 0 or px <= 0 or side not in ("B", "A"):
            continue

        # Pre-startup fills are part of the REST baseline we already loaded —
        # mark them seen so reconnects don't re-process, but DON'T update nets
        # (would double-count the baseline state).
        if t_ms <= s.get("startup_ts_ms", startup_ts_ms):
            continue

        d = sz if side == "B" else -sz
        prev = float(s["nets"].get(coin, 0.0))
        new_net = round(prev + d, 8)

        # Detect transitions BEFORE updating nets
        flipped = (abs(new_net) > FLAT_EPS and abs(prev) > FLAT_EPS
                   and (prev > 0) != (new_net > 0))
        went_flat = (abs(prev) > FLAT_EPS and abs(new_net) <= FLAT_EPS)
        opened = (abs(prev) <= FLAT_EPS and abs(new_net) > FLAT_EPS) or flipped

        # CLOSE / FLIP: had a paper-open on this coin
        if coin in s["open"] and (went_flat or flipped):
            o = s["open"][coin]
            mid = float(mids.get(coin, 0)) or px   # fall back to fill px if mid missing
            ret = (1 if o["dir"] > 0 else -1) * (mid - o["entry"]) / o["entry"] * 100
            s["cum_ret"] += ret
            s["n"] += 1
            s["wins"] += 1 if ret > 0 else 0
            s["log"].append({
                "coin": coin, "dir": "L" if o["dir"] > 0 else "S",
                "entry": o["entry"], "exit": mid, "ret": round(ret, 2),
                "opened": o["t"], "closed": int(t_ms / 1000),
            })
            hold_h = (t_ms / 1000 - o["t"]) / 3600
            logger.success(f"[shadow-ws] {label} CLOSE {coin} "
                           f"{'L' if o['dir']>0 else 'S'} {o['entry']:.4g}→{mid:.4g} "
                           f"= {ret:+.2f}% ({hold_h:.1f}h) cum {s['cum_ret']:+.1f}% n={s['n']}")
            del s["open"][coin]
            persist = True

        # OPEN: flat→non-zero, or sign flip (post-close)
        if opened:
            if coin in s.get("baseline", []):
                # baseline holds first transition is the close above; this open scores normally
                # only skip if this is the FIRST open and we never closed yet
                pass
            if coin not in s["open"]:
                mid = float(mids.get(coin, 0)) or px
                s["open"][coin] = {
                    "dir": 1 if new_net > 0 else -1,
                    "entry": mid,
                    "t": int(t_ms / 1000),
                }
                logger.info(f"[shadow-ws] {label} OPEN {coin} "
                            f"{'L' if new_net>0 else 'S'} @ {mid:.4g} "
                            f"(fill px {px:.4g}, mid drift {(mid-px)/px*100:+.2f}%)")
                persist = True

        # Baseline clears once a held coin goes flat — re-entries then score
        if coin in s.get("baseline", []) and abs(new_net) <= FLAT_EPS:
            s["baseline"] = [c for c in s["baseline"] if c != coin]
            logger.info(f"[shadow-ws] {label} baseline {coin} cleared (now flat)")
            persist = True

        s["nets"][coin] = new_net
        if abs(new_net) <= FLAT_EPS:
            s["nets"].pop(coin, None)

    # Bounded dedup ring
    s["seen_fills"] = list(seen)[-SEEN_CAP:]
    persist = True
    if persist:
        _save_state_locked()


def _mids_handler(msg):
    """Update in-memory mids from allMids push (multiple coins per msg)."""
    try:
        data = msg.get("data") or {}
        mids_obj = data.get("mids") or {}
        for c, p in mids_obj.items():
            try:
                mids[c] = float(p)
            except Exception:
                continue
    except Exception as e:
        logger.exception(f"[shadow-ws] mids handler error: {e}")


def _scoreboard_loop():
    while True:
        time.sleep(SCOREBOARD_S)
        try:
            with state_lock:
                lines = []
                for label, s in state.items():
                    n = s["n"]; cum = s["cum_ret"]
                    wr = (s["wins"]/n*100) if n else 0
                    lines.append(f"  {label:<22} trips {n:>3} | WR {wr:>3.0f}% | "
                                 f"cumRet {cum:>+7.1f}% | paper ${cum/100*STAKE:>+8.0f} | "
                                 f"open {len(s.get('open', {}))}")
            logger.info("[shadow-ws] scoreboard:\n" + "\n".join(lines))
        except Exception as e:
            logger.warning(f"[shadow-ws] scoreboard error: {e}")


def main():
    global state
    with state_lock:
        state = _load_state()
        _seed_or_upgrade()
        _save_state_locked()

    logger.info(f"[shadow-ws] start | {len(CANDS)} candidates | WS push-mode | paper-only")

    ws = WebsocketManager(WS_BASE)
    ws.start()

    # Global allMids — ms-latency mark prices for paper entry/exit recording
    ws.subscribe({"type": "allMids"}, _mids_handler)

    # Per-candidate userFills — push on every on-chain fill
    for label, addr in CANDS.items():
        ws.subscribe({"type": "userFills", "user": addr}, _make_fills_handler(label))
        logger.info(f"[shadow-ws] subscribed userFills {label}={addr[:10]}…")

    threading.Thread(target=_scoreboard_loop, daemon=True).start()

    # Block forever; WS runs in its own thread
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
