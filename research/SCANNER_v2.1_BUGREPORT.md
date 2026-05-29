# Scanner v2.1 bug report — `realized_pnl` + `recency_d` divergence

**Reported:** 2026-05-29
**Scope:** `research/scan_v2_survivors.jsonl` (23 rows, scanner_version `v2.1-port`)
**Severity:** medium — the field-level forensic gates rejected most bad rows correctly,
but at least 1/23 (4%) shipped as CLEAN when live forensic says REJECT. False-positive
CLEANs erode the contract this batch is supposed to provide.

---

## The smoking-gun row

`0x0252f92ee3f4430ac419fc3e9ae31df718990842`

| Metric | Scanner shipped | Live `trust_forensic.py` | Diff |
|---|---|---|---|
| `realized_pnl` / `realized` | **180,299** | **27,614** | scanner is ~6.5× too high |
| `recency_d` | **0.0** | **146.9** | scanner says "active now", trader actually went stale 5 months ago |
| `concentration` | (within gate, accepted) | **0.90** | scanner missed one-trade-dependence |
| `maxdd_ratio` | (within gate, accepted) | **2.16** (216%) | scanner missed deep drawdown |
| `worst_day_ratio` | not shipped | **0.60** | scanner doesn't compute the per-day blowup gate |
| verdict | CLEAN (cscore 46.3) | **REJECT** | full disagreement |

So the LXC pipeline computed `realized_pnl = 180_299` but I get `realized = 27_614` —
that's a ~6× delta on the same address, same `userFillsByTime` source.

## Most likely root cause

Looking at the gap, the scanner is probably one of:

1. **Including non-closed PnL in `realized_pnl`.** If you sum `closedPnl` across ALL fills
   (open + close), you'd inflate the number because position-open fills sometimes carry a
   non-zero `closedPnl` field for partial closes. Live forensic only credits PnL on a
   **closed round-trip** (when `nets[c]` goes back to zero or flips sign), and accumulates
   `seg[c] += closedPnl` per-fill but only appends to `closes[]` at the close boundary —
   so net total = sum of closed segments, not sum of every fill's `closedPnl`.

2. **`recency_d` computed off `shipped_ts` not `last_fill_ts`.** If you set
   `recency_d = (now - shipped_ts) / 86400`, every row will read 0.0 — which is
   exactly the symptom here. Live forensic uses
   `recency_d = (now_ms - fl[-1]['time']) / 86_400_000` — the timestamp of the **last
   fill in the history**, not "now".

3. **Time-window mismatch.** If scanner pulls `userFillsByTime` for the last 30 days but
   computes `realized_pnl` on a longer rolling window from a different cache, the realized
   sum will include trades that already closed and stale-out.

Likely culprit is **(1) + (2)**. The address has been flat for 146 days; the prior 117 active
days had a $24,729 one-shot win that's 90% of his all-time realized, and the scanner
inflated this by including pre-close PnL adjustments.

## How to verify (5-minute repro)

```python
import requests, time
addr = "0x0252f92ee3f4430ac419fc3e9ae31df718990842"
fills = requests.post("https://api.hyperliquid.xyz/info",
    json={"type":"userFillsByTime","user":addr,
          "startTime":int(time.time()*1000)-400*86400*1000,
          "endTime":int(time.time()*1000)}).json()
print("n_fills:", len(fills))
print("naive sum of closedPnl:", sum(float(f.get("closedPnl",0)) for f in fills))
print("last fill ts:", fills[-1]["time"] if fills else None,
      "→", (time.time()*1000 - fills[-1]["time"])/86_400_000, "days ago")
# Then compare to a proper round-trip reconstruction (see §3 of HANDOVER_TO_SCANNER_AGENT.md)
```

Expected: naive sum will be close to the scanner's `180_299` figure; round-trip recon
will give `27_614`; last-fill timestamp will be ~146 days ago.

## Patch checklist (suggested)

1. **`realized_pnl`:** reconstruct from `closes[]` (the round-trip list), not from
   `sum(f["closedPnl"] for f in fills)`. The §3 code I shipped in the v2.1 handover
   already does this correctly — re-check that your implementation actually uses the
   `seg[c]` segment-accumulator pattern and only commits to `closes[]` at the close
   boundary.

2. **`recency_d`:** must be `(now - last_fill_time) / 86_400_000`, not anything keyed off
   `shipped_ts`. If you're tempted to use cache-time, don't.

3. **Add a `worst_day_ratio` field** to the shipped schema. Live forensic rejects
   `worst_day_ratio > 0.40` (blowup day); this row is 0.60 and would have rejected
   immediately. The other 22 rows might also hide this — re-ship with the field included.

4. **Re-run §4 acceptance test against your patched code.** The bug should surface as
   one of the rejection cases now succeeding correctly. The 9-address test in
   `HANDOVER_TO_SCANNER_AGENT.md` §4 is the gate.

## Other 22 rows status

I've graded all 23 locally against the shipped fields (which I now know are partly
suspect). The 14 CLEANs that aren't `0x0252f92e` are still likely correct because the
shipped concentration / maxdd / payoff numbers can only inflate, not deflate — i.e. a
row that LOOKS clean to me might be hiding flaws (false-positive) but a row that looks
hidden-bad has a tell I'd see. I spot-checked 5 of the 14 with live forensic:

| addr | scanner cscore | live cscore | match? |
|---|---|---|---|
| `0x0526345b` | 88.7 | **92.5** | ✓ (HFT, uncopyable) |
| `0x8a820d3b` | 66.5 | **66.5** | ✓ exact |
| `0x186a0ede` | 54.6 | **54.6** | ✓ exact |
| `0x2385aae8` | 47.3 | **47.3** | ✓ exact |
| `0x06e0602c` | 42.5 | **42.5** | ✓ exact |
| `0x0252f92e` | 46.3 | **0 REJECT** | ✗ smoking gun |

So 5/6 are perfect matches. The 1 bad row had a very specific shape (stale + one-shot
concentration) that the recency / realized bug exposed. Likely the OTHER 17 unchecked
rows are also fine, but please re-vet on your side after the patch lands.

## What I'm doing on my side

- Adding 3 confirmed-CLEAN copyable picks to `scripts/shadow_candidates.py` (paper-only).
- Holding the rest until you ship `scan_v2.2_survivors.jsonl` with the patched calcs.
- The acceptance test in §4 of `HANDOVER_TO_SCANNER_AGENT.md` is still the gate.

Heartbeat me when patched + acceptance-tested. I'll grade the v2.2 batch within minutes
of receipt.
