#!/usr/bin/env python3
"""
Shadow-validation of leaderboard scan candidates — PAPER ONLY, never trades.

Watches each candidate's live positions and records their FRESH round-trips out-of-sample:
open (flat→position or flip) → entry at current mark; close (→flat or flip) → exit at mark,
paper return = direction × (exit−entry)/entry. Per-trader cumulative return%, win-rate, and
trade log let us see which names actually hold up before risking any capital.

Discipline (mirrors the live engine):
- FRESH-ENTRY ONLY: positions held at startup are seeded as BASELINE and NOT credited — we
  only score entries that open after we begin watching (no stale-adoption inflation).
- Equal-weight % per round-trip (leverage-agnostic signal quality), $100 nominal stake for $ display.
- Clean-read-only; state persisted to data/shadow_scan_state.json so restarts don't lose history.

Runs as hl-shadow-scan.service. No keys needed beyond read-only API.
"""
import sys, time, json, os, datetime as dt

sys.path.insert(0, "/root/hl-bot")
sys.path.insert(0, "/root/hl-bot/src")

import requests
from loguru import logger

API   = "https://api.hyperliquid.xyz/info"
POLL_S = 300     # 5 min — paper validation doesn't need 60s; saves rate-limit budget
STAKE  = 100.0   # nominal $ per paper position (for $ display; ranking uses return%)
STATE  = "/root/hl-bot/data/shadow_scan_state.json"

# Rewritten 2026-05-29 after the 3,837-wallet scan + upgraded forensic gates
# (knife-trap, paper-drag, sample-aware concentration, warn-vs-reject tiering).
#
# ALL 5 sister-agent picks from the bigger data bank (0x9c16bc8f bear monster,
# 0x6bea81d7, 0xf8999371, 0x9c972d06, 0x27c5fdef) hard-rejected under the new
# forensic — same 99%-WR loss-hider footprint that's failed every prior whale
# sweep. Bigger pool, same lesson: real copy edge is rare. Do not shadow them.
#
# What IS shadow-worthy: the 4 COPYABLE_DB Tier 1 names that pass forensic
# but aren't yet sleeved. Shadow tracks "what would a sleeve have realized"
# while source-health tracks the trust score — complementary signals.
#
# Kept 36f2 from the prior batch (67% WR small sample, still inconclusive).
CANDS = {
    "36f2_patient":         "0x36f26e2e5bed062968c17fc770863fd740713205",
    "da830d2d_HYPEmajors":  "0xda830d2d83a57cea255bcfd0cf89c3e94abde0fd",
    "c4ea203e_liquidmajor": "0xc4ea203e2eb096c4d949b9a64a5d49c0a8a1d8b3",
    "e6deb805_BTCSOLswing": "0xe6deb8055207cf89fd3111f581708705a1bd0c4f",
    "74dd1b67_ETHBNB":      "0x74dd1b672c1efbdd2559aa39e31cb56792a151bd",
}
FLAT_EPS = 1e-9


def _post(t, **k):
    return requests.post(API, json={"type": t, **k}, timeout=20).json()


def _positions(addr):
    """coin → signed size for an address."""
    cs = _post("clearinghouseState", user=addr)
    out = {}
    for ap in cs.get("assetPositions", []):
        p = ap["position"]; szi = float(p.get("szi", 0))
        if abs(szi) > FLAT_EPS:
            out[p["coin"]] = szi
    return out


def _load():
    if os.path.exists(STATE):
        try: return json.load(open(STATE))
        except Exception: pass
    return {}


def _save(state):
    tmp = STATE + ".tmp"
    json.dump(state, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def _summary(state):
    lines = []
    for label, s in state.items():
        n = s["n"]; cum = s["cum_ret"]; wr = (s["wins"] / n * 100) if n else 0
        lines.append(f"  {label:<18} trips {n:>3} | WR {wr:>3.0f}% | cumRet {cum:>+7.1f}% | paper ${cum/100*STAKE:>+8.0f} | open {len(s['open'])}")
    return "\n".join(lines)


def main():
    state = _load()
    mids = _post("allMids")
    for label, addr in CANDS.items():
        if label not in state:
            held = _positions(addr)
            # seed baseline: current holds are NOT credited (fresh-entry discipline)
            state[label] = {"addr": addr, "open": {}, "baseline": list(held.keys()),
                            "cum_ret": 0.0, "wins": 0, "n": 0, "log": []}
            logger.info(f"[shadow] seed {label} baseline (not scored): {list(held.keys()) or 'flat'}")
    _save(state)
    logger.info(f"[shadow] start | {len(CANDS)} candidates | poll {POLL_S}s | paper-only")

    while True:
        try:
            mids = _post("allMids")
        except Exception as e:
            logger.warning(f"[shadow] mids read failed (skip): {e}"); time.sleep(POLL_S); continue

        for label, addr in CANDS.items():
            s = state[label]
            try:
                pos = _positions(addr)
            except Exception as e:
                logger.warning(f"[shadow] {label} read failed (skip): {e}"); continue

            # CLOSES / FLIPS: coin was open for us, now flat or flipped
            for coin in list(s["open"].keys()):
                o = s["open"][coin]
                cur = pos.get(coin, 0.0)
                flipped = (cur != 0 and (cur > 0) != (o["dir"] > 0))
                if abs(cur) <= FLAT_EPS or flipped:
                    px = float(mids.get(coin, 0)) or o["entry"]
                    ret = (1 if o["dir"] > 0 else -1) * (px - o["entry"]) / o["entry"] * 100
                    s["cum_ret"] += ret; s["n"] += 1; s["wins"] += 1 if ret > 0 else 0
                    s["log"].append({"coin": coin, "dir": "L" if o["dir"] > 0 else "S",
                                     "entry": o["entry"], "exit": px, "ret": round(ret, 2),
                                     "opened": o["t"], "closed": int(time.time())})
                    logger.success(f"[shadow] {label} CLOSE {coin} {'L' if o['dir']>0 else 'S'} "
                                   f"{o['entry']:.4g}→{px:.4g} = {ret:+.1f}% (cum {s['cum_ret']:+.1f}%)")
                    del s["open"][coin]

            # OPENS: coin now held that we aren't tracking AND wasn't a seeded baseline still-open
            for coin, szi in pos.items():
                if coin in s["open"]:
                    continue
                # skip a baseline position until it has been closed once (then future re-opens count)
                if coin in s.get("baseline", []):
                    continue
                px = float(mids.get(coin, 0))
                if px <= 0:
                    continue
                s["open"][coin] = {"dir": 1 if szi > 0 else -1, "entry": px, "t": int(time.time())}
                logger.info(f"[shadow] {label} OPEN {coin} {'L' if szi>0 else 'S'} @ {px:.4g}")

            # once a baseline coin goes flat, drop it from baseline so re-entries are scored
            s["baseline"] = [c for c in s.get("baseline", []) if c in pos]

        _save(state)
        # periodic scoreboard (every ~30 min)
        if int(time.time()) % 1800 < POLL_S:
            logger.info("[shadow] scoreboard:\n" + _summary(state))
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
