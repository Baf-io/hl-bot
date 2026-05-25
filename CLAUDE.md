# HL-Bot Agent Briefing

> **Agent self-maintenance rule:** After every fix, update this file:
> - Move resolved bugs from "Open issues" to "Fix log" (one line each, keep last 10)
> - Update "Current state" section to reflect what's actually deployed
> - Delete stale context — this file must stay under ~120 lines

---

## What this bot does
Hyperliquid perp trading bot. FULL ACTIVE-SWING profile: copies 3 weighted, two-sided
GENERALIST swing traders (fresh-entry-only, ride-with-trader), plus a walled-off isolated
BTC tracker (78aa). User can hand-manage coins via MANUAL_COINS. Runs 24/7 on a Linux VPS
as `hl-bot.service`.

---

## Architecture (one line per file)
| File | Role |
|---|---|
| `src/main.py` | Wiring, scheduler, guardian loop |
| `src/signals/leaderboard_copy.py` | Copy engine: STATE-BASED reconcile (polls trader net positions), specialist routing, sizing |
| `src/execution/executor.py` | Order placement, startup position sync |
| `src/risk/manager.py` | Risk gating (one-per-coin, margin/notional caps) |
| `config/settings.py` | All tunable constants |
| `config/traders.json` | Whitelisted traders + per-trader `weight` (+ optional `specialty` pin). Re-read live every 5min |

---

