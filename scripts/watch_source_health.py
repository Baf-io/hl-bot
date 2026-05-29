#!/usr/bin/env python3
"""
SOURCE-HEALTH WATCHDOG — re-runs trust_forensic on every live-sleeve source +
every COPYABLE_DB Tier 1 candidate on a schedule. Detects:

  • SCORE DROP    — score fell DROP_THRESHOLD points below the historical baseline
  • ABSOLUTE FAIL — score < ABS_FAIL (some gate started failing or composite below floor)
  • GATE FLIP     — previously CLEAN, now hard-fails (new martingale / knife-trap / etc.)
  • RECOVERY      — previously failing, now CLEAN again (re-arm alerts on next deterioration)

Persists per-source baseline + last-N history to data/source_health.json so the
watchdog survives restarts without re-baselining. ntfy fires once per state-change
(latched) — no spam if a source stays bad. Read-only. No trading.

Env tunables:
  SH_INTERVAL_S        check cadence (default 604800 = 7 days)
  SH_DROP_THRESHOLD    points below baseline that triggers alert (default 15)
  SH_ABS_FAIL          absolute score floor (default 40)
  SH_HISTORY_KEEP      history entries retained per source (default 20)
"""
import os, sys, json, time, datetime as dt, requests
from dotenv import load_dotenv
load_dotenv("/root/hl-bot/.env")

sys.path.insert(0, "/root/hl-bot/scripts")
from trust_forensic import forensic, gates, cscore

STATE     = "/root/hl-bot/data/source_health.json"
TOPIC     = os.getenv("NTFY_TOPIC", "")
INTERVAL  = int(os.getenv("SH_INTERVAL_S", str(7 * 86400)))      # weekly
DROP      = float(os.getenv("SH_DROP_THRESHOLD", "15"))           # absolute pts
ABS_FAIL  = float(os.getenv("SH_ABS_FAIL", "40"))
HIST_KEEP = int(os.getenv("SH_HISTORY_KEEP", "20"))

# What to watch. Lead with the live sleeve source(s); follow with COPYABLE_DB Tier 1 +
# Downside (still shadowed). Roster traders are leaderboard-SHADOW-only; not included
# here to keep the weekly cycle short. Add a row if a sleeve goes live on a new source.
WATCHED = [
    ("0x77998579", "0x77998579f578c01030db65e75edc47bfe890c291", "LEAD sleeve src (live)"),
    ("0x4af52283", "0x4af52283ea6de9236c47b28e5dbf156453df8efb", "Downside vault (shadow)"),
    ("0xda830d2d", "0xda830d2d83a57cea255bcfd0cf89c3e94abde0fd", "DB Tier 1 — HYPE/majors"),
    ("0x5847fd14", "0x5847fd1490cbfb598116edd0e2901689aed65983", "DB Tier 1 — BTC pure"),
    ("0xc4ea203e", "0xc4ea203e2eb096c4d949b9a64a5d49c0a8a1d8b3", "DB Tier 1 — liquid majors"),
    ("0xe6deb805", "0xe6deb8055207cf89fd3111f581708705a1bd0c4f", "DB Tier 1 — BTC/SOL swing"),
    ("0x74dd1b67", "0x74dd1b672c1efbdd2559aa39e31cb56792a151bd", "DB Tier 1 — ETH/BNB"),
    ("0x422c3cf3", "0x422c3cf3457a85e9f369242340c023a72a4d6374", "DB Tier 1 — HYPE/ETH/XRP"),
    ("0x5c42b895", "0x5c42b895c1a7f42fe0f72ed2ba1fe442376fb61d", "DB Tier 1 — HYPE (borderline)"),
]


def push(title, msg, tags="rotating_light", prio="high"):
    if not TOPIC:
        print(f"  [no NTFY] {title}: {msg}", flush=True); return
    try:
        ascii_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Alert"
        requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode(),
                      headers={"Title": ascii_title, "Priority": prio, "Tags": tags}, timeout=10)
        print(f"  PUSHED: {ascii_title}", flush=True)
    except Exception as e:
        print(f"  push fail: {e}", flush=True)


def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}
def save_state(s): json.dump(s, open(STATE, "w"), indent=2)


def score_one(addr):
    """Returns (score, fails, metrics) where metrics has the key numbers we care about."""
    p = forensic(addr)
    if not p:
        return None, ["insufficient history"], None
    f = gates(p)
    s = 0.0 if f else float(cscore(p))
    metrics = {
        "wr": p.get("wr"),
        "n_closed": p.get("n_closed"),
        "concentration": p.get("concentration"),
        "avg_down_ratio": p.get("avg_down_ratio"),
        "payoff": p.get("payoff"),
        "paper_drag": p.get("paper_drag"),
        "realized": p.get("realized"),
        "recency_d": p.get("recency_d"),
    }
    return s, f, metrics


