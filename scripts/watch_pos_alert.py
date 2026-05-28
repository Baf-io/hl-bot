#!/usr/bin/env python3
"""
POSITION WATCHDOG — ntfy push for a single discretionary trade:
  1. LIMIT-FILL: a target order oid disappears + position size grew  →  push "filled"
  2. ADVERSE-MOVE ladder: price crosses escalating thresholds  →  push "going against you"
  3. AUTO-DISARM: position closes, flips, or price goes back FAVORABLY past the green threshold

Each trigger fires ONCE then latches (no spam). Read-only, no trading. Env-parameterized
so future trades can re-use the same script.
"""
import os, json, time, requests
from dotenv import load_dotenv
load_dotenv("/root/hl-bot/.env")

API   = "https://api.hyperliquid.xyz/info"
TOPIC = os.getenv("NTFY_TOPIC","")
ACCT  = os.getenv("POSW_ACCT","").lower()
COIN  = os.getenv("POSW_COIN","BTC")
DIR   = os.getenv("POSW_DIR","SHORT").upper()                # SHORT or LONG
LIMIT_OID = os.getenv("POSW_LIMIT_OID","")                   # optional: track this oid for fill detection
LIMIT_PX  = float(os.getenv("POSW_LIMIT_PX","0"))            # only for the fill message
ENTRY_PX  = float(os.getenv("POSW_ENTRY_PX","0"))            # current avg entry (for context)
ADVERSE   = [float(x) for x in os.getenv("POSW_ADVERSE","").split(",") if x.strip()]   # csv of adverse prices
GREEN_PX  = float(os.getenv("POSW_GREEN","0"))               # price that means "the trade is working" → re-arm adverse
POLL      = int(os.getenv("POSW_POLL_S","30"))
STATE     = f"/root/hl-bot/data/pos_alert_{COIN}_state.json"

def post(t,**k):
    try: return requests.post(API, json={"type":t, **k}, timeout=12).json()
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

def position_size(state_resp):
    """Returns signed BTC size; 0 if flat or missing."""
    for p in state_resp.get("assetPositions", []):
        if p["position"].get("coin") == COIN:
            return float(p["position"].get("szi", 0))
    return 0.0

def has_oid(orders, oid):
    return any(str(o.get("oid")) == str(oid) for o in (orders or []))

def load():
    try: return json.load(open(STATE))
    except Exception: return {}
def save(s): json.dump(s, open(STATE,"w"))

def main():
    if not ACCT or not COIN:
        print("POSW_ACCT/POSW_COIN unset"); return
    print(f"[pos-watch] start | acct={ACCT[:10]}… {COIN} {DIR} entry=${ENTRY_PX:,.0f} "
          f"oid={LIMIT_OID or 'none'}@${LIMIT_PX:,.0f} adverse={ADVERSE} green=${GREEN_PX:,.0f} "
          f"poll={POLL}s topic={TOPIC or 'unset'}", flush=True)
    state = load()
    if not state:
        push(f"Pos-watch armed: {COIN} {DIR}",
             f"Watching {COIN} {DIR} @ ${ENTRY_PX:,.0f}. Limit ${LIMIT_PX:,.0f} (oid {LIMIT_OID or 'n/a'}). "
             f"Adverse levels: {ADVERSE}. Re-arm if BTC <${GREEN_PX:,.0f}.", "eyes", "default")
        state = {"fired_adverse": [], "fired_fill": False, "last_size": None}

    fired_adv = set(state.get("fired_adverse", []))
    fired_fill = state.get("fired_fill", False)
    last_size = state.get("last_size")

    while True:
        try:
            ch = post("clearinghouseState", user=ACCT)
            oo = post("openOrders", user=ACCT)
            mids = post("allMids")
            if not (ch and mids is not None):
                time.sleep(POLL); continue
            px = float(mids.get(COIN, 0))
            sz = position_size(ch)
            if last_size is None: last_size = sz

            # (1) FILL DETECTION
            if not fired_fill and LIMIT_OID:
                has = has_oid(oo, LIMIT_OID)
                grew = (DIR == "SHORT" and sz < last_size - 1e-9) or (DIR == "LONG" and sz > last_size + 1e-9)
                if (not has) and grew:
                    avg = "n/a"
                    for p in ch.get("assetPositions", []):
                        if p["position"].get("coin") == COIN:
                            avg = p["position"].get("entryPx", "n/a")
                    push(f"🎯 {COIN} scale-in FILLED @ ${LIMIT_PX:,.0f}",
                         f"{COIN} {DIR} now {abs(sz):.5f} (was {abs(last_size):.5f}). New avg ~${avg}. Current ${px:,.0f}.",
                         "dart")
                    fired_fill = True

            # (2) AUTO-DISARM: position flat or flipped direction
            if (DIR == "SHORT" and sz >= 0) or (DIR == "LONG" and sz <= 0):
                # Either FLAT or FLIPPED. Push once.
                if not state.get("disarmed"):
                    if abs(sz) < 1e-9:
                        push(f"✅ {COIN} {DIR} position CLOSED", f"Pos-watch disarming. Final size 0. BTC ${px:,.0f}.", "white_check_mark")
                    else:
                        side = "LONG" if sz > 0 else "SHORT"
                        push(f"↔️ {COIN} position FLIPPED to {side}", f"Was {DIR}, now {side} {abs(sz):.5f}. Watchdog disarming.", "twisted_rightwards_arrows")
                    state["disarmed"] = True; save({**state, "fired_adverse": list(fired_adv), "fired_fill": fired_fill, "last_size": sz})
                time.sleep(POLL); continue

            # (3) GREEN RE-ARM: if price went favorably past the green threshold, re-arm adverse pings
            if GREEN_PX > 0:
                favorable = (DIR == "SHORT" and px < GREEN_PX) or (DIR == "LONG" and px > GREEN_PX)
                if favorable and fired_adv:
                    print(f"  green re-arm @ ${px:,.0f}: clearing adverse latches {sorted(fired_adv)}", flush=True)
                    push(f"🟢 {COIN} trade working (re-arming alerts)",
                         f"{COIN} ${px:,.0f} crossed favorable threshold ${GREEN_PX:,.0f}. Adverse pings re-armed.",
                         "green_circle", "default")
                    fired_adv = set()

            # (4) ADVERSE-MOVE LADDER
            for lvl in ADVERSE:
                if lvl in fired_adv: continue
                hit = (DIR == "SHORT" and px >= lvl) or (DIR == "LONG" and px <= lvl)
                if hit:
                    # Compute the rough P&L at this level on current size
                    pnl = -(px - ENTRY_PX) * abs(sz) if DIR == "SHORT" else (px - ENTRY_PX) * abs(sz)
                    arrow = "🔺" if DIR == "SHORT" else "🔻"
                    push(f"{arrow} {COIN} crossed ${lvl:,.0f} (against {DIR})",
                         f"{COIN} ${px:,.0f} (entry ${ENTRY_PX:,.0f}, size {abs(sz):.5f}). uPnL ~${pnl:+.0f}. Level {lvl:,.0f} adverse.",
                         "rotating_light")
                    fired_adv.add(lvl)

            last_size = sz
            save({**state, "fired_adverse": list(fired_adv), "fired_fill": fired_fill, "last_size": sz})
        except Exception as e:
            print(f"  loop error: {e}", flush=True)
        time.sleep(POLL)

if __name__ == "__main__":
    main()