## Current state (update after each deploy)
- **Portfolio:** $1120 USDC (PORTFOLIO_USD=1120 in VPS .env)
- **Active traders (2026-05-25 → FULL ACTIVE-SWING pivot):** 3 weighted GENERALISTS (multi-coin, two-sided, perp-durable, deep-vetted ORGANIC): `0xf83858`(wt 1.0, 19-coin diversified), `0x41829013`(wt 0.6, multi-wallet fund — trimmed), `0x69b05701`(wt 0.4, HYPE-beta — least). Plus 78aa→BTC tracker. DROPPED the conviction-HODLERS feec88(SOL)/a9b95f(HYPE) — they never trim, incompatible w/ fresh-entry. (Earlier drops: fc667, 4f7634, a4dedd.)
- **MANUAL_COINS (now `{}` — HYPE unblocked 2026-05-25 when user closed his manual short):** coins the USER trades by hand — copier skips them (`COPIER_SKIP_COINS = TRACKER_COINS|MANUAL_COINS`, currently just `{BTC}`). Set `MANUAL_COINS=HYPE,...` to hand-manage a coin again.
- **COPY_TRADER_WHITELIST:** must be empty in .env — traders.json is the source
- **Copy model:** STATE-BASED. `reconcile()` (every `COPY_RECONCILE_INTERVAL_S=45s`) rebuilds each trader's net `clearinghouseState`, prunes phantoms vs OUR live HL state (`drop_phantoms`), builds desired portfolio (specialist routing, skip contested, highest-conviction holder), diffs vs held, mirrors net changes. NOT fill-driven. **FRESH-ENTRY-ONLY (`FRESH_ENTRY_ONLY=true`, Stage 2 / zero-copy-lag):** only OPEN on a trader's observed flat→position/flip transition AND within `FRESH_ENTRY_MAX_ATR=0.5`×ATR of their open price; NEVER adopt a position they already hold (stale adoption at a worse price was the copy-lag leak — ETH −$15.61 / SOL −3% vs his +74%). First poll after restart = baseline only; a newly-ADDED trader (roster change/live `traders.json` reload) also seeds to baseline — their existing holds are NOT treated as fresh (this bug once auto-opened an unwanted ETH short on a mid-run reload). (`_detect_fresh_opens`/`_is_fresh_entry`; legacy debounce kept for `FRESH_ENTRY_ONLY=false`.) NOTE: the running bot re-reads `traders.json` every 5 min, so roster edits go LIVE without a restart. **RESIZE close-and-reopen DISABLED (`RESIZE_ENABLED=false`)** — locked running losses + double fees (−$79 of the first −$133); positions ride at entry size until a real exit/flip/stop.
- **Sizing (weighted, fixed-per-position):** each pos = `COPY_POSITION_PCT=0.12` of the COPY BUDGET × the source trader's `weight`, capped `MAX_POSITION_SIZE_PCT=0.15`. **Copy budget = equity − tracker reserve** (`TRACKER_MARGIN_USD×|TRACKER_COINS|`) so the isolated sleeve & copy book never fight for margin. Fixed-per-position (NOT ÷n) so many-coin generalists don't shrink to dust; book bounded by `MAX_OPEN_POSITIONS` + risk caps (graceful, no wall). Gate `COPY_MIN_CONVICTION_PCT=0.05`; `MIN_POSITION_NOTIONAL=50`; `COPY_MAX_COPY_LEVERAGE=10`. **Vol-scaled leverage (`VOL_SCALED_LEV=true`):** per-coin lev ≤ `STOP_LOSS_MARGIN_PCT/(STOP_NOISE_ATR=0.5·dailyATR)`. Applies to NEW entries only.
- **BUY-WHEN-THEY-ADD (`ADD_MIRROR_ENABLED=true`, trend-gated):** when the source trader grows a coin's notional ≥`ADD_MIN_FRAC=0.10` poll-over-poll AND the daily trend (`close vs SMA(TREND_SMA_DAYS=5)`) aligns with our direction: if HELD → ADD `min(their %, ADD_STEP_MAX=0.50)`×margin (capped); if FLAT → RE-OPEN (`COPY_REOPEN_ON_ADD=true`, the "repeat" after a TP). Notifies. **TRIMS never mirrored** (their trims are early/neg-edge: adds 90–100% WR in trends, trims 0–35%). `action="add"` bypasses one-per-coin + `add_to_position` wtd-avg entry.
- **Exits = PROBABLE-TP SCALP ("sell at +1% → repeat", `_ride_winners`):** FULL exit at +`COPY_TP_PCT=0.01` favorable price (backtest: their adds hit +1% within 6h ~80% of the time — the probable roof), paired with a TIGHT full-exit SL at −`COPY_SL_PCT=0.015` (a +1% TP needs a small SL). NO trail-lock → re-enter on the trader's next add (the repeat). Supersedes the old +25% bank / ride-trail (`BANK_*`/`RIDE_*`/`SPECIALIST_STOP_ATR` now UNUSED). Guardian −70% nuclear = deep backstop.
- **Lev-tracker sleeve (`src/signals/lev_tracker.py`, `TRACKER_ENABLED`):** auto-mirrors ONE trader's DIRECTION on `TRACKER_COINS={BTC}` in ISOLATED margin (`TRACKER_MARGIN_USD=200`/coin — applies to his NEXT entry, current pos rides; ≤`TRACKER_MAX_LEV=40`x, poll `TRACKER_POLL_S=10`s). Source `0x78aa…` (verified 50d: 42% win, 2.71 payoff, ~95% long, multi-day BTC holder — see `docs/POSITION_THESIS.md`). Follows open/close/flip (NOT size); **+`TRACKER_TP_PCT=0.01` TP**: banks the sleeve at +1% favorable then `TRACKER_REOPEN_COOLDOWN_S=300`s before re-syncing back in (sell-on-1%-repeat for BTC). Isolated → max loss/coin = margin staked. `TRACKER_DRY_RUN` logs only.
- **Alerts:** Telegram + ntfy phone push (`NTFY_TOPIC` in .env) — high-signal ONLY (halt / guardian force-close / daily summary), never per-trade.

---

