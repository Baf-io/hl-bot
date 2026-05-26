"""
scan_common.py — shared hold-time / cadence analytics for the trader scanners.

WHY THIS EXISTS
───────────────
Every scanner used to estimate hold time the same broken way:

    opens[coin] = ts                      # overwrites on each Open fill
    elif "Close" in dir: pair with opens  # within a 30-day window only

That has three failure modes, all biased against the traders we actually want:

  1. WINDOW TRUNCATION → "?". Pairing only inside a 30-day slice means a position
     opened weeks ago (or opened >30d ago and closed recently) has no Open fill in
     the window, so nothing pairs → empty list → sentinel (99/"?"). The LONGEST,
     most copy-able holders are exactly the ones that show "?", while a fast
     mean-reversion scalper who round-trips inside the window reads as a clean,
     low, "passing" number. Backwards for a copy bot whose thesis is "mirror slow
     holders" — the scalper looks clean, the holder looks broken.
  2. CURRENTLY-OPEN POSITIONS INVISIBLE. A still-open position has no Close fill,
     so a buy-and-hold trader's most important positions never count.
  3. TWAP OVERWRITE + MEAN SKEW. `opens[coin]=ts` keeps the LAST open of a TWAP'd
     entry, not the first, under-measuring the hold; and the reported mean is
     skewed by TWAP fragments and the odd scalp.

THE FIX (here)
──────────────
Reconstruct position EPISODES from signed fill size over FULL history, per coin.
Net position is seeded from each coin's first fill `startPosition` (so a truncated
fill history still anchors correctly), then walked fill-by-fill:
  • net leaves 0            → episode opens (record first-open ts)
  • net returns to 0        → episode closes (hold = close_ts − open_ts)
  • net flips through 0     → close old episode, open a new one at this ts
  • net != 0 at the end     → episode still OPEN (age = now − open_ts)

We report MEDIAN closed-hold and MEDIAN open-age SEPARATELY (plus a cadence split:
% of closed episodes that are intraday <1h vs multi-day >=24h) — that split is the
real swing-vs-scalper tell, independent of win rate.
"""
from collections import defaultdict

MS_HOUR = 3_600_000.0
_EPS = 1e-6                         # treat |net| below this as flat (float residue)
_MAX_CLOSED_H = 24 * 365.0         # ignore absurd pairs (bad data), but DON'T cap at 30d
_MAX_OPEN_H   = 24 * 730.0


def _fill_delta(fill) -> float:
    """Signed size change from a single fill. Prefer `side` (B=buy/+, A=sell/−);
    fall back to the `dir` label if `side` is absent."""
    sz = abs(float(fill.get("sz", 0) or 0))
    if sz == 0:
        return 0.0
    side = fill.get("side", "")
    if side == "B":
        return sz
    if side == "A":
        return -sz
    # Fallback: infer from dir text. Open Long / Close Short increase net; the
    # opposite decrease it.
    d = str(fill.get("dir", ""))
    if ("Open" in d and "Long" in d) or ("Close" in d and "Short" in d):
        return sz
    if ("Open" in d and "Short" in d) or ("Close" in d and "Long" in d):
        return -sz
    return 0.0


