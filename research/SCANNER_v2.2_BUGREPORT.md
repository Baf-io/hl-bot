# Scanner v2.2 — feedback on the 44-survivor batch

**Reported:** 2026-05-30
**Batch:** `research/scan_v2_2_survivors.jsonl` (44 rows, scanner_version `v2.3`)
**Severity:** medium — calibration is solid (no false positives this time), but three
schema-contract issues need fixing before v2.3 batch.

## What you got right (don't break these)

- **CScore calibration is perfect.** Live `trust_forensic.py` reproduced your shipped
  cscores **EXACTLY** on the top 11 spot-checks (80.9, 73.7, 68.7, 65.0, 56.9, 55.1,
  54.6, 52.9, 51.8, 51.6, 50.8). No `0x0252f92e`-style false positives this batch.
  The v2.1 BUGREPORT patches landed correctly.
- **Cohort block present on all 44.** Schema is intact, partner lists structured well.
- **Cohort discovery: 0xbbf82c80 ↔ 0x9f3e77cb** — 24 co-opens on HYPE at 100% same
  direction. This is the strongest cohort signal in the batch and validates that the
  §7 analysis is finding real structure when it exists. Likely the same operator on
  two wallets — entity-graph (gaming-PC ETL) will confirm.

## Issues to fix in v2.3

### 1. `tier_eligible_for_solo_sleeve` is `null` on all 44 rows

The v2.3 schema spec (§6) requires this boolean field, computed per these rules:

```python
LIQUID_MAJORS = {"BTC","ETH","SOL","HYPE","XRP","DOGE","SUI","BNB","AVAX","LINK","LTC"}

def tier_eligible_for_solo_sleeve(tier, top_3_coins, payoff):
    in_majors = all(c in LIQUID_MAJORS for c in top_3_coins)
    if tier in ("swing","intraday"): return True
    if tier == "scalp":              return in_majors and payoff >= 0.7
    if tier == "ultra-scalp":        return in_majors and payoff >= 1.0
    return False
```

I'm computing this myself on receive, but the schema contract needs to ship the field.

### 2. Tier classification has a cutoff bug

Row `0xbbf82c801652...` has `median_hold_h: 0.03` (1.8 minutes) labeled `tier: "scalp"`.
Per the v2.3 §6 spec:

| `median_hold_h` range | Correct tier |
|---|---|
| ≥ 4 | `swing` |
| 0.5 ≤ h < 4 | `intraday` |
| **0.083 ≤ h < 0.5** (5-30min) | `scalp` |
| **0.0167 ≤ h < 0.083** (1-5min) | **`ultra-scalp`** ← this row should be here |
| < 0.0167 (<1min) | DON'T SHIP |

`0.03h = 1.8min` falls in the **ultra-scalp** band, not scalp. Likely the classifier
collapses both bands into "scalp". This affects ~1-2 rows in the current batch but
will affect more as the v2.3 batch ships more sub-5min names.

### 3. Cohort partner counts are very sparse — only ONE meaningful pair

23 of 44 rows have a top partner with `co_opens_n ≥ 2`; only **1 pair** has
`co_opens_n ≥ 10` (the bbf82c80/9f3e77cb pair). For cohort-vote-3-of-N to be useful,
we need more sources with non-trivial overlap. Suspects:

- **60s window too tight.** Most non-coordinated traders won't agree within 60s even
  if they share a thesis. Consider also reporting a `co_active_900s_pct` (15min window)
  — that's the natural human-cohort timescale for "saw the same signal."
- **History window too short.** If you only scanned the last 30d for co-activity,
  thinly-active sources won't have enough overlap. Try 90d for the cohort pass even
  if the forensic pass uses 30d.
- **Fresh-open definition too narrow.** Reminder: a fresh open is flat→non-zero OR
  flip. If you're only counting flips (which are rare), most opens won't qualify.
  Flat→open should be the bulk of the count.

If you ship v2.3 with sparse cohorts, the cohort-vote strategy can't fire on more than
the one pair we already found. Tightening this is the highest-impact fix.

### 4. Tier-shape distribution skewed swing-heavy

Got 37 swing / 5 intraday / 0 scalp / 1 ultra-scalp / 1 sub-1min-drop. The whole point
of v2.3 was unlocking the scalp/ultra-scalp band — getting only 1 ultra-scalp suggests
the pre-filter is still culling fast-cadence candidates. Check the pre-filter:

- Does `n_fills ≥ 200` cut out scalpers with shorter histories? Lower to 100.
- Does the `account_age_days ≥ 90` filter cut out recent-but-active scalpers? OK to
  keep but worth noting in the heartbeat.
- Are you applying the OLD `median_hold_h ≥ 0.5` floor anywhere in the pipeline as a
  vestigial check? Grep for it and remove.

Expected v2.3 tier-shape: roughly 50% swing / 25% intraday / 15% scalp / 10% ultra-scalp.
If v2.3 ships <5 names in scalp+ultra-scalp combined, the pre-filter is still wrong.

## What I'm doing with this batch on my side

- Added 6 fresh names from this batch to `scripts/shadow_candidates.py` (paper-only):
  bbf82c80, 9f3e77cb, 5d9d19a3, b19e0376, f2704e08, 1169a721. Shadow now 18 candidates.
- Skipped 0xd708cf759c (ZEC top coin, illiquid-major concern).
- Held the rest pending v2.3 batch.
- `watch_cohort_vote.py` design is blocked on more cohort partners with n ≥ 10 —
  fixing #3 above unblocks it.

Heartbeat me when v2.3 patched + ready to scan. The acceptance test (§4 of the main
handover) still gates the next batch.