## Critical rules — never break these
1. `_compute_size` returns `(0.0, lev)` to signal SKIP — callers check `our_size == 0`
2. Copier is STATE-BASED (`reconcile()` diffs trader net positions vs held). NEVER reintroduce `userFills`/fill-stream copying — it churned on TWAP/trim fills and bled fees.
3. `_refresh_account_values` REBUILDS each trader's snapshot fresh every poll (closed coins must vanish) — don't make it additive.
4. `reconcile` is idempotent: desired-vs-actual diff. A failed/missed action re-applies next tick. `copier.risk` must be wired so it can see held positions.
5. Routing: specialist coins (traders.json `specialty`) → that trader only; generalist coins require all holders agree on direction else SKIP (contested); pick highest-conviction holder.
6. Leverage read from `signal.meta["leverage"]`, never hardcoded. `_sync_positions_from_hl` runs at startup before first reconcile.
7. Exit signal `direction` = the side WE HOLD (the one being closed), NOT the offsetting side. Guardian/flip/close all follow this; `_close_position` matches on it.
8. Closes go through `_place_market_close` (reduce-only `market_close`) + `_parse_fill` — never `market_open`, never trust bare `status=="ok"`. A failed close leaves the position in the tracker.

---

## Sizing formula (weighted fixed-per-position — see "Sizing" above)
```
budget    = equity − (TRACKER_MARGIN_USD × |TRACKER_COINS|)      # copy budget, tracker reserved
per_margin = min(budget × COPY_POSITION_PCT × trader.weight, budget × MAX_POSITION_SIZE_PCT)
our_lev   = min(their_lev, COPY_MAX_COPY_LEVERAGE, STOP_LOSS_MARGIN_PCT/(STOP_NOISE_ATR×ATR))
our_notional = per_margin × our_lev
→ skip if our_notional < MIN_POSITION_NOTIONAL ($50)
→ book bounded by MAX_OPEN_POSITIONS + risk caps (graceful, no margin wall)
```

---

## Open issues
- Active-swing pivot (2026-05-25) is freshly deployed — UNPROVEN live: the bank (+25%), the ride-trail, and fresh-entries on the new generalists haven't fired yet. Watch the first of each.
- Generalist deep-vet caveats: 41829013 is a multi-wallet fund op (opaque, hence wt 0.6); 69b05701 is HYPE-beta concentrated (wt 0.4). Re-vet if performance diverges from the perp-durable thesis.
- `docs/POSITION_THESIS.md` is now stale (pre-pivot roster) — re-pull before trusting per-position detail.

---

