# HL-Bot Agent Briefing

> **Agent self-maintenance rule:** After every fix, update this file:
> - Move resolved bugs from "Open issues" to "Fix log" (one line each, keep last 10)
> - Update "Current state" section to reflect what's actually deployed
> - Delete stale context — this file must stay under ~120 lines

---

## What this bot does
Hyperliquid perp trading bot. Copies 5 whitelisted leaderboard traders
proportionally (margin-based sizing), plus cascade + funding carry strategies.
Runs 24/7 on a Linux VPS as `hl-bot.service`.

---

## Architecture (one line per file)
| File | Role |
|---|---|
| `src/main.py` | Wiring, scheduler, guardian loop |
| `src/signals/leaderboard_copy.py` | Copy engine: STATE-BASED reconcile (polls trader net positions), specialist routing, sizing |
| `src/execution/executor.py` | Order placement, startup position sync |
| `src/risk/manager.py` | Risk gating (one-per-coin, margin/notional caps) |
| `config/settings.py` | All tunable constants |
| `config/traders.json` | The 5 whitelisted trader addresses + labels |

---

## Current state (update after each deploy)
- **Portfolio:** $1120 USDC (PORTFOLIO_USD=1120 in VPS .env)
- **Active traders (2026-05-25 cut to elite-only):** a9b95f→HYPE, feec88→SOL — **single-coin specialists, PINNED** (only their `specialty` coin is copied; their other positions ignored). Plus 78aa→BTC tracker (separate sleeve). DROPPED: fc667 (contributed nothing — all <3.4% conv, gated out), 4f7634 (not worth following — closed TON/ZEC), a4dedd/LIT (earlier).
- **COPY_TRADER_WHITELIST:** must be empty in .env — traders.json is the source
- **Copy model:** STATE-BASED. `reconcile()` (every `COPY_RECONCILE_INTERVAL_S=45s`) rebuilds each trader's net `clearinghouseState`, prunes phantoms vs OUR live HL state (`drop_phantoms`), builds desired portfolio (specialist routing, skip contested, highest-conviction holder), diffs vs held, mirrors net changes. NOT fill-driven. NEW entries debounced `COPY_ENTRY_DEBOUNCE_TICKS=2` ticks (kills fleeting copies). **RESIZE close-and-reopen DISABLED (`RESIZE_ENABLED=false`)** — it locked running losses + double fees (−$79 of the first −$133); positions ride at entry size until a real exit/flip/stop.
- **Sizing:** equal-weight to `COPY_TARGET_DEPLOY=0.85`, capped `MAX_POSITION_SIZE_PCT=0.15` margin/pos; gate `COPY_MIN_CONVICTION_PCT=0.05` (their margin ≥5% of their acct to copy/vote); `MIN_POSITION_NOTIONAL=50`; `COPY_MAX_COPY_LEVERAGE=10`. **Vol-scaled leverage (`VOL_SCALED_LEV=true`):** per-coin lev capped to `STOP_LOSS_MARGIN_PCT/(STOP_NOISE_ATR=0.5·dailyATR)` so the −9% stop always lands ≥0.5 ATR away (with stop=0.09: HYPE→~2.3x, SOL→4.3x, BTC→7x). Applies to NEW entries only (existing positions keep their entry leverage).
- **Exits (HARD STOP + BANK-AND-RIDE, `RIDE_WINNERS_ENABLED`, `_ride_winners`):** HARD stop −`STOP_LOSS_MARGIN_PCT=0.09` of margin (price = pct/lev), **NO ATR floor** (old `max()` floor made it −47/−62% & fired 0×), enforced even if ATR missing. BANK `BANK_FRACTION=0.50` at +`BANK_AT_MARGIN_RET=0.25` margin (R≈2.78), then runner trails: after peak clears `RIDE_ACTIVATE_ATR=1.0`×ATR, exit on `RIDE_GIVEBACK_ATR=1.0`×ATR retrace. NOTE: −9% on an existing 10x pos = ~0.2 ATR (noise-tight by design — user's low-risk call). **Specialist conviction coins (SOL/HYPE) ride WIDER:** stop = max(−9%/lev, `SPECIALIST_STOP_ATR=1.0`×ATR) so we don't noise-chop an elite hold — bank + follow-trader-exit are the controls there. Guardian −70% nuclear = deep backstop. See `docs/ENTRY_EXIT_PLAN.md`.
- **Lev-tracker sleeve (`src/signals/lev_tracker.py`, `TRACKER_ENABLED`):** auto-mirrors ONE trader's DIRECTION on `TRACKER_COINS={BTC}` in ISOLATED margin (`TRACKER_MARGIN_USD=200`/coin — applies to his NEXT entry, current pos rides; ≤`TRACKER_MAX_LEV=40`x, poll `TRACKER_POLL_S=10`s). Source `0x78aa…` (verified 50d: 42% win, 2.71 payoff, ~95% long, multi-day BTC holder — see `docs/POSITION_THESIS.md`). Follows open/close/flip (NOT size); isolated → max loss/coin = margin staked. Walled off. `TRACKER_DRY_RUN` logs only.
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

## Sizing formula
```
their_lev       = min(notional / marginUsed, COPY_MAX_COPY_LEVERAGE)  [cached per coin]
their_margin    = their_notional / their_lev
their_margin_pct = their_margin / their_acct_val
our_margin      = PORTFOLIO_USD × their_margin_pct
our_notional    = our_margin × their_lev
→ skip if our_notional < MIN_POSITION_NOTIONAL ($50)
→ skip if our_margin   < PORTFOLIO_USD × COPY_MIN_MARGIN_PCT (~$34)
```

---

## Open issues
- Conviction weighting is mostly flattened by the risk manager's 15% margin cap (big holds all clamp to ~$168 margin). Fine for risk; revisit if we want to over-weight top-conviction coins.
- "Faster in-and-out" framework requested (2026-05-24): user wants higher-margin, more active trades. These 5 traders are slow macro holders, so faster trading needs a different alpha sleeve (momentum/cascade) — see `docs/FRAMEWORK_PLAN.md` (pending). NOT yet implemented; copy core is the proven base.

---

## Fix log (newest first, keep last 10)
- `0ccc6d5` Entry/exit overhaul (see `docs/ENTRY_EXIT_PLAN.md`): (1) HARD stop −20% margin, dropped the broken `max()` ATR floor (was −47/−62%, fired 0×); (2) BANK 50% at +2R then ride; (3) vol-scaled leverage so the stop lands ≥0.5 ATR (HYPE→5x etc.); (4) RESIZE close-and-reopen DISABLED (locked −$79 of churn); (5) entry debounce 2 ticks + conviction gate 0.03→0.05. Tracker → $200/coin, 10s poll. Deployed + read-only reconcile dry-run = in-sync, no errors.
- `8731016` Lev-tracker sleeve (`lev_tracker.py`): auto-mirrors `0x78aa…` direction on TRACKER_COINS (BTC) in isolated margin, fixed $100/coin stake, ≤40x, 60s poll. Follows open/close/flip not size. Wired into main.py gather under `TRACKER_ENABLED`. Dry-run tested (in-sync = no-op).
- `25aa807` "78aa tactic" exits: `_ride_winners` replaces scale-out — tight stop (-25% margin, 0.6×ATR floor) + let winners run (1.5×ATR trail, no early bank). `TRACKER_COINS={BTC}` walls off the manual isolated lev-tracker from the copier (no sync/manage/desire). Executor coin-fallback now accepts `stop_loss`/`ride_trail`.
- `637ed6e` reconcile prunes phantom positions vs OUR live HL state (`drop_phantoms`) — fixes ghosts left by manual close/liquidation/SL-TP. Raise `COPY_MIN_MARGIN_PCT` 0.01→0.03 (no $15 dust trades).
- `8140e87` synced positions now carry real leverage (notional/marginUsed); was defaulting lev=1.0 → full notional counted as margin-delta → delta limit spuriously blocked entries after restarts.
- `a2b3804` ntfy phone alerts, high-signal only (halt / force-close / daily summary).
- `94fc6ca` reconcile RESIZEs under-sized holdings (vs post-cap target, no oscillation); dedupe contested log.
- `f33477a` STATE-BASED reconcile rewrite: poll trader net positions every 45s, mirror only real changes (was fill-stream → churned on TWAP/trim fills, 238 fills/48h, fees > gross loss). Specialist routing + contested skip + conviction pick. Removed fill handler/userFills/backfill.
- `c99e55a` Guardian force-close was dead (offsetting dir + full-string reason never matched). Now held dir + bare reason. Close via reduce-only `market_close` + `_parse_fill`. Nuclear now margin-based (70%).
- `ce54c96` Add margin floor `COPY_MIN_MARGIN_PCT` + fix orphaned dust if market_close fails

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
