# Entry/Exit Overhaul — Proposal (DRAFT, not yet implemented)

**Status:** awaiting sign-off. Nothing in here is live. Last-known-good rollback: `ea72576`.
**Author context:** drafted 2026-05-25 after the live audit in `docs/POSITION_THESIS.md`.

---

## 0. What we're fixing (from the data, not theory)

Realized PnL = **−$133** over the first ~36h. Where it actually went:

| cause | $ impact | root |
|---|---|---|
| RESIZE close-and-reopen | TON −$50, SOL −$21, ZEC −$8 = **−$79** | reconcile closes the *full* position to re-open at a new size → locks the running loss |
| Tiny alt churn (ONDO/NEAR/WLD/FARTCOIN…) | ~−$17 | fleeting low-conviction copies, fee-dominated |
| Fees | **−$36** (27% of total) | trade count too high |
| Trader-driven (LIT drop, old BTC short) | ~−$31 | legitimate |
| **Stop-loss** | **$0 / fired 0 times** | the `max(margin, ATR)` bug = effective stop −47% to −62% of margin |

**Conclusion: the loss is mostly self-inflicted churn + a stop that never fired — not bad trader calls.** This plan attacks churn + installs a real stop. Entries (routing, conviction gate, equal-weight) are sound and largely unchanged.

---

## 1. Design principles
1. **Deterministic dollar risk per trade** — expressed as % of *margin* (margin is what we staked, so % of margin = fixed dollars). This is the correct frame; "% of notional" would make high-lev losses bigger.
2. **Volatility-aware** — a fixed dollar stop must not sit inside noise; we tune *leverage* to the coin's ATR so the stop lands at a real move, not a wiggle.
3. **Minimise churn & fees** — never close a position just to resize; debounce fleeting copies; cooldown after a stop.
4. **Ride vs bank is a conscious, per-sleeve choice** — copy core banks some (you asked for "take profits sooner"); the 78aa tracker rides uncapped (his edge).

---

## 2. Proposed changes

### FIX 1 — Real stop-loss (replaces the broken ATR-floor `max()`)
**Now:** `stop_px = max(STOP_LOSS_MARGIN_PCT/lev, STOP_MIN_ATR_MULT*atr)` → the ATR term *widens* the stop to −47/−62% margin.
**New:** hard margin stop, **no widening floor**:
```
stop_px = STOP_LOSS_MARGIN_PCT / lev          # e.g. 0.20 / lev
exit full position the instant favorable-excursion <= -stop_px
```
- `STOP_LOSS_MARGIN_PCT = 0.20` → **every loser cut at −20% of its margin, deterministically.** On a ~$150 margin position that's a **−$30 hard cap** (vs the −$50+ we just ate).
- A stopped coin stays trail-locked until the trader's position resets (already implemented for stops).

### FIX 2 — Volatility-scaled leverage (so the tight stop isn't noise)
The honest tension: −20% margin at 10x on a 9%-ATR alt = a 2% price move = noise → whipsaw. **Fix it on the leverage side, not by loosening the stop:**
```
lev_cap = STOP_LOSS_MARGIN_PCT / (STOP_NOISE_ATR * atr)      # so stop distance >= STOP_NOISE_ATR ATRs
our_lev = min(trader_lev, COPY_MAX_COPY_LEVERAGE, lev_cap)
```
With `STOP_NOISE_ATR = 0.5`, the −20% stop always lands at **≥0.5 daily-ATR** (a real move). Effect on current coins:

| coin | daily ATR | leverage today | **new lev cap** | −20% stop lands at |
|---|---|---|---|---|
| BTC | 2.6% | 10 | 10 (cap) | 0.77 ATR |
| SOL | 4.2% | 10 | 9.5 | 0.5 ATR |
| ETH | ~3% | 10 | 10 (cap) | 0.67 ATR |
| HYPE | 7.8% | 10 | **5.1** | 0.5 ATR |
| ZEC | 9.1% | 1* | 1* | wide (trader runs 1x) |

\*ZEC/TON already mirror the trader's 1x. **Net effect: volatile coins get smaller, safer positions; the dollar stop is identical everywhere (−20% margin) but never sub-noise.** This is textbook vol-targeting — appropriate for a $1.2k account.

