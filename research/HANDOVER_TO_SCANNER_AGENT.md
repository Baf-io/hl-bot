# Handover: chain/economy scanner agent → me (hl-bot trading agent)

You're building the full-pool scan/economy mapper on the gaming PC. I'm running the live
copy-trade book on the VPS. Here's what I need from you to *use* your output, and the failure
modes I've already seen.

---

## My trader's MODEL (what your scanner serves)

He copies discretionary swing traders via HL sub-account sleeves. **Optimize the scan for
THIS profile — anything else is noise to us.**

- **Cadence:** swing / multi-day holds. Median hold ≥ 30min, ideally ≥ 4h. Sub-minute scalpers
  are uncopyable (our sleeves poll every 20s; copy lag destroys their edge).
- **Execution:** fresh-entry-only. We mirror **direction**, not size. Our sleeve sizes legs
  off our own equity (`MARGIN_PCT × eq × LEV`), capped 3-4x iso. We do NOT proportional-peg.
- **Universe:** BTC, ETH, SOL, HYPE liquid majors first. Alt watchlists optional. We do NOT
  copy `xyz:` / `hyna:` / `cash:` / `km:` / `flx:` equity/commodity perps — different market
  structure, thin books, often illiquid for our exits.
- **Risk:** dead-man 8-12% reduce-only stop, daily kill-switch, sub-account walled off.

## HARD GATES (auto-reject — don't even surface these)

| Gate | Threshold | Why |
|---|---|---|
| Realized PnL | < $50k OR ≤ 0 | not enough sample to trust |
| Closed **round-trips** (NOT fills) | < 50 | thin sample = noise WR |
| Total-account taker% | < 70% | maker-heavy = uncopyable (we'd be taking liquidity) |
| Median hold | < 30 min | scalper — 20s poll lag eats edge |
| Account age | < 90 days | not regime-tested |
| Martingale rate (% adds into losers) | > 25% | will average-into a blowup eventually |
| Concentration (biggest trip / realized) | > 30% | one-trade-dependent |
| Max DD / realized | > 40% | will draw down again |
| Worst day / realized | > 25% | one blowup day risk |
| WR (closed round-trips only) | ≥ 99% | almost always loss-hider OR profit-hider |
| Top-coin set | dominated by `xyz:`/`hyna:`/`@N`/`cash:`/`km:`/`flx:`/`vntl:` | wrong market |
| Vault? | yes | redundant w/ depositor track; we already cover vaults separately |

## ⚠️ TRAPS THE LAST HANDOFF FELL INTO — DO NOT REPEAT

The 2026-05-28 `research/trader_candidates.md` handoff had these defects; both standouts
(`0x2f01afc9`, `0x24a44aef`) failed my forensic for these reasons:

1. **`taker%` was DOMINANT-LEG taker, not total-account.** A pure-short specialist who
   shorts with takers and longs with makers passed the 90% filter despite being 16% taker
   overall. Use **total-account taker fraction** (all fills, all coins, all directions).
2. **`n` was TOTAL FILLS, not CLOSED round-trips.** A bot that adds/trims 100× per position
   shows n=2000 but has 19 actual round-trips. Use the **round-trip count** (each transition
   from flat→pos→flat counts as one).
3. **"paper-loss check" caught loss-hiders but not PROFIT-hiders.** Same distortion in reverse:
   one big open paper-WIN inflates a tiny realized track. Add a **paper-PROFIT gate**: flag
   if unrealized > 0.5× realized.
4. **Headline WR was on the dominant side only, not all closed trips.** A SHORT specialist
   with one bad long got "99.6% WR" from his short book and a hidden disaster long.
5. **Martingale rate was not checked.** 55% avg-into-losers passed every other filter. This
   is the SINGLE most predictive failure signal — add it as a hard gate.

## REQUIRED OUTPUT FIELDS (per candidate, all rows must include)

```
addr                    # full 0x...40-char address (no truncation)
direction               # "long" | "short" | "two_sided"
realized_pnl            # CLOSED-trip realized only, USD
unrealized_pnl          # current open uPnL (separate)
paper_profit_ratio      # unrealized / realized — flag if > 0.5
n_round_trips           # NOT fills count
wr_closed               # win rate on closed round-trips, ALL coins
wr_by_third             # [t1, t2, t3] for trajectory (improving/degrading)
concentration_pct       # biggest_trip / realized
martingale_rate         # adds_into_losers / total_adds
maxdd_vs_realized       # max running drawdown / realized
worst_day_vs_realized   # worst single-day loss / realized
total_taker_pct         # account-wide, not per-leg
median_hold_h           # closed round-trips, median hours
account_age_days        # span from first fill
top_coins               # 5 most-traded by notional, full coin name not truncated
current_legs            # [{coin, dir, notional_usd, lev, upnl}, ...]
is_vault                # bool
first_funder_label      # CEX/fund/protocol if EVM trail traceable
```

## OUTPUT DESTINATION

Append your scan survivors to: `data/research2/COPYABLE_DB.md` (Tier 1 table) — same column
shape we already use. Don't create competing files. Existing entries are the current ground
truth; if you find a candidate already there, note in a `Tier-3 RE-VET` section, don't
duplicate.

## WHAT'S ALREADY IN PLACE (read before scanning)

- `scripts/trust_forensic.py` — my gold-standard forensic (read this; it's the spec). Your
  scan should produce data that this can verify in seconds.
- `data/research2/COPYABLE_DB.md` — current 8 vet-passed names
- `data/research2/results.jsonl` — last full-pool pass (3,141 rows); has stale `av=0` for
  many accounts because of withdrawals. Re-query live state per finalist.
- `config/traders.json` — the 13 traders currently in the live roster (committed weighting)
- `CLAUDE.md` — top-of-file state; read the `Current state` section before reasoning about
  what's live

## DELIVERABLE TARGET

**5–10 names** that survive ALL hard gates. Top 3 with a 1-paragraph dossier (why this one,
biggest risk, what role on the live book — e.g., "fills the BTC long-side gap"). Don't
deliver 50 candidates; that's a re-scan, not a handoff.

## IF SCANNING OFF-CHAIN ECONOMY (the "complete chain" part)

- EVM funding trails: who funded the wallet first? CEX deposit address → tag retail. Fund
  multisig → flag opaque. Same-tx fanout → flag farming/sybil cluster.
- Sister wallets: same first-funder + similar early-tx pattern = same entity → de-dup before
  scoring (otherwise the same operator shows up 3× as different "candidates").
- Vault depositors: a wallet that ONLY deposits to vaults is not a trader; exclude from copy
  candidate ranking.
- Nansen / Etherscan tagging: if you have keys, label "Fund," "Smart Money," "Whale"
  segments. Note: HL-native wallets won't have EVM trails.

## TWO-WAY CHANNEL

Drop your survivor list as appendix in `data/research2/COPYABLE_DB.md`. I'll re-run my
forensic on your top 3 within minutes. If any score ≥ 50, candidate for live sleeve
deployment.

Sister-wallet aliases of names I already rejected (`0x987df25b`, `0x99967871`, `0x739c52c1`,
`0xeb47e64c`, `0x99df385a`, `0x807ddb66`, `0x3093189b`, `0x2f01afc9`, `0x24a44aef`): also
reject by association unless you have evidence the sibling has materially different behavior.
