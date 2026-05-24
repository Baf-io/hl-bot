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
| `src/signals/leaderboard_copy.py` | Copy engine: fill handler, sizing, backfill |
| `src/execution/executor.py` | Order placement, startup position sync |
| `src/risk/manager.py` | Risk gating (one-per-coin, margin/notional caps) |
| `config/settings.py` | All tunable constants |
| `config/traders.json` | The 5 whitelisted trader addresses + labels |

---

## Current state (update after each deploy)
- **Portfolio:** $1120 USDC (PORTFOLIO_USD=1120 in VPS .env)
- **Active traders:** fc667, 42b6d9, a9b95f, 6bea81 (SOL), a4dedd (LIT)
- **COPY_TRADER_WHITELIST:** must be empty in .env — traders.json is the source
- **Sizing:** margin-based proportional; `COPY_MIN_MARGIN_PCT=0.01` (1% = $11.20 floor)
- **Min notional:** `MIN_POSITION_NOTIONAL=50`
- **Leverage cap:** `COPY_MAX_COPY_LEVERAGE=10`

---

## Critical rules — never break these
1. `_compute_size` returns `(0.0, lev)` to signal SKIP — callers check `our_size == 0`
2. Never emit an exit signal for a coin not in `_trader_positions[address]` — it's a WS replay
3. Leverage is read from `signal.meta["leverage"]`, never hardcoded
4. `_sync_positions_from_hl` runs sync at startup — all HL positions registered before backfill
5. Backfill waits on `_refresh_done` event, NOT a sleep
6. Exit signal `direction` = the side WE HOLD (the one being closed), NOT the offsetting side. Guardian/flip/trader-close all follow this. `_close_position` matches on it.
7. Closes go through `_place_market_close` (reduce-only `market_close`) + `_parse_fill` — never `market_open`, never trust bare `status=="ok"`. A failed close leaves the position in the tracker.

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
- **Per-trader adaptive logic (not started):** copy sizing/entry is one-size-fits-all. Traders differ in sizing style & entry cadence (scaling in vs single-shot) — needs per-trader handling. Also unresolved: coin-conflict when a copy signal hits a coin already held by cascade/funding (currently first-come-wins, copy silently blocked).
- Native SL/TP fills (own-signal strategies) close on-exchange but the bot never reconciles them, so the position lingers in `risk.open_positions` until guardian/trader-exit. Low priority (copy trades have no SL/TP).

---

## Fix log (newest first, keep last 10)
- `(uncommitted)` Guardian force-close was dead: sent offsetting direction + full-string reason → never matched. Now sends held direction + bare reason. Close path now uses reduce-only `market_close` + `_parse_fill` (was `market_open` + bare `status=="ok"` → orphaned live positions / flip risk). Nuclear trigger now margin-based (70% of margin), was price-move % that couldn't fire before liquidation on leverage.
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
- WS channel `userFills:{addr}` **replays recent fills on reconnect** — guard with `was_tracking` check