def episode_holds(fills, now_ms, coin=None):
    """
    Reconstruct per-coin position episodes over the FULL fill history.

    Returns (closed_h, open_h):
      closed_h : hold durations (hours) of episodes that returned to flat / flipped
      open_h   : ages (hours) of episodes still open at now_ms

    `coin` limits to a single coin; otherwise every coin is reconstructed.
    """
    by_coin = defaultdict(list)
    for f in fills:
        c = f.get("coin", "")
        if coin is not None and c != coin:
            continue
        by_coin[c].append(f)

    closed, open_ = [], []
    for fl in by_coin.values():
        fl = sorted(fl, key=lambda f: float(f.get("time", 0) or 0))

        # Seed net from the first fill's pre-trade position so a truncated history
        # (open leg older than the oldest fill we have) still anchors correctly.
        try:
            net = float(fl[0].get("startPosition"))
        except (TypeError, ValueError):
            net = 0.0
        ep_start = float(fl[0].get("time", 0) or 0) if abs(net) > _EPS else None

        for f in fl:
            ts = float(f.get("time", 0) or 0)
            prev = net
            net = round(net + _fill_delta(f), 6)

            prev_flat, now_flat = abs(prev) <= _EPS, abs(net) <= _EPS
            if prev_flat and not now_flat:                       # opened from flat
                ep_start = ts
            elif not prev_flat and now_flat:                     # closed to flat
                if ep_start is not None:
                    h = (ts - ep_start) / MS_HOUR
                    if 0 < h < _MAX_CLOSED_H:
                        closed.append(h)
                ep_start = None
            elif not prev_flat and not now_flat and (prev > 0) != (net > 0):
                if ep_start is not None:                         # flipped through 0
                    h = (ts - ep_start) / MS_HOUR
                    if 0 < h < _MAX_CLOSED_H:
                        closed.append(h)
                ep_start = ts

        if abs(net) > _EPS and ep_start is not None:             # still open at `now`
            h = (now_ms - ep_start) / MS_HOUR
            if 0 < h < _MAX_OPEN_H:
                open_.append(h)

    return closed, open_


def median(xs):
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def hold_stats(fills, now_ms, coin=None) -> dict:
    """
    Full hold/cadence profile. Median closed-hold and median open-age are reported
    SEPARATELY so a buy-and-hold trader (long open ages, few/no closes) is no longer
    indistinguishable from "no data".

    Keys (None where there is nothing to compute):
      med_closed_h, mean_closed_h, n_closed
      med_open_h, max_open_h, n_open
      pct_intraday  — fraction of CLOSED episodes < 1h   (scalper tell)
      pct_multiday  — fraction of CLOSED episodes >= 24h (swing tell)
    """
    closed, open_ = episode_holds(fills, now_ms, coin)
    return {
        "med_closed_h":  median(closed),
        "mean_closed_h": (sum(closed) / len(closed)) if closed else None,
        "n_closed":      len(closed),
        "med_open_h":    median(open_),
        "max_open_h":    max(open_) if open_ else None,
        "n_open":        len(open_),
        "pct_intraday":  (sum(1 for h in closed if h < 1) / len(closed)) if closed else None,
        "pct_multiday":  (sum(1 for h in closed if h >= 24) / len(closed)) if closed else None,
    }


def legacy_avg_hold(stats: dict, sentinel: float = 99.0) -> float:
    """
    Back-compat single hold number for the scanners' existing scores/filters.
    Prefer the mean of CLOSED episodes; if the trader has only OPEN positions
    (a pure holder with nothing closed in history), use the median open age so
    they aren't sentinel-flagged as "no data"; else the sentinel.
    """
    if stats["mean_closed_h"] is not None:
        return stats["mean_closed_h"]
    if stats["med_open_h"] is not None:
        return stats["med_open_h"]
    return sentinel


def fmt_hold(stats: dict) -> str:
    """Compact 'closed | open' hold string for tables, e.g. '6.2h | 13d open'."""
    def _h(x):
        if x is None:
            return "—"
        return f"{x/24:.1f}d" if x >= 48 else f"{x:.1f}h"
    parts = []
    if stats["n_closed"]:
        parts.append(_h(stats["med_closed_h"]))
    else:
        parts.append("?")                       # genuinely no closed episodes
    if stats["n_open"]:
        parts.append(f"{_h(stats['med_open_h'])} open")
    return " | ".join(parts)


def fmt_hold_short(stats: dict) -> str:
    """Terse single-token hold for fixed-width tables. A trailing '*' means the
    median reflects a still-OPEN position (no closed episodes). '?' = no data."""
    def _h(x):
        return f"{x/24:.0f}d" if x >= 48 else f"{x:.1f}h"
    if stats["n_closed"]:
        return _h(stats["med_closed_h"])
    if stats["n_open"]:
        return _h(stats["med_open_h"]) + "*"
    return "?"
