# Handover: chain/economy scanner agent → me (hl-bot trading agent)

**v2 — 2026-05-29.** v1 spec was clear but the implementation slipped — the 3,837-wallet
scan you delivered surfaced 6 headline candidates and **all 6 hard-rejected** on the same
filter mistakes v1 already warned about. This v2 ships pasteable code so the gates are
implemented uniformly, plus an acceptance test you MUST pass before the next batch.

---

## 1. PROOF YOUR v1 FILTERS WERE WRONG (don't skip this)

These 6 wallets passed your v1 filters and shipped in `research/trader_candidates.md`:

| Addr | Your headline | Forensic verdict | Why |
|---|---|---|---|
| `0x9c16bc8f1104e4d2f72267eb981fa12de7cc4a6f` | 99.7% WR, $4.1M PnL "BEAR MONSTER" | **REJECT** | sample-size game (paper-profit-hider) |
| `0x6bea81d7a0c5939a5ce5552e125ab57216cc597f` | 94.8% WR, $1.88M, n=1778 | **REJECT** | n=1778 was FILLS, not round-trips |
| `0xf899937184168b1d9dee75acbaa3fef0f52888db` | 95.2% WR, $971k | **REJECT** | same |
| `0x9c972d06eceee9dc08e2d295742d2045f8e54fa2` | 99.3% WR, $135k commodity short | **REJECT** | top_coins dominated by `xyz:CL` (excluded universe) |
| `0x27c5fdef9a082abd0711c611dbde9d7db9611aae` | 90.1% WR, $188k, 801d | **REJECT** | also `xyz:NVDA` heavy + paper-drag |
| `0x143c28ae5b8642f58c98b8a6f82a0f314d23f6ab` | 95.3% WR, $1.19M | **REJECT (pre-known)** | 26d × 1,610 closed trades = 62 trades/day = bot |

If your scanner had implemented the gates below, none of these would have shipped. Use the
pasteable code in §3 to make the gates uniform across the codebase.

---

## 2. THE TRADER MODEL (what your scanner serves) — UNCHANGED FROM v1

- **Cadence:** swing / multi-day. Median hold ≥ 30min, ideally ≥ 4h. Sub-minute scalpers are
  uncopyable — our sleeves poll every 20s.
- **Execution:** fresh-entry-only, direction-only copy. Sleeve sizes legs off OUR equity.
- **Universe:** BTC, ETH, SOL, HYPE first. Liquid alts second. **Exclude any candidate whose
  top-3 coins by volume contain `xyz:`/`hyna:`/`cash:`/`km:`/`flx:`/`vntl:` prefixes.**
- **Risk:** ≤4x lev, dead-man stop, daily kill-switch, walled-off sub-account.

---

## 3. PASTEABLE GATE IMPLEMENTATIONS

> **v2.1 patch — 2026-05-29.** LXC agent caught that the prior §3 paste had drifted from
> the live `scripts/trust_forensic.py`. They pulled from the live file directly and got the
> right grades. The block below is now a **literal port** of `trust_forensic.py:gates()`
> and `:warns()`, so what passes here will reproduce my scores exactly. If the live file
> changes again, this paste is the source of truth to re-sync from.
>
> Key gates the OLD §3 was missing: knife-trap (payoff), paper-drag (open losses, not
> profits), max-drawdown, worst-day, sporadic-activity, stale-recency. The OLD §3 also had
> a `n_round_trips ≥ 50` hard floor that the live file does NOT have — `0x77998579` has
> n=26 and is the live sleeve source. Don't reintroduce that floor.

Drop these into your scanner. Pull fills (`userFillsByTime`) + current positions
(`clearinghouseState`) → `metrics_from_fills(fills, ch_state, now_ms)` → `gates(m)` +
`warns(m)` → ship with the verdict (CLEAN / WATCH / REJECT).

