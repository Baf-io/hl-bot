#!/usr/bin/env python3
"""
SMART-MONEY FLIP ALERT — ntfy push when ≥N tracked sources rotate the same direction on the same
coin within a sliding window. Read-only, no trading.

Per-source per-coin direction is polled every FLIP_POLL_S; a transition (SHORT→LONG, FLAT→LONG,
or the bearish inverse) goes onto a rolling FLIP_WINDOW_H buffer. When ≥FLIP_THRESHOLD distinct
sources cluster on the same coin+direction within the window, fire ONE ntfy push and latch the
direction (won't re-fire on the same cluster until the cohort flips back).
"""
import os, json, time, datetime as dt, requests
from collections import defaultdict, deque
from dotenv import load_dotenv
load_dotenv("/root/hl-bot/.env")

API="https://api.hyperliquid.xyz/info"
TOPIC = os.getenv("NTFY_TOPIC","")
POLL  = int(os.getenv("FLIP_POLL_S","300"))           # 5 min default
THRESHOLD = int(os.getenv("FLIP_THRESHOLD","2"))       # ≥N distinct sources
WINDOW_H  = float(os.getenv("FLIP_WINDOW_H","6"))      # rolling hours
STATE     = "/root/hl-bot/data/flip_alert_state.json"

# Watchlist — high-trust durable sources only
SOURCES = {
    "0x77998579": ("0x77998579f578c01030db65e75edc47bfe890c291", "LEAD-66.2"),
    "0x78aa6328": ("0x78aa6328eae8028a089c35d2819f79c78de2a7e5", "78aa-BTC-tracker"),
    "0x4af52283": ("0x4af52283ea6de9236c47b28e5dbf156453df8efb", "Downside-vault"),
    "0x807ddb66": ("0x807ddb66768050ee457165ad6d21cc1275a896ac", "807-vetted-swing"),
    "0x3093189b": ("0x3093189bd4d429cb2f47d03c14b9e9200b6b9425", "BTC-mega-swing-527d"),
    "0xf83858e5": ("0xf83858e57d9f804f5ca1603bce82558119aeac7b", "gen-2.4yr"),
    "0x41829013": ("0x41829013ce94b21131b68d5a7b0247bd4eefce96", "multi-coin-fund"),
    "0xeeaf4055": ("0xeeaf4055e581dae6a4eaea0a66181ecb207787f1", "ETH-bear-specialist"),
    "0x99df385a": ("0x99df385a3cb3a44d79532f2fa698c248c31909b2", "HYPE-specialist-543d"),
    "0xf94b3463": ("0xf94b346387ae5f1ae84bf10e36008cb552ca82d5", "BTC-ZEC-swing"),
    "0xdbbb9b4e": ("0xdbbb9b4e361e1e1b6b4c640914ed4e4a11896df9", "multi-bear-248d"),
}
COINS = {"BTC","ETH","SOL","HYPE"}

def post(t,**k):
    try: return requests.post(API, json={"type":t, **k}, timeout=15).json()
    except Exception as e: print(f"  api fail {t}: {e}", flush=True); return None

def push(title, msg, tags="bell", prio="high"):
    if not TOPIC:
        print(f"  [no NTFY] {title}: {msg}", flush=True); return
    try:
        requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode(),
                      headers={"Title": title, "Priority": prio, "Tags": tags}, timeout=10)
        print(f"  PUSHED: {title}", flush=True)
    except Exception as e:
        print(f"  push fail: {e}", flush=True)

def direction(addr, coin):
    """Returns +1 (long), -1 (short), 0 (flat). None on api fail."""
    ch = post("clearinghouseState", user=addr)
    if not ch: return None
    for p in ch.get("assetPositions", []):
        pp = p["position"]
        if pp.get("coin") != coin: continue
        sz = float(pp.get("szi", 0))
        if abs(sz) < 1e-9: return 0
        return 1 if sz > 0 else -1
    return 0

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {"dir": {}, "events": [], "latched": {}}

def save_state(s): json.dump(s, open(STATE, "w"))

