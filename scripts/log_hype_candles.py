#!/usr/bin/env python3
"""
HYPE 1m candle logger — for predictive-trigger reverse-engineering later.

HL discards 1m candles after ~4 days. To re-run the bbf82c80 trigger analysis
with a larger sample (need n≥80 fresh opens, ~60 days at his cadence), we need
local archived candle history. This service polls 1m candles every 30s, dedups
by candle start time, and appends new candles to a JSONL.

Cheap: ~2 API calls/min, append-only writes. Designed to run 60+ days untouched.

Output: data/hype_candles_1m.jsonl   (one candle per line, no rewriting)
"""
import sys, os, json, time, requests, datetime as dt
sys.path.insert(0, "/root/hl-bot"); sys.path.insert(0, "/root/hl-bot/src")
from loguru import logger

API = "https://api.hyperliquid.xyz/info"
COIN = os.getenv("CANDLE_COIN", "HYPE")
INTERVAL = "1m"
OUT = f"/root/hl-bot/data/{COIN.lower()}_candles_1m.jsonl"
POLL_S = 30

def _post(t, **k):
    return requests.post(API, json={"type": t, **k}, timeout=15).json()

def _existing_starts():
    seen = set()
    if os.path.exists(OUT):
        for ln in open(OUT):
            try: seen.add(json.loads(ln).get("t"))
            except: continue
    return seen

def main():
    seen = _existing_starts()
    logger.info(f"[candle-log] start {COIN} {INTERVAL} → {OUT} ({len(seen)} prior candles)")
    while True:
        try:
            now_ms = int(time.time() * 1000)
            req = {"coin": COIN, "interval": INTERVAL,
                   "startTime": now_ms - 3600*1000,   # last hour buffer
                   "endTime": now_ms}
            candles = _post("candleSnapshot", req=req)
            if isinstance(candles, list):
                new_count = 0
                with open(OUT, "a") as f:
                    for c in candles:
                        if c.get("t") in seen:
                            continue
                        seen.add(c.get("t"))
                        f.write(json.dumps(c) + "\n")
                        new_count += 1
                if new_count:
                    logger.info(f"[candle-log] +{new_count} candles (total {len(seen)})")
        except Exception as e:
            logger.warning(f"[candle-log] poll error: {e}")
        time.sleep(POLL_S)

if __name__ == "__main__":
    main()