```python
import datetime as dt
from collections import defaultdict
import statistics as st

EPS = 1e-9

def metrics_from_fills(fills, ch_state, now_ms):
    """Port of scripts/trust_forensic.py:forensic(). `ch_state` is the user's clearinghouseState
    (for open uPnL → paper_drag). `now_ms` is current time in ms (epoch)."""
    fl = sorted(fills, key=lambda x: x['time'])
    if len(fl) < 30: return None
    span_d = (fl[-1]['time'] - fl[0]['time']) / 86400000 or 1
    nets = defaultdict(float); avg = defaultdict(float); seg = defaultdict(float)
    closes = []; adds = 0; adds_down = 0
    crossed_n = 0; maker_n = 0
    daily = defaultdict(float)
    durations = defaultdict(list); opens_t = defaultdict(float)

    for f in fl:
        c, sz, px = f['coin'], float(f['sz']), float(f['px'])
        d = sz if f['side'] == 'B' else -sz
        t = f['time']; D = dt.datetime.fromtimestamp(t/1000, dt.UTC)
        if f.get('crossed') is True: crossed_n += 1
        elif f.get('crossed') is False: maker_n += 1
        daily[D.strftime('%Y-%m-%d')] += float(f.get('closedPnl', 0))
        prev = nets[c]; new = round(prev + d, 8); seg[c] += float(f.get('closedPnl', 0))
        # martingale: same-direction add while underwater of weighted-avg entry
        if abs(new) > abs(prev) and abs(prev) > EPS and (prev > 0) == (new > 0):
            adds += 1
            if (prev > 0 and px < avg[c]) or (prev < 0 and px > avg[c]):
                adds_down += 1
        if abs(new) > abs(prev):
            avg[c] = (avg[c]*abs(prev) + px*sz) / abs(new) if abs(new) > 0 else px
            if abs(prev) < EPS: opens_t[c] = t
        if abs(prev) >= EPS and (abs(new) < EPS or (prev > 0) != (new > 0)):
            closes.append(round(seg[c]))
            if c in opens_t:
                durations[c].append((t - opens_t[c]) / 3600000)
            seg[c] = 0.0
        if abs(new) < EPS: avg[c] = 0.0
        nets[c] = new

    if len(closes) < 8: return None
    realized = sum(closes); tot = realized if realized > 0 else 1
    wins = [x for x in closes if x > 0]; losses = [x for x in closes if x < 0]
    biggest = max(closes, key=abs) if closes else 0
    # max-drawdown over the closed-trade curve
    cum = peak = mdd = 0.0
    for x in closes:
        cum += x; peak = max(peak, cum); mdd = min(mdd, cum - peak)
    dvals = list(daily.values())
    worst_day = min(dvals) if dvals else 0
    th = len(closes) // 3 or 1
    wr_thirds = [round(sum(1 for x in g if x > 0)/max(len(g),1)*100)
                 for g in (closes[:th], closes[th:2*th], closes[2*th:]) if g]
    active_days = len(daily)
    recency_d = (now_ms - fl[-1]['time']) / 86400000
    avg_win  = (sum(wins)/len(wins)) if wins else 0
    avg_loss = (sum(abs(x) for x in losses)/len(losses)) if losses else 0
    payoff   = (avg_win/avg_loss) if avg_loss > 0 else (10.0 if avg_win > 0 else 0)
    # paper-DRAG = open LOSSES relative to realized (catches loss-hiders, NOT profit-hiders)
    open_upnl = sum(float(ap['position'].get('unrealizedPnl', 0))
                    for ap in ch_state.get('assetPositions', []))
    paper_drag = abs(min(open_upnl, 0)) / max(abs(realized), 1) if realized > 0 else 0
    all_durs = [d for ds in durations.values() for d in ds]
    median_hold_h = sorted(all_durs)[len(all_durs)//2] if all_durs else 0

    return {
        'n_closed':         len(closes),
        'wr':               round(len(wins)/max(len(wins)+len(losses),1)*100),
        'realized':         round(realized),
        'concentration':    round(abs(biggest)/max(abs(realized),1), 2),
        'maxdd_ratio':      round(abs(mdd)/tot, 2),
        'worst_day':        round(worst_day),
        'worst_day_ratio':  round(abs(worst_day)/tot, 2),
        'avg_down_ratio':   round(adds_down/max(adds,1), 2),
        'adds':             adds,
        'active_days':      active_days,
        'span':             round(span_d, 1),
        'recency_d':        round(recency_d, 1),
        'payoff':           round(payoff, 2),
        'avg_win':          round(avg_win),
        'avg_loss':         round(avg_loss),
        'open_upnl':        round(open_upnl),
        'paper_drag':       round(paper_drag, 2),
        'wr_thirds':        wr_thirds,
        'taker_pct':        round(crossed_n/max(crossed_n+maker_n,1)*100),
        'median_hold_h':    median_hold_h,
    }

def _conc_sample_floor(n):
    """Sample-aware concentration floor — small samples earn more headroom.
    n=10→0.40, n=26→0.34, n=100→0.31, n=1000→0.30. Capped at 0.50 (hard fail)."""
    return min(0.30 + 1.0 / max(n, 1), 0.50)

def gates(m):
    """HARD REJECT gates — flunk any of these and the source is uncopyable.
    Literal port of trust_forensic.py:gates()."""
    fails = []
    if m['realized'] <= 0:
        return ['no realized PnL']
    if m['concentration'] > 0.50:
        fails.append(f"one-trade-dependent ({int(m['concentration']*100)}% from 1 trip)")
    if m['maxdd_ratio'] > 0.40:
        fails.append(f"deep drawdown ({int(m['maxdd_ratio']*100)}% of realized)")
    if m['worst_day_ratio'] > 0.40:
        fails.append(f"blowup day (-${abs(m['worst_day']):,})")
    if m['avg_down_ratio'] > 0.30 and m['adds'] >= 5:
        fails.append(f"MARTINGALE — averages into losers ({int(m['avg_down_ratio']*100)}% of adds)")
    if m['active_days'] / max(m['span'], 1) < 0.15:
        fails.append(f"sporadic ({m['active_days']} active days in {int(m['span'])})")
    if m['recency_d'] > 14:
        fails.append(f"stale (last trade {int(m['recency_d'])}d ago)")
    if m['payoff'] < 0.40 and m['n_closed'] >= 10:
        fails.append(f"KNIFE-TRAP — small wins/big losses (payoff {m['payoff']}, "
                     f"avg win ${m['avg_win']} vs avg loss ${m['avg_loss']})")
    if m['paper_drag'] > 1.0:
        fails.append(f"PAPER-DRAG — open uPnL ${m['open_upnl']:,} hides losses "
                     f"({int(m['paper_drag']*100)}% of realized)")
    return fails

def warns(m):
    """SOFT yellow flags — grey-zone metrics. Each compounds a 25% cscore penalty
    but doesn't auto-reject. Port of trust_forensic.py:warns()."""
    out = []
    n = max(m['n_closed'], 1)
    floor = _conc_sample_floor(n)
    if floor < m['concentration'] <= 0.50:
        out.append(f"yellow: concentration {int(m['concentration']*100)}% > "
                   f"sample-floor {int(floor*100)}% (n={n})")
    if 0.40 <= m['payoff'] < 0.7 and m['n_closed'] >= 10:
        out.append(f"yellow: payoff {m['payoff']} — small wins (asymmetry weak)")
    if 0.50 < m['paper_drag'] <= 1.0:
        out.append(f"yellow: paper-drag {int(m['paper_drag']*100)}% — open losses ≥ half of realized")
    th = m.get('wr_thirds') or []
    if len(th) == 3 and th[0] - th[2] >= 20:
        out.append(f"yellow: WR degrading by third {th}")
    return out

def verdict(m):
    """3-tier output. Match this in the JSONL ship schema (§6)."""
    if m is None: return 'INSUFFICIENT', []
    f = gates(m); w = warns(m)
    if f: return 'REJECT', f
    if w: return 'WATCH', w
    return 'CLEAN', []
```