def main():
    print(f"[flip-alerts] start | {len(SOURCES)} sources × {len(COINS)} coins | "
          f"threshold ≥{THRESHOLD} in {WINDOW_H}h window | poll {POLL}s | topic={TOPIC or 'unset'}", flush=True)
    state = load_state()
    if "dir" not in state: state["dir"] = {}
    if "events" not in state: state["events"] = []
    if "latched" not in state: state["latched"] = {}
    first_run = not state["dir"]
    if first_run:
        push("Flip-alert armed", f"Watching {len(SOURCES)} smart-money sources on {','.join(sorted(COINS))} for ≥{THRESHOLD}-cluster flips within {WINDOW_H}h. Baseline-seeding now.", "eyes", "default")

    while True:
        try:
            now = time.time()
            for sk, (addr, tag) in SOURCES.items():
                for coin in COINS:
                    key = f"{sk}_{coin}"
                    new_d = direction(addr, coin)
                    if new_d is None: continue
                    prev_d = state["dir"].get(key)
                    if first_run or prev_d is None:
                        state["dir"][key] = new_d
                        continue
                    if new_d != prev_d:
                        # Bullish transition: SHORT→LONG or FLAT→LONG
                        if new_d == 1 and prev_d in (-1, 0):
                            ev_dir = "LONG"
                            event = {"t": now, "src": sk, "tag": tag, "coin": coin, "dir": ev_dir, "from": "SHORT" if prev_d == -1 else "FLAT"}
                            state["events"].append(event)
                            print(f"  flip: {sk} {coin} {prev_d}→{new_d} ({event['from']}→LONG) [{tag}]", flush=True)
                        # Bearish transition: LONG→SHORT or FLAT→SHORT
                        elif new_d == -1 and prev_d in (1, 0):
                            ev_dir = "SHORT"
                            event = {"t": now, "src": sk, "tag": tag, "coin": coin, "dir": ev_dir, "from": "LONG" if prev_d == 1 else "FLAT"}
                            state["events"].append(event)
                            print(f"  flip: {sk} {coin} {prev_d}→{new_d} ({event['from']}→SHORT) [{tag}]", flush=True)
                        # else: closing to flat (LONG→FLAT or SHORT→FLAT) — not a flip, skip
                        state["dir"][key] = new_d
                    time.sleep(0.12)   # rate-limit per source

            # Drop expired events
            cutoff = now - WINDOW_H * 3600
            state["events"] = [e for e in state["events"] if e["t"] >= cutoff]

            # Check clusters: ≥THRESHOLD distinct sources flipping same direction on same coin in window
            buckets = defaultdict(set)   # (coin, dir) → set of source keys
            details = defaultdict(list)
            for e in state["events"]:
                buckets[(e["coin"], e["dir"])].add(e["src"])
                details[(e["coin"], e["dir"])].append(e)

            for (coin, ev_dir), sources in buckets.items():
                if len(sources) < THRESHOLD: continue
                latch_key = f"{coin}_{ev_dir}"
                if state["latched"].get(latch_key): continue   # already fired this cluster
                # FIRE
                evs = details[(coin, ev_dir)]
                lines = [f"{e['tag']} ({e['src']}) {e['from']}→{ev_dir}  {dt.datetime.utcfromtimestamp(e['t']).strftime('%H:%MUTC')}" for e in evs[-6:]]
                arrow = "🟢🔼" if ev_dir == "LONG" else "🔴🔽"
                push(f"{arrow} {len(sources)} sources rotated {ev_dir} on {coin}",
                     f"In last {WINDOW_H:.0f}h:\n" + "\n".join(lines),
                     "rotating_light" if ev_dir == "LONG" else "chart_with_downwards_trend")
                state["latched"][latch_key] = now
                # Clear the OPPOSITE direction's latch (cohort changed mind → re-arm reverse)
                opp = f"{coin}_{'SHORT' if ev_dir=='LONG' else 'LONG'}"
                state["latched"].pop(opp, None)

            # Expire latches: when no events in window for that (coin,dir), clear latch so future re-firings can happen
            for latch_key in list(state["latched"].keys()):
                coin, ev_dir = latch_key.rsplit("_", 1)
                if (coin, ev_dir) not in buckets:
                    print(f"  re-arming latch {latch_key} (cluster aged out)", flush=True)
                    del state["latched"][latch_key]

            save_state(state)
            first_run = False
        except Exception as e:
            print(f"  loop error: {e}", flush=True)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
