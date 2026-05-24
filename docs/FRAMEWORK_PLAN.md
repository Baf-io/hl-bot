# Framework Plan — "faster in-and-out" alpha sleeve

> Status: PROPOSAL (not implemented). Core copy engine is the proven base; this
> adds a separate, ring-fenced fast sleeve. Decision points marked **[DECIDE]**.

## Finding (data-driven, 2026-05-24)
The 5 copied traders are **slow macro holders**:
- Hold-time analysis: for 4 of 5, positions never close within the observable
  ~2000-fill window — they hold the same net position the whole time. Current
  positions sit on large unrealized gains (a9b95f HYPE +$11M).
- They TWAP heavily *within* a held position (60–285 fills/episode) but the
  position itself is multi-day. Only a4dedd churns intraday (LIT, ~410 fills/day).
- We already proved that reacting to their fills = fee death (238 fills/48h,
  fees > gross loss). State-based copy fixed that by holding.

**Conclusion:** "Faster in-and-out" CANNOT come from copying these traders.
Their edge *is* the slow hold. Faster trading needs a **separate alpha source**.

## The hard constraint: fees
Taker round-trip ≈ **0.09%** (0.045% × 2). On a ~$1.1k account, a fast sleeve
must clear that *every* trade. Implications:
- **Maker (post-only limit) entries** to cut/zero the fee, or
- only act when expected move ≫ fee (R:R > ~3:1).
Without this, fast trading is a guaranteed bleed. This is non-negotiable.

## Architecture: two capital-segregated sleeves
- **Core (copy)** — ~70% equity. Current state-based copier: slow, holds,
  trailing-stop profit lock, autocompound. Proven; leave it alone.
- **Tactical (fast)** — ~30% equity, **ring-fenced**, own signals, hard
  sleeve-level daily-loss halt so it can never bleed the core.

### Isolation: separate HL sub-account **[DECIDE]**
Core already holds HYPE long (copied). On ONE HL account, a tactical HYPE trade
nets/conflicts with that (one-per-coin rule blocks it). Cleanest fix: run the
tactical sleeve on a **separate Hyperliquid sub-account** — own collateral, own
risk, zero netting conflict with the copy book. Recommended.

## HYPE alpha sleeve (user pick)
HYPE is liquid, volatile, and all 3 generalist copy-traders are long it (strong
directional bias to lean on). Candidate signals (start with ONE):
1. **Momentum/breakout** — `momentum_ignition`/`cascade` modules already exist
   (dormant). Enter on impulse, exit on tight trailing stop.
2. **Mean-reversion** on intraday extremes vs VWAP/bands.
Bias filter: only take LONGs while the copy cohort is net-long HYPE (don't fight
the macro), unless explicitly running both directions.

## Diversify vs tunnel — recommendation
Do **NOT** full-tunnel into fast on a small account (high variance, fee-sensitive).
**Diversify** with a small ring-fenced HYPE sleeve, and **validate before risking
capital.**

## Phased rollout
- **Phase 0 (done):** core copy solid — state-based, trail, compound. ✓
- **Phase 1 [recommended next]:** build the HYPE momentum signal in **shadow
  mode** — log every would-be entry/exit and its hypothetical PnL *net of fees*
  for ~3–5 days. No capital at risk. Measure: hit rate, avg R, net edge/day.
- **Phase 2:** if shadow shows positive net-of-fee edge, enable live on a
  separate sub-account with ~$200–300, maker entries, max N trades/day, and a
  hard sleeve daily-loss halt (e.g. −5%).
- **Phase 3:** scale only if it holds up live.

## Risk controls for the fast sleeve
- Separate capital envelope (sub-account); core never funds the sleeve's losses.
- Maker-only or move-threshold entries (fee discipline).
- Max trades/day cap (anti-overtrade — the original failure mode).
- Sleeve-level daily-loss halt, independent of the global one.
- No silent overlap with core-held coins (sub-account solves this).