## Fix log (newest first, keep last 10)
- `967eaf4` Probable-TP scalp + buy-on-add-repeat + unblock BTC/HYPE: exits now FULL +1% TP (`COPY_TP_PCT`, backtest 80% hit in 6h) / −1.5% SL (`COPY_SL_PCT`), no trail-lock → re-enter on next add ("repeat"). Buy-when-they-add extended to OPEN-when-flat (`COPY_REOPEN_ON_ADD`). HYPE unblocked (`MANUAL_COINS={}` — user closed manual short). BTC tracker gains a +1% TP + 300s cooldown (sell-on-1%-repeat, isolated). Superseded bank/ride. Dry-run: TP/SL fire correct, desired incl HYPE; deployed 0 errors. NOTE: copyable add-flow is thin (BTC=tracker; backtest ~0.7 trades/day on alts) — value rides on HYPE being unblocked + the tracker.
- `f32183e` Add-mirroring (scale-IN only, trend-gated): mirror a trader ADDING to a held coin (notional ↑≥10% poll-over-poll) ONLY if the coin's daily trend aligns w/ our dir; add min(their%,50%)×margin capped at per-pos cap + notify. TRIMS not mirrored (their trims are early/neg-edge: adds 90–100% WR in trends vs trims 0–35%). New `action="add"` across risk(approve bypasses one-per-coin + `add_to_position` wtd-avg entry)/executor(`_add_to_position`)/copier(`_emit_add`, `_trend_dir`). Dry-run 5/5; deployed 0 errors.
- `e05a750` FULL active-swing pivot: roster→3 weighted GENERALISTS (f83858 1.0 / 41829013 0.6 / 69b05701 0.4, deep-vetted organic), dropped conviction-hodlers feec88/a9b95f. Weighted fixed-per-position sizing (`COPY_POSITION_PCT×weight` off copy-budget=equity−tracker-reserve; no ÷n dust, no margin wall). All copy coins ride wider stop (max −9%/lev, 1×ATR). Added `MANUAL_COINS={HYPE}` (user's discretionary short — copier hands-off). FIXED fresh-detection bug: newly-added trader (live traders.json reload) seeds baseline, not fresh-adopted (had auto-opened an unwanted ETH short). Dry-run: feasibility 18% of budget, no dust/wall; deployed clean, 0 errors.
- `d1019ca` Stage 2 zero-copy-lag (`FRESH_ENTRY_ONLY`): only open on a trader's fresh flat→pos/flip within 0.5×ATR of their open; never adopt stale holds (the copy-lag leak — BTC entered 3.5% late=+27%, HYPE 56% late=+17%, SOL 10% late=−3%). `_detect_fresh_opens`/`_is_fresh_entry`, baseline-seeds on boot. Dry-run: 5/5 logic tests pass; deployed, baseline seeded, book grandfathered.
- `7a52603` Roster cut to elite-only: a9b95f PINNED→HYPE + feec88 PINNED→SOL (single-coin specialists) + 78aa→BTC tracker; deleted fc667 (gated out) + 4f7634 (closed TON/ZEC). Specialist conviction coins ride wider stop max(−9%/lev, 1.0×ATR).
- `3a69f36` Tighten risk: stop −9% margin, bank +25% (R≈2.78). First live stop fired clean (ETH −$15.61).
- `0ccc6d5` Entry/exit overhaul (see `docs/ENTRY_EXIT_PLAN.md`): (1) HARD stop −20% margin, dropped the broken `max()` ATR floor (was −47/−62%, fired 0×); (2) BANK 50% at +2R then ride; (3) vol-scaled leverage so the stop lands ≥0.5 ATR (HYPE→5x etc.); (4) RESIZE close-and-reopen DISABLED (locked −$79 of churn); (5) entry debounce 2 ticks + conviction gate 0.03→0.05. Tracker → $200/coin, 10s poll. Deployed + read-only reconcile dry-run = in-sync, no errors.
- `8731016` Lev-tracker sleeve (`lev_tracker.py`): auto-mirrors `0x78aa…` direction on TRACKER_COINS (BTC) in isolated margin, fixed $100/coin stake, ≤40x, 60s poll. Follows open/close/flip not size. Wired into main.py gather under `TRACKER_ENABLED`. Dry-run tested (in-sync = no-op).
- `25aa807` "78aa tactic" exits: `_ride_winners` replaces scale-out — tight stop (-25% margin, 0.6×ATR floor) + let winners run (1.5×ATR trail, no early bank). `TRACKER_COINS={BTC}` walls off the manual isolated lev-tracker from the copier (no sync/manage/desire). Executor coin-fallback now accepts `stop_loss`/`ride_trail`.
- `637ed6e` reconcile prunes phantom positions vs OUR live HL state (`drop_phantoms`) — fixes ghosts left by manual close/liquidation/SL-TP. Raise `COPY_MIN_MARGIN_PCT` 0.01→0.03 (no $15 dust trades).

---

## VPS commands
```bash
sudo journalctl -u hl-bot -f                    # live logs
sudo journalctl -u hl-bot -n 200 --no-pager    # last 200 lines
sudo systemctl restart hl-bot                   # restart
cd ~/hl-bot && git pull && sudo systemctl restart hl-bot  # deploy
python -c "import requests; r=requests.post('https://api.hyperliquid.xyz/info', json={'type':'clearinghouseState','user':'<addr>'}); print(r.json()['marginSummary'])"
```

---

## Known HL SDK quirks
- `Exchange.market_open()` — no `reduce_only` param; use `.order()` for reduce-only
- `Exchange.market_close()` makes an extra `user_state` API call internally
- SDK always returns `{"status":"ok"}` even on errors — must check `response.data.statuses[0]`
- WS channel `userFills:{addr}` replays recent fills on reconnect — was a churn source; copier no longer subscribes (state-based). Don't reintroduce it.