def check_one(short, addr, note, state):
    print(f"  [{short}] {note} — running forensic...", flush=True)
    score, fails, m = score_one(addr)
    now = int(time.time())
    prev = state.get(short, {})
    baseline = prev.get("baseline_score")
    last_score = prev.get("last_score")
    was_clean = (last_score or 0) > 0 and not (prev.get("last_fails") or [])

    entry = {
        "addr": addr, "note": note,
        "baseline_score": baseline if baseline is not None else score,
        "last_score": score,
        "last_check_ts": now,
        "last_check_iso": dt.datetime.fromtimestamp(now, dt.UTC).strftime("%Y-%m-%dT%H:%MZ"),
        "last_fails": fails or [],
        "history": (prev.get("history") or []) + [{
            "ts": now, "score": score, "fails": fails or [], **(m or {})
        }],
    }
    entry["history"] = entry["history"][-HIST_KEEP:]
    state[short] = entry

    if baseline is None:
        # First-ever check — seed baseline. If the seed is already failing, ntfy NOW —
        # otherwise we'd wait forever for a "drop from 0" that can't happen.
        print(f"     baseline seeded @ score={score} fails={fails}", flush=True)
        if (score or 0) < ABS_FAIL or fails:
            metrics_line = (f"wr={m['wr']}% n={m['n_closed']} payoff={m['payoff']} "
                            f"mart={int(m['avg_down_ratio']*100)}% paper={int(m['paper_drag']*100)}%"
                            if m else "(no metrics)")
            push(f"Source health: {short} BASELINE FAIL",
                 f"{note}\nFirst check is already failing — no historical baseline established.\n"
                 f"Fails: {', '.join(fails[:3]) if fails else 'low score'}\n{metrics_line}",
                 tags="warning", prio="default")
        return

    # Detection
    alerts = []
    if score is not None and baseline is not None:
        drop = baseline - score
        if drop >= DROP:
            alerts.append(("SCORE DROP", f"score {baseline:.1f}→{score:.1f} (-{drop:.1f}pts vs baseline)"))
        if score < ABS_FAIL and (last_score or baseline) >= ABS_FAIL:
            alerts.append(("ABS FAIL", f"score {score:.1f} < {ABS_FAIL:.0f} floor"))
    is_clean_now = (score or 0) > 0 and not fails
    if was_clean and not is_clean_now:
        alerts.append(("GATE FLIP", f"newly failing: {', '.join(fails[:2]) if fails else 'low score'}"))
    if (not was_clean) and is_clean_now and last_score is not None:
        alerts.append(("RECOVERY", f"back to CLEAN @ score {score:.1f}"))

    if alerts:
        kinds = " · ".join(k for k, _ in alerts)
        details = "\n".join(f"- {k}: {v}" for k, v in alerts)
        metrics_line = (f"wr={m['wr']}% n={m['n_closed']} payoff={m['payoff']} "
                        f"mart={int(m['avg_down_ratio']*100)}% paper={int(m['paper_drag']*100)}%"
                        if m else "(no metrics)")
        msg = (f"{note}\n{details}\n\n{metrics_line}\nrealized=${m['realized'] if m else '?':,}, "
               f"recency={m['recency_d'] if m else '?'}d")
        push(f"Source health: {short} {kinds}", msg,
             tags="rotating_light" if "RECOVERY" not in kinds else "green_circle",
             prio="high" if "RECOVERY" not in kinds else "default")


def run_cycle():
    print(f"\n[source-health] cycle start {dt.datetime.now(dt.UTC).strftime('%Y-%m-%d %H:%MUTC')} — {len(WATCHED)} sources", flush=True)
    state = load_state()
    for short, addr, note in WATCHED:
        try:
            check_one(short, addr, note, state)
        except Exception as e:
            print(f"  [{short}] EXC: {e}", flush=True)
        save_state(state)
        time.sleep(2)   # be polite to the HL info API across batch checks
    # Scoreboard line at end of cycle
    rows = [(s, st.get("last_score", 0), st.get("baseline_score", 0), st.get("note", ""))
            for s, st in state.items()]
    rows.sort(key=lambda r: -r[1])
    print("[source-health] scoreboard (score | baseline | source):", flush=True)
    for s, sc, bl, note in rows:
        delta = (sc or 0) - (bl or 0)
        print(f"  {s} {sc or 0:>5.1f} | bl {bl or 0:>5.1f} | Δ{delta:+5.1f}  {note}", flush=True)


def main():
    print(f"[source-health] start | {len(WATCHED)} sources | interval {INTERVAL}s "
          f"| drop={DROP} abs_fail={ABS_FAIL} | topic={TOPIC or 'unset'}", flush=True)
    # Run once immediately so a fresh deploy gets a baseline + a current scoreboard
    while True:
        try:
            run_cycle()
        except Exception as e:
            print(f"[source-health] cycle EXC: {e}", flush=True)
        print(f"[source-health] next cycle in {INTERVAL}s "
              f"(~{INTERVAL//3600}h)", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
