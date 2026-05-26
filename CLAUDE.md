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
- **⚠️ RISK-POLICY-CONTRACT CLAMP ACTIVE (2026-05-25, commit `93207a8`):** hard limits override everything below — **≤3x, MAX 1 open position, kill-switch −3%/day & −5%/week, NO averaging (adds rejected), PROBE-only (≤$50 notional/≤$5 risk) until a source is KEEP-validated, 40x tracker OFF, funding/cascade OFF.** Book FLATTENED to comply. The aggressive active-swing/scalp/add-mirror/tracker machinery below is SUSPENDED by the clamp (constants still present, gated off). **B progress — ALL LIVE:** ✅B1 native per-order stops, EYES-ON-BOOK verified (`_place_protective_stop` places at the brain's explicit `stop_px` else `COPY_SL_PCT`, then RE-QUERIES `open_orders` for the acked oid — an order that ACKs but isn't resting = FAILURE → force-close; brain entries get ONLY this stop + TTL, NO legacy SL/TP). ✅B2 stop-derived sizing — LIVE entries sized by US: `notional = RISK_PER_TRADE_PCT(1%)×equity / stop_dist`, cap 3x (brain's `size_notional` IGNORED for live; probe keeps ≤$50). ✅B3 KEEP/WATCH/PROBE tiering — `KEEP_SOURCES` (.env) unlocks `mode=live`; non-KEEP → probe-only. **The BRAIN owns the graduation gate: it only emits `mode=probe` for WATCH sources (EmberCN, OnchainLens) — `mode=live` is reserved for sources IT promotes to KEEP. So KEEP_SOURCES stays EMPTY on my side until the brain graduates one (don't pre-graduate — it desyncs the gate).** ✅B4 two-box transport: **LXC-Claude X-scraper brain (node `bafscrape-1`/100.93.122.60) → HMAC-SHA256-signed POST → `src/execution/intake.py` on me (100.115.113.91:8787) → I re-verify sig + idempotency + EVERY cardinal independently.** `INTAKE_ACK_ONLY=false`=live. Lifecycle (entry+stop+TTL) owned by executor/guardian. **Stop-verification echo:** executor records per-`signal_id` fill+stop status; brain polls `GET /status/{sid}` for `stop_resting:true`+oid/px (don't trust bare "accepted"). **Leaderboard copy → SHADOW (paper, `COPY_SHADOW=true`).**
- **Portfolio:** $1120 USDC (PORTFOLIO_USD=1120 in VPS .env)
- **⚠️ Stray HYPE long — INTENTIONALLY KEPT (2026-05-26, user decision):** HYPE LONG 24.27 (~$1.5k notional) @ $61.77, 10x cross, resting Stop @ $59.99 + TP @ $90. Orphaned pre-clamp leaderboard-copy entry (mirrored 0x69b0 ~17:44 05-25 before SHADOW switch). HYPE ∈ `COPIER_SKIP_COINS` → bot won't adopt/manage/close it; NOT in risk book (doesn't eat brain slot). Protected by its stop; user chose keep-as-is. DON'T auto-close it as "drift".
- **⚠️ MANUAL CLAMP-OVERRIDE position (2026-05-25, user-approved):** discretionary BTC LONG mirroring `0x78aa` — **0.01165 BTC @ $77,277, $900 notional, $300 isolated margin @ 3x, hard stop @ $73,000 (resting oid `442192581292`), liq $52.2k, ~$50/4.1% risk.** Opened DIRECTLY via SDK, NOT in the risk book → walled off (isolated + BTC∈`COPIER_SKIP_COINS`; bot won't adopt/manage/auto-close it; brain's 1 slot stays free). Overrides the clamp's "tracker OFF" + max-1 by user instruction. **AUTONOMOUS PROPORTIONAL MIRROR:** `hl-78aa-tracker.service` (standalone systemd, `scripts/watch_78aa_btc.py`, polls 15s) continuously sizes our isolated BTC to `PEG_FRAC(0.00777)×his_size` — scale IN when he adds, OUT when he trims, FLIP when he flips, FLAT when he's flat. Runs 24/7 independent of any session/user-machine. Guardrails: notional CAP `MAX_NOTIONAL=$1500`, per-leg reduce-only stop at `STOP_PCT=5.53%` from avg entry (≈$73k at seed, resized each rebalance, eyes-on-book confirmed), daily kill-switch `HALT_USD=$150` realized (flatten+idle to next UTC day), CLEAN-READ-ONLY (failed read skips tick), `MIN_REBAL_USD=$40` deadband. Seed: our 0.01165 ↔ his 1.5. Isolated + BTC∈`COPIER_SKIP_COINS` → walled off from hl-bot.service. Tune via constants atop the script. Manage: `systemctl {status,stop} hl-78aa-tracker`.
- **Active traders (2026-05-25 → FULL ACTIVE-SWING pivot):** 3 weighted GENERALISTS (multi-coin, two-sided, perp-durable, deep-vetted ORGANIC): `0xf83858`(wt 1.0, 19-coin diversified), `0x41829013`(wt 0.6, multi-wallet fund — trimmed), `0x69b05701`(wt 0.4, HYPE-beta — least). Plus 78aa→BTC tracker. DROPPED the conviction-HODLERS feec88(SOL)/a9b95f(HYPE) — they never trim, incompatible w/ fresh-entry. (Earlier drops: fc667, 4f7634, a4dedd.)
- **MANUAL_COINS (now `{}` — HYPE unblocked 2026-05-25 when user closed his manual short):** coins the USER trades by hand — copier skips them (`COPIER_SKIP_COINS = TRACKER_COINS|MANUAL_COINS`, currently just `{BTC}`). Set `MANUAL_COINS=HYPE,...` to hand-manage a coin again.
- **Tracker findings log (2026-05-26):** `hl-tracker-scan.service` (`scripts/tracker_scan.py`, free HL+Pyth, poll 15m) appends to `data/tracker_findings.md` ONLY on change (78aa moves / candidate opens-closes / shadow round-trip / our-book change) + 12h heartbeat. Prices via Pyth Hermes (`src/tracker/prices.py`, keyless, crypto+equities). Etherscan/Nansen layers pending keys in `.env`. `journalctl -u hl-tracker-scan -f`.
- **Shadow-scan validation (2026-05-25):** `hl-shadow-scan.service` (`scripts/shadow_candidates.py`, paper-only, poll 60s) tracks 5 leaderboard scan candidates ([[durable-twosided-traders]]: ca41/2c5d/78dc/36f2/05c6) — records their FRESH round-trips out-of-sample (current holds seeded as baseline, NOT scored), per-trader cum return%/WR, state in `data/shadow_scan_state.json`. Validates before any live exposure. `journalctl -u hl-shadow-scan -f`.
- **COPY_TRADER_WHITELIST:** must be empty in .env — traders.json is the source
- **Copy model:** STATE-BASED. `reconcile()` (every `COPY_RECONCILE_INTERVAL_S=45s`) rebuilds each trader's net `clearinghouseState`, prunes phantoms vs OUR live HL state (`drop_phantoms`), builds desired portfolio (specialist routing, skip contested, highest-conviction holder), diffs vs held, mirrors net changes. NOT fill-driven. **FRESH-ENTRY-ONLY (`FRESH_ENTRY_ONLY=true`, Stage 2 / zero-copy-lag):** only OPEN on a trader's observed flat→position/flip transition AND within `FRESH_ENTRY_MAX_ATR=0.5`×ATR of their open price; NEVER adopt a position they already hold (stale adoption at a worse price was the copy-lag leak — ETH −$15.61 / SOL −3% vs his +74%). First poll after restart = baseline only; a newly-ADDED trader (roster change/live `traders.json` reload) also seeds to baseline — their existing holds are NOT treated as fresh (this bug once auto-opened an unwanted ETH short on a mid-run reload). (`_detect_fresh_opens`/`_is_fresh_entry`; legacy debounce kept for `FRESH_ENTRY_ONLY=false`.) NOTE: the running bot re-reads `traders.json` every 5 min, so roster edits go LIVE without a restart. **RESIZE close-and-reopen DISABLED (`RESIZE_ENABLED=false`)** — locked running losses + double fees (−$79 of the first −$133); positions ride at entry size until a real exit/flip/stop.
- **Sizing (weighted, fixed-per-position):** each pos = `COPY_POSITION_PCT=0.12` of the COPY BUDGET × the source trader's `weight`, capped `MAX_POSITION_SIZE_PCT=0.15`. **Copy budget = equity − tracker reserve** (`TRACKER_MARGIN_USD×|TRACKER_COINS|`) so the isolated sleeve & copy book never fight for margin. Fixed-per-position (NOT ÷n) so many-coin generalists don't shrink to dust; book bounded by `MAX_OPEN_POSITIONS` + risk caps (graceful, no wall). Gate `COPY_MIN_CONVICTION_PCT=0.05`; `MIN_POSITION_NOTIONAL=50`. **Scalp leverage:** NEW entries use a FIXED `SCALP_LEVERAGE=12` (capped `COPY_MAX_COPY_LEVERAGE=12`), overriding trader-lev + the old vol-cap so a +1% TP is meaningful $ (12x: +1%TP≈+$8 on the alts, −1.5%SL≈−$12; liq ≈ −7.9% so the soft SL has buffer). `VOL_SCALED_LEV` now unused for entries. Existing positions keep their entry lev.
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
our_lev   = min(SCALP_LEVERAGE, COPY_MAX_COPY_LEVERAGE)          # fixed scalp lev (12x)
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
- `bc21adc` Eyes-on-book stop verification + `/status` echo + reverted premature KEEP: `_place_protective_stop` now RE-QUERIES `open_orders` for the acked oid (3 tries) — ACK ≠ resting; an order that acks but isn't on the book → unstopped → force-close. Executor records per-`signal_id` fill+stop in `_signal_status`; intake serves `GET /status/{sid}` so the brain polls `stop_resting:true`+oid/px instead of trusting "accepted". **Reverted EmberCN→KEEP (back to WATCH): the BRAIN owns the graduation gate, sends only `mode=probe` for WATCH; pre-graduating my side desynced it.** `tests/test_stop_verification.py` 5/5 (confirmed/acked-not-resting/rejected/fail-safe/echo). Deployed PID 110137.
- `274b580`+`55519c7` B2/B3 + brain-stop fixes: (B2) LIVE entries sized by us — `notional = 1%·equity / stop_dist`, cap 3x; brain's `size_notional` ignored for live, probe keeps ≤$50. (B3) `KEEP_SOURCES` unlocks `mode=live`; **EmberCN graduated WATCH→KEEP** (next live ETH ≈ $300 notional @ 3x). FIXED double-order bug: `_open_position` no longer fires legacy `_place_native_sltp` for brain entries (it had added an unwanted TP @ $2366 to the live probe — cancelled it; brain = stop+TTL only). Brain entries now also set their capped leverage (was gated `is_copy` only). Deployed PID 108410, KEEP loaded, book = 1 stop.
- `b1bbc69` Fixed `_place_protective_stop` `UnboundLocalError` (`sl` referenced on the brain explicit-stop path) — had force-closed the first live probe at breakeven (contract held: no unstopped position). First clean live brain fill after: 0.0237 ETH @ $2112.5 ($50), stop @ $2029.
- `93207a8` RISK-POLICY-CONTRACT CLAMP: ≤3x, MAX 1 position, kill-switch −3%/day & −5%/week, NO averaging (adds→REJECT), PROBE-only (≤$50 notional/≤$5 risk) until KEEP, tracker/funding/cascade OFF. Book flattened to comply (equity $1201). B1 native per-order stops + intake server (`intake.py`, aiohttp, HMAC, idempotent, re-checks all cardinals) added. Leaderboard → SHADOW.
- `f4e6839` Higher-leverage scalp: NEW copy entries use FIXED `SCALP_LEVERAGE=12` (raised `COPY_MAX_COPY_LEVERAGE` 10→12), overriding trader-lev + the vol-cap, so the +1% TP is meaningful (~+$8/scalp on alts vs ~$1 at the old 2.3x vol-cap; −1.5% SL ≈ −$12; liq −7.9% gives the 45s soft-SL buffer). Modelled 10/12/15x; deployed 12x, 0 errors. Above ~12x would need native exchange stops (soft SL polls every 45s).
- `967eaf4` Probable-TP scalp + buy-on-add-repeat + unblock BTC/HYPE: exits now FULL +1% TP (`COPY_TP_PCT`, backtest 80% hit in 6h) / −1.5% SL (`COPY_SL_PCT`), no trail-lock → re-enter on next add ("repeat"). Buy-when-they-add extended to OPEN-when-flat (`COPY_REOPEN_ON_ADD`). HYPE unblocked (`MANUAL_COINS={}` — user closed manual short). BTC tracker gains a +1% TP + 300s cooldown (sell-on-1%-repeat, isolated). Superseded bank/ride. Dry-run: TP/SL fire correct, desired incl HYPE; deployed 0 errors. NOTE: copyable add-flow is thin (BTC=tracker; backtest ~0.7 trades/day on alts) — value rides on HYPE being unblocked + the tracker.
- `f32183e` Add-mirroring (scale-IN only, trend-gated): mirror a trader ADDING to a held coin (notional ↑≥10% poll-over-poll) ONLY if the coin's daily trend aligns w/ our dir; add min(their%,50%)×margin capped at per-pos cap + notify. TRIMS not mirrored (their trims are early/neg-edge: adds 90–100% WR in trends vs trims 0–35%). New `action="add"` across risk(approve bypasses one-per-coin + `add_to_position` wtd-avg entry)/executor(`_add_to_position`)/copier(`_emit_add`, `_trend_dir`). Dry-run 5/5; deployed 0 errors.
- `e05a750` FULL active-swing pivot: roster→3 weighted GENERALISTS (f83858 1.0 / 41829013 0.6 / 69b05701 0.4, deep-vetted organic), dropped conviction-hodlers feec88/a9b95f. Weighted fixed-per-position sizing (`COPY_POSITION_PCT×weight` off copy-budget=equity−tracker-reserve; no ÷n dust, no margin wall). All copy coins ride wider stop (max −9%/lev, 1×ATR). Added `MANUAL_COINS={HYPE}` (user's discretionary short — copier hands-off). FIXED fresh-detection bug: newly-added trader (live traders.json reload) seeds baseline, not fresh-adopted (had auto-opened an unwanted ETH short). Dry-run: feasibility 18% of budget, no dust/wall; deployed clean, 0 errors.
- `d1019ca` Stage 2 zero-copy-lag (`FRESH_ENTRY_ONLY`): only open on a trader's fresh flat→pos/flip within 0.5×ATR of their open; never adopt stale holds (the copy-lag leak — BTC entered 3.5% late=+27%, HYPE 56% late=+17%, SOL 10% late=−3%). `_detect_fresh_opens`/`_is_fresh_entry`, baseline-seeds on boot. Dry-run: 5/5 logic tests pass; deployed, baseline seeded, book grandfathered.
- `7a52603` Roster cut to elite-only: a9b95f PINNED→HYPE + feec88 PINNED→SOL (single-coin specialists) + 78aa→BTC tracker; deleted fc667 (gated out) + 4f7634 (closed TON/ZEC). Specialist conviction coins ride wider stop max(−9%/lev, 1.0×ATR).

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
