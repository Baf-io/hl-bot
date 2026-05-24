#!/usr/bin/env python3
"""
Read-only preview of the scale-out profit-taking on our LIVE book.

Mirrors src/signals/leaderboard_copy._scale_out + _atr_pct exactly, reading every
threshold from config.settings so it never drifts from the running bot. Pulls our
current Hyperliquid positions and prints, per coin, how far each is from its TP1
trim trigger. Pure info queries — places NO orders.

    .venv/bin/python scripts/preview_scaleout.py

Caveat: runner (post-TP1) exits trail off the in-memory peak_price_pct, which only
exists inside the running process — they can't be previewed here, only TP1 distance.
"""
import os
import sys
import time

import requests

# Make `config` importable regardless of where this is run from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings as s  # noqa: E402

BASE = (
    "https://api.hyperliquid-testnet.xyz/info"
    if s.HL_TESTNET
    else "https://api.hyperliquid.xyz/info"
)


def _post(payload: dict):
    return requests.post(BASE, json=payload, timeout=10).json()


def atr_pct(coin: str, period: int = None):
    """Daily ATR(period) / last close — identical math to the bot's _atr_pct."""
    period = period or s.ATR_PERIOD
    end = int(time.time() * 1000)
    start = end - (period + 5) * 86_400_000
    candles = _post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1d", "startTime": start, "endTime": end,
    }})
    if not isinstance(candles, list) or len(candles) < period + 1:
        return None
    trs = [
        max(
            float(candles[i]["h"]) - float(candles[i]["l"]),
            abs(float(candles[i]["h"]) - float(candles[i - 1]["c"])),
            abs(float(candles[i]["l"]) - float(candles[i - 1]["c"])),
        )
        for i in range(1, len(candles))
    ]
    last_close = float(candles[-1]["c"])
    if last_close <= 0:
        return None
    return (sum(trs[-period:]) / period) / last_close


def main():
    state = _post({"type": "clearinghouseState", "user": s.HL_WALLET_ADDRESS})
    mids = _post({"type": "allMids"})
    equity = float(state.get("marginSummary", {}).get("accountValue", 0))

    net = "MAINNET" if not s.HL_TESTNET else "TESTNET"
    print(
        f"{net}  equity=${equity:,.0f}  | TP1 {s.SCALEOUT_TP1_FRACTION:.0%} @ "
        f"max({s.SCALEOUT_MIN_ATR_MULT}×ATR, {s.SCALEOUT_TP1_MARGIN_RET:.0%}/lev), "
        f"runner {s.SCALEOUT_RUNNER_TRAIL_ATR}×ATR trail\n"
    )
    hdr = f"{'coin':6}{'dir':6}{'lev':>4}{'now%':>8}{'ATR%':>7}{'TP1@':>7}  status"
    print(hdr)
    print("-" * 72)

    n = 0
    for ap in state.get("assetPositions", []):
        pos = ap["position"]
        szi = float(pos.get("szi", 0))
        if szi == 0:
            continue
        n += 1
        coin = pos["coin"]
        direction = "long" if szi > 0 else "short"
        entry = float(pos.get("entryPx") or 0)
        notional = abs(float(pos.get("positionValue") or 0))
        margin_used = float(pos.get("marginUsed") or 0)
        lev = (min(notional / margin_used, s.COPY_MAX_COPY_LEVERAGE)
               if margin_used > 0 else 1.0)
        lev = max(lev, 1.0)
        px = float(mids.get(coin, 0) or 0)
        exc = (((px - entry) / entry) if direction == "long"
               else ((entry - px) / entry)) if (px and entry) else 0.0

        atr = atr_pct(coin)
        if not atr:
            print(f"{coin:6}{direction:6}{lev:3.0f}x{exc * 100:7.1f}%"
                  f"    n/a    n/a  SKIP (no ATR — bot won't act)")
            continue
        trig = max(s.SCALEOUT_MIN_ATR_MULT * atr, s.SCALEOUT_TP1_MARGIN_RET / lev)
        binder = "ATR" if s.SCALEOUT_MIN_ATR_MULT * atr >= s.SCALEOUT_TP1_MARGIN_RET / lev else "mgn"
        if exc >= trig:
            status = f"TP1 HIT → would trim {s.SCALEOUT_TP1_FRACTION:.0%} [{binder}]"
        else:
            status = f"hold (+{(trig - exc) * 100:.1f}% to TP1) [{binder}]"
        print(f"{coin:6}{direction:6}{lev:3.0f}x{exc * 100:7.1f}%"
              f"{atr * 100:6.1f}%{trig * 100:6.1f}%  {status}")

    if n == 0:
        print("(no open positions)")


if __name__ == "__main__":
    main()
