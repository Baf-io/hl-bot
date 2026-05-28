#!/usr/bin/env python3
"""
BTC RANGE ALERT — ntfy push when BTC breaks the $74k–$78k box. Read-only, no trading.

Polls BTC mid; fires ONE push on a break below LOW or above HIGH, then LATCHES (won't re-fire
until price returns inside the band) so it never spams. Standalone systemd service.
Tune thresholds via env: BTC_ALERT_LOW, BTC_ALERT_HIGH, BTC_ALERT_POLL_S.
"""
import os, time, requests
from dotenv import load_dotenv
load_dotenv("/root/hl-bot/.env")

API="https://api.hyperliquid.xyz/info"
TOPIC=os.getenv("NTFY_TOPIC","")
LOW=float(os.getenv("BTC_ALERT_LOW","74000"))
HIGH=float(os.getenv("BTC_ALERT_HIGH","78000"))
POLL=int(os.getenv("BTC_ALERT_POLL_S","30"))

def push(title, msg, tags, prio="high"):
    try:
        requests.post(f"https://ntfy.sh/{TOPIC}", data=msg.encode(),
                      headers={"Title": title, "Priority": prio, "Tags": tags}, timeout=10)
    except Exception as e:
        print(f"push fail: {e}", flush=True)

def btc():
    j=requests.post(API, json={"type":"allMids"}, timeout=15).json()
    return float(j["BTC"])

def main():
    if not TOPIC:
        print("NTFY_TOPIC unset"); return
    state="inside"   # inside | below | above
    print(f"[btc-alert] start | band ${LOW:,.0f}-${HIGH:,.0f} | poll {POLL}s | topic {TOPIC}", flush=True)
    push("BTC range alert armed", f"Watching BTC for <${LOW:,.0f} / >${HIGH:,.0f}. Currently ${btc():,.0f}.", "eyes", "default")
    while True:
        try:
            p=btc()
        except Exception:
            time.sleep(POLL); continue
        if p < LOW and state != "below":
            push("🔻 BTC broke BELOW $74k", f"BTC ${p:,.0f} — below ${LOW:,.0f} support. Range-low break (your short TP zone).", "chart_with_downwards_trend")
            state="below"; print(f"[btc-alert] FIRED below @ {p}", flush=True)
        elif p > HIGH and state != "above":
            push("🔺 BTC broke ABOVE $78k", f"BTC ${p:,.0f} — above ${HIGH:,.0f} resistance. Range-high break / short-squeeze risk (your stop zone).", "chart_with_upwards_trend")
            state="above"; print(f"[btc-alert] FIRED above @ {p}", flush=True)
        elif LOW <= p <= HIGH:
            state="inside"   # re-arm both sides once back in the box
        time.sleep(POLL)

if __name__=="__main__": main()
