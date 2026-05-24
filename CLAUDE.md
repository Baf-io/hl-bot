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
- **Active traders:** fc667, a9b95f (generalists); 42b6d9→ZEC, 6bea81→SOL, a4dedd→LIT (specialists via `specialty` in traders.json)
- **COPY_TRADER_WHITELIST:** must be empty in .env — traders.json is the source
- **Copy model:** STATE-BASED. `reconcile()` polls each trader's net `clearinghouseState` every `COPY_RECONCILE_INTERVAL_S=45s`, builds a desired portfolio (specialist routing, skip contested coins, highest-conviction holder), diffs vs held, mirrors net changes. NOT fill-driven.
- **Sizing:** margin-based proportional; `COPY_MIN_MARGIN_PCT=0.01` ($11.20 floor); `MIN_POSITION_NOTIONAL=50`; `COPY_MAX_COPY_LEVERAGE=10`

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
→ skip if our_margin   < PORTFOLIO_USD × COPY_MIN_MARGIN_PCT ($11.20)
```

---

## Open issues
- Conviction weighting is mostly flattened by the risk manager's 15% margin cap (big holds all clamp to ~$168 margin). Fine for risk; revisit if we want to over-weight top-conviction coins.
- Reconcile does not RESIZE: a held position whose trader changed size is left as-is until they flip/close. Acceptable (avoids churn); revisit if drift matters.
- Native SL/TP fills (own-signal strategies) close on-exchange but the bot never reconciles them, so the position lingers in `risk.open_positions` until guardian/trader-exit. Low priority (copy trades have no SL/TP).

---

## Fix log (newest first, keep last 10)
- `(uncommitted)` STATE-BASED reconcile rewrite: copier now polls trader net positions every 45s and mirrors only real changes (was fill-stream → churned on TWAP/trim fills, 238 fills/48h, fees > gross loss). Adds specialist routing (ZEC/SOL/LIT) + contested-coin skip (BTC long-vs-short) + conviction pick. Removed fill handler / userFills subs / one-shot backfill.
- `c99e55a` Guardian force-close was dead: sent offsetting direction + full-string reason → never matched. Now sends held direction + bare reason. Close path uses reduce-only `market_close` + `_parse_fill` (was `market_open` + bare `status=="ok"`). Nuclear trigger now margin-based (70%).
- `ce54c96` Add margin floor `COPY_MIN_MARGIN_PCT=0.01` + fix orphaned dust if market_close fails
- `2665b56` Skip dust coins entirely (return 0.0 from _compute_size) instead of flooring
- `7c8a8f1` MIN_POSITION_NOTIONAL=50 backstop in risk manager + executor dust cleanup
- `b5a08ef` asyncio.Event for backfill sync (killed sleep race); dust close on startup
- `9a78997` Executor uses signal leverage not hardcoded 5x
- `5ed2e66` 5-trader whitelist; traders.json path fix; full addresses (lookup script used)
- `b7ae1db` Margin-based sizing (was notional-based — wrong at high leverage)

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