### FIX 3 — Kill RESIZE churn (biggest single win)
**Now:** reconcile closes the full position and re-opens at a new size whenever it drifts outside 0.6×–1.6× target → realizes the running loss + 2× taker fees. This caused −$79.
**New:** **never close-and-reopen for sizing.**
- Under-sized (e.g. startup-synced small): **ADD** the delta with a single reduce-only-safe `market_open` top-up — no close, no realized loss.
- Over-sized: **leave it** (riding a bit big is harmless; it'll normalize on the trader's next real change).
- Phase-1 simplest: `RESIZE_ENABLED=false` (one flag, trivial rollback) — positions ride at entry size until a real exit/flip/stop. Phase-2: add the ADD-only top-up.

### FIX 4 — Anti-churn on entries
- **Debounce:** only copy a *new* coin after the trader has held it ≥2 reconcile ticks (~90s). Kills fleeting/scalp positions (the ONDO/NEAR/WLD bleed).
- **Re-entry cooldown:** after a stop, don't re-open that coin until the trader's net position resets (already true for stops; extend to all exits).
- **Raise the conviction gate** `COPY_MIN_CONVICTION_PCT 0.03 → 0.05`: copy only genuine bets. Fewer, higher-quality positions, less fee drag. (Side effect: fc667 stays fully filtered — see thesis doc; he contributes nothing today anyway.)

### FIX 5 — Profit-taking (the cap-vs-ride fork — YOUR CALL)
Two clean options for the **copy core** (the tracker stays uncapped/ride either way):

- **Option A — Bank-and-ride (rec, matches "take profits sooner"):** stop −20% margin (−1R); **bank 50% at +40% margin (+2R)**; trail the runner at 1.0×ATR. Realizes profit on a clean 2:1, keeps a runner for trends. HYPE +20% today would start banking.
- **Option B — Pure ride:** stop −20% margin; no early bank; trail full position at 1.0×ATR once it clears +1 ATR. Lumpier, bigger tails, less frequent realization.

---

## 3. Worked example — the TON −$50 trade under new rules
- **Old:** TON short $1,327 @ 10x drifted to −38% margin; RESIZE closed it → **−$50.48 locked**, reopened at $189.
- **New:** No resize (Fix 3) → never closed for sizing. Hard stop (Fix 1) would have cut it at **−20% margin ≈ −$26** *if* it hit the stop — otherwise it rides to the trader's exit. Either path beats −$50, and the churn fee is gone.

---

## 4. New / changed parameters
| param | now | proposed |
|---|---|---|
| `STOP_LOSS_MARGIN_PCT` | 0.25 (but overridden) | **0.20 (hard, enforced)** |
| `STOP_MIN_ATR_MULT` (the floor) | 0.6 | **removed** |
| `STOP_NOISE_ATR` (new, leverage tuner) | — | **0.5** |
| `RIDE_GIVEBACK_ATR` | 1.5 | **1.0** (bank closer to peak) |
| `RESIZE_ENABLED` (new) | implicit on | **false** (phase 1) |
| `COPY_ENTRY_DEBOUNCE_TICKS` (new) | — | **2** |
| `COPY_MIN_CONVICTION_PCT` | 0.03 | **0.05** |
| profit mode | ride (no bank) | **A or B (your pick)** |

## 5. Code touch points (for implementation)
- `src/signals/leaderboard_copy.py`: `_ride_winners` (stop math), `_lev_for`/`_build_desired` (vol-scaled lev), reconcile RESIZE branch (Fix 3), entry debounce + conviction gate.
- `src/execution/executor.py`: ADD-only top-up path (phase 2 only).
- `config/settings.py`: new constants above.
- No change to the tracker (`lev_tracker.py`) — it's walled off and already rides uncapped.

## 6. Rollout & safety
- All changes behind constants → flip back in `.env`/settings instantly. Rollback commit `ea72576`.
- **Validate via `TRACKER_DRY_RUN`-style logging first:** ship with extra log lines showing computed stop_px / lev_cap per position for one cycle, eyeball them against this table, *then* enable live actions.
- Deploy = local edits + `sudo systemctl restart hl-bot` (no git pull — edits are local).

---

## 7. Decisions I need from you
1. **Stop tightness:** −20% of margin (rec) — or −15% (tighter) / −25% (looser)?
2. **Profit mode:** Option A bank-and-ride (rec) or Option B pure ride?
3. **Vol-scaled leverage (Fix 2):** yes (rec) — or keep flat 10x cap and just accept whipsaw on alts?
4. **Resize:** phase-1 disable (rec) or go straight to ADD-only top-up?
5. **Conviction gate 0.03 → 0.05?** (fewer, stronger copies)