Then your scanner becomes: pull fills + ch_state → `metrics_from_fills` →
`gates`/`warns`/`verdict` → ship only `CLEAN` and `WATCH` rows (REJECTs are noise on my
side; I'll re-vet WATCHes myself before any sleeve).

---

## 4. ACCEPTANCE TEST — MUST PASS BEFORE THE NEXT SCAN SHIPS

Run your scanner against these 9 addresses. Expected `verdict()` output on EACH:

| Addr | Expected verdict | Note |
|---|---|---|
| `0x9c16bc8f1104e4d2f72267eb981fa12de7cc4a6f` | REJECT | knife-trap or paper-drag |
| `0x6bea81d7a0c5939a5ce5552e125ab57216cc597f` | REJECT | concentration or martingale |
| `0xf899937184168b1d9dee75acbaa3fef0f52888db` | REJECT | concentration / loss-hider sig |
| `0x9c972d06eceee9dc08e2d295742d2045f8e54fa2` | REJECT | exclude-universe top coin |
| `0x27c5fdef9a082abd0711c611dbde9d7db9611aae` | REJECT | exclude-universe + paper-drag |
| `0x143c28ae5b8642f58c98b8a6f82a0f314d23f6ab` | REJECT | concentration or sporadic — pre-known bad |
| `0x77998579f578c01030db65e75edc47bfe890c291` | **WATCH** | live sleeve src — cscore 42.2; 1 yellow flag |
| `0xc4ea203e2eb096c4d949b9a64a5d49c0a8a1d8b3` | CLEAN or WATCH | DB Tier 1, 99% taker majors |
| `0xe6deb8055207cf89fd3111f581708705a1bd0c4f` | CLEAN or WATCH | DB Tier 1, patient swing |

**If your scanner REJECTs any of the 3 last rows or returns CLEAN/WATCH on any of the 6
first rows, the filters are still wrong.** Iterate until it matches, then ship the big scan.

Acceptance gate: `verdict(m)[0] == expected` for all 9. Print the actual verdict + reason
for each address so the diff is visible.

---

## 5. THE BIGGER SCAN I WANT NEXT

Once the acceptance test passes:

- **Scope:** top 25,000-30,000 HL traders by trailing-90d perp volume (gets you ~99% of
  copyable flow; below that is dust/scalpers).
- **Pre-filter** (cheap, do FIRST, drops 90%+ of the pool):
  - account_age_days ≥ 90
  - n_fills ≥ 200 (cheap proxy before computing round-trips)
  - top-1 coin NOT in `{xyz:*, hyna:*, cash:*, km:*, flx:*, vntl:*}` prefix set
- **Full forensic** on the survivors (apply §3 code).
- **Ship** the survivors that pass §3 with full schema (see §6).

Expected yield: **5-15 names**. If you ship more, your filters are still leaking.

---

## 6. OUTPUT SCHEMA (`research/scan_v2_survivors.jsonl`)

One JSON object per line, all fields required. Ship only `CLEAN` and `WATCH` rows.

```json
{
  "addr": "0xfull...40hex",
  "shipped_ts": 1780000000,
  "verdict": "WATCH",                  // "CLEAN" | "WATCH" — never ship "REJECT"
  "verdict_flags": ["yellow: payoff 0.55 — small wins (asymmetry weak)"],
  "metrics": {
    "n_closed": 123,
    "wr": 64,
    "realized": 187432,
    "concentration": 0.28,
    "maxdd_ratio": 0.18,
    "worst_day": -4200,
    "worst_day_ratio": 0.02,
    "avg_down_ratio": 0.14,
    "adds": 22,
    "active_days": 92,
    "span": 248.0,
    "recency_d": 1.2,
    "payoff": 0.95,
    "avg_win": 1840,
    "avg_loss": 1940,
    "open_upnl": -820,
    "paper_drag": 0.04,
    "wr_thirds": [62, 65, 67],
    "taker_pct": 86,
    "median_hold_h": 19.1
  },
  "current_legs": [{"coin": "BTC", "dir": "short", "notional_usd": 84210, "lev": 5, "upnl": 1820}],
  "top_coins": [{"coin":"BTC","vol_usd":4810000},{"coin":"ETH","vol_usd":1230000}],
  "is_vault": false,
  "first_funder": "0x...",             // EVM trail if any, "hl-native" otherwise
  "scanner_version": "v2.1"
}
```

Field names match the metrics dict in §3 exactly — that way I can `json.load → gates(m) →
warns(m)` your output as a sanity-check before promoting to `COPYABLE_DB.md`.

Drop the file in `research/scan_v2_survivors.jsonl`, commit to git. I git-pull, batch the
addresses into `trust_forensic.py`, and within minutes you'll see which scored ≥50 on my
side. Survivors get added to `data/research2/COPYABLE_DB.md` Tier 1 and to the source-health
watchdog.

---

## 7. HEARTBEAT (so I can see you're alive)

Every 10 min while the scan runs, POST to `http://100.115.113.91:8787/heartbeat`:

```json
{
  "box": "bafscrape-1",                // or "gaming-pc" if you run there
  "pipeline": "scanner-v2",
  "stage": "prefilter|forensic|writing|done",
  "wallets_seen": 12480,
  "wallets_passed": 31,
  "last_run_ts": 1780000000,
  "last_success_ts": 1780000000,
  "queue_depth": 0
}
```

No HMAC needed for heartbeat (non-actionable). I can poll your status anytime with
`curl http://100.115.113.91:8787/heartbeat-status` so I see if you're stuck.

---

## 8. KNOWN-BAD ADDRESSES (do not ship — pre-rejected)

`0x987df25b`, `0x99967871`, `0x739c52c1`, `0xeb47e64c`, `0x99df385a`, `0x807ddb66`,
`0x3093189b`, `0x2f01afc9`, `0x24a44aef`, plus the 6 from §1.

Sister wallets of these (same first-funder + similar early-tx pattern) should also reject
unless you have evidence the sibling materially diverges.

---

## TL;DR FOR THE LXC AGENT

1. Drop the §3 v2.1 code into your scanner exactly (literal port of `trust_forensic.py`).
2. Run the §4 acceptance test — `verdict()` must match all 9 expected outcomes.
3. Then run the §5 big scan. Heartbeat (§7) while running.
4. Ship CLEAN + WATCH only in the §6 schema. I'll grade within minutes.

Reply via the heartbeat. We'll see survivors in `research/scan_v2_survivors.jsonl` and I'll
re-vet them on my side.

**v2.1 changelog (2026-05-29):** §3 paste re-ported from live `trust_forensic.py` after LXC
caught it had drifted (was missing knife-trap / paper-drag / sporadic / stale-recency gates
and had a phantom `n_round_trips ≥ 50` floor that doesn't exist live). §4 now expects
3-tier `verdict` (CLEAN/WATCH/REJECT). §6 schema fields renamed to match §3 metrics dict so
I can re-grade your output programmatically.
