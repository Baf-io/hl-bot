# HL-Bot Agent Briefing

> **Self-maintenance rule:** after every fix, move resolved bugs from "Open issues" → "Fix log" (keep last 10), update "Current state" to reflect what's actually deployed, delete stale context. This file must stay under ~120 lines.

---

## What this bot does
Hyperliquid perp trading **infrastructure**. The model is **sleeve-copy on subaccounts** (each smart-money source mirrored on its own isolated sub) + **discretionary trading on MAIN** (user-owned, hands-off from any bot signal) + **alerts/watchdogs** + a **paper-validated leaderboard shadow**. All processes are independent systemd services so a single failure only takes down its own sleeve. Runs 24/7 on a Linux VPS.

---

## Architecture (one file per role)
| Path | Role |
|---|---|
| `scripts/watch_swing_sleeve.py` | Sleeve engine: env-parametrized, mirrors ONE trader's direction on a sub (`vault_address` routing), fresh-entry-only, dead-man stop, daily kill-switch. One systemd service per source. |
| `scripts/watch_flip_alerts.py` | Cohort-flip detector: ntfy when ≥N high-trust sources rotate same direction on same coin in a window. |
| `scripts/watch_pos_alert.py` | Single-trade watchdog: ntfy on limit-fill + escalating adverse-move ladder; auto-disarm. |
| `scripts/watch_btc_alert.py` | BTC range ntfy. |
| `scripts/tracker_scan.py` | Change-triggered findings log (HL+Pyth free layer). |
| `scripts/shadow_*.py` | Paper-validate candidate sources without capital. |
| `scripts/trust_forensic.py` | Consistency-first forensic vetting (script is the spec — read before adding sources). |
| `scripts/{candidate,contestant,deep,drift,hft,intraday,jupperp,regime,solana}_scan.py` + `forensic_worker.py` | Research/scan toolkit feeding `data/research2/COPYABLE_DB.md`. |
| `src/main.py` | `hl-bot.service`: leaderboard-copy in SHADOW + brain intake server (B4) + position guardian. NO live MAIN trading signals here. |
| `src/signals/leaderboard_copy.py` | State-based reconcile of the multi-source roster (SHADOW only). |
| `src/execution/{executor,intake}.py` | Order placement + HMAC brain-signal receiver. |
| `src/risk/manager.py` | Risk gating for intake-driven orders (probe-only until KEEP). |
| `src/tracker/prices.py` | Pyth price layer (used by scanners). |
| `config/traders.json` | Roster of 12 weighted traders (read live every 5min by leaderboard). |
| `config/settings.py` | All tunables. |
| `data/research2/COPYABLE_DB.md` | Canonical vet-passed source list. |
| `research/` | Cross-agent handoffs (scanner agent → me). |

---

## Infrastructure (2026-05-30 migration)
Two-box architecture for IP-budget isolation + lower live-trade latency:
- **us-east-1 Hetzner Ashburn `5.161.252.215`** (live trading) — `hl-77998579-sleeve`,
  `hl-bbf82c80-scalp-sleeve` (PAPER), `hl-pos-watch`, `hl-flip-alerts`. RTT to HL ~200ms
  (vs 245ms from Nuremberg, mostly server-side processing — WS push should be much
  cheaper). Agent: `bot-ashburn` (key rotated during migration, separate from any
  prior key — never reused across boxes).
- **Nuremberg Hetzner** (research/scanning + intake) — `hl-bot` (intake server, has
  Tailscale binding for LXC POST, kept here until LXC IP cutover), `hl-shadow-scan`,
  `hl-source-health`, `hl-candle-log`, `hl-btc-alert`, `hl-tracker-scan`. Heavy /info
  reads stay here so they don't compete with live-trading IP budget.
- SSH segregation: Nuremberg can no longer reach Ashburn (migration key removed
  post-cutover). User-laptop key authorizes both boxes independently.

## Current state (update after each deploy)
- **Equity / accounts (2026-05-28 19:10):** MAIN $702.82 (user discretion, hands-off from all bot signals — recent: BTC short 0.04794 @ $73,583 + scale-in limit @ $74,200, watched by `hl-pos-watch`; legacy PURR funding-carry short still open pending user decision). Sub Baf $483.72 = lead77 sleeve. Other subs drained ~$1 (Downside, SwingDyn, AkkaVault — kept for future re-funding).
- **Live sleeve (only one):** `hl-77998579-sleeve` on Sub Baf `0xdac952c2…2246`, mirrors `0x77998579` (trust forensic score 66.2, highest vetted) bar-for-bar on BTC+ETH. `MAX_CONCURRENT=3`, `MARGIN_PCT=0.40`, `LEV=4` iso → $773/leg notional, $193 margin, $93 dead-man stop-loss. Fresh-entry-only, baseline-seeded his BTC -13.1 + ETH -74.1 (not adopted).
- **Live alerts:** `hl-flip-alerts` (cohort flip ≥2 sources / 6h / BTC+ETH+SOL+HYPE → ntfy), `hl-pos-watch` (fill detection + adverse-move ladder $74.2k/74.5k/75.0k/75.5k + auto-disarm), `hl-btc-alert` (range $74k-$78k), `hl-tracker-scan` (findings log).
- **Live shadows (paper-only, no capital):** `hl-shadow-scan` (5 candidates), `hl-shadow-downside` (Downside vault full alt book), `hl-shadow-2385`.
- **hl-bot.service:** leaderboard-copy in SHADOW (paper, doesn't touch MAIN) + brain intake server LIVE on `:8787` (HMAC-signed POST from LXC scraper at `bafscrape-1`; probe-only at ≤$50 until brain promotes a source to KEEP — `KEEP_SOURCES` stays empty here, brain owns the gate).
- **Telegram + ntfy** (`NTFY_TOPIC=bafscraper-1`) for high-signal alerts only: halt / guardian force-close / daily summary / cohort flip / pos-watch ladder.

---

## Critical rules — never break these
1. **NEVER touch MAIN from a hl-bot strategy.** MAIN is user-discretion-only. Any new signal must run as its own `scripts/watch_*.py` against a dedicated sub. (FundingCarry/Cascade/etc. were removed 2026-05-28 because they violated this and opened PURR on MAIN.)
2. **Sub orders use `vault_address`, not `account_address`** (see `scripts/watch_swing_sleeve.py`). `account_address` only affects reads; orders signed for `vault_address` execute on that sub. Always pull the FULL sub address from the `subAccounts` API — never infer from a truncated CLAUDE.md `0xdac9…2246`.
3. **NEVER adopt a held position** (sleeve rule). First sighting → baseline-seed (record his current size, do NOT mirror). Only fire on a witnessed flat→position or flip transition. `ADOPT_BAND_PCT>0` allows comparable-price adoption (±band% of his entry) but defaults OFF.
4. **Sleeve sizes legs from OUR equity** (`MARGIN_PCT × eq × LEV`), never proportional-pegged to the source's notional. Proportional pegs are banned — they uncap exposure to the source's leverage decisions.
5. **Leaderboard copier (SHADOW) is idempotent state-based reconcile**, never fill-stream. Diff desired-vs-actual every `COPY_RECONCILE_INTERVAL_S=45s`; a missed action self-heals next tick.
6. **Exits use `_place_market_close`** (reduce-only) + `_parse_fill` — never `market_open`, never trust bare `status=="ok"`. SDK returns `ok` even on errors.
7. **Brain intake re-verifies every cardinal independently** (HMAC sig + idempotency + size + price + symbol + side). `KEEP_SOURCES` on MY side stays empty unless the brain explicitly graduates a source to KEEP.
8. **Trust forensic before adding any source.** Score < 50 → reject. Hard gates: martingale rate, concentration, one-trade-dependency, taker%, sample size. The headline WR / PnL on a leaderboard means nothing without these.

---

## VPS commands
```bash
sudo journalctl -u hl-77998579-sleeve -f       # main live sleeve
sudo journalctl -u hl-flip-alerts -f           # cohort flip detector
sudo journalctl -u hl-pos-watch -f             # discretionary trade watchdog
sudo journalctl -u hl-bot -f                   # leaderboard shadow + brain intake
cd ~/hl-bot && git pull && sudo systemctl restart hl-bot   # deploy
python -c "import requests; r=requests.post('https://api.hyperliquid.xyz/info', json={'type':'clearinghouseState','user':'<addr>'}); print(r.json()['marginSummary'])"
```

---

## Open issues
- `hl-pos-watch` only watches one trade at a time (env-parametrized). If you open a second discretionary trade on MAIN, the watchdog needs its own service instance or refactor to multi-trade.
- Brain pipeline (`scripts/intake.py` → executor): `KEEP_SOURCES` empty by design; only the brain graduates a source. Confirm with brain-side before flipping any source live.

---

## Fix log (newest first, keep last 10)
- 2026-05-28 PM (`<new commit>`) **Major cleanup.** Removed funding-carry/cascade/OI-squeeze/stat-arb/momentum/lev-tracker signal modules + `find_traders.py` + `test_leaderboard.py` + retired sleeve services (hl-26fe / hl-78aa-tracker / hl-807 / hl-95bfa / hl-akka / hl-downside) + one-shot scan services (hl-deep / hl-drift / hl-jupperp / hl-sol / hl-shadow-b65d) + stale state files. `STRATEGY_FUNDING_CARRY`/`STRATEGY_CASCADE` flags purged from `.env` after FundingCarry rogue-opened PURR on MAIN. `src/main.py` slimmed to leaderboard-shadow + brain-intake only. `config/settings.py` purged of TRACKER_/CASCADE_/FUNDING_/STRATEGY_OI/STAT/MOMENTUM blocks. Net: 9 live services, src/ has no dead signal modules.
- 2026-05-28 (`dbabcbc`) feat: `hl-pos-watch` (limit-fill + adverse-move ladder watchdog) + `research/trader_candidates.md` (sister-agent handoff).
- 2026-05-28 (`1cd4c79`) docs: `research/HANDOVER_TO_SCANNER_AGENT.md` (spec for the chain-economy scanner: hard gates, output schema, common traps).
- 2026-05-28 (`0e81085`) chore: consolidated multi-agent scan/research toolkit + flip-alerts + roster expansion (3→12 traders).
- 2026-05-28 services: `hl-flip-alerts` (ntfy on cohort rotation). `hl-77998579-sleeve` deployed @ 4x/40% on Sub Baf after Downside RETIRED. `0x77998579` forensic 66.2 (highest ever); whales 0x99967871/0x739c52c1/0x99df385a/0x807ddb66/0x3093189b/0xeb47e64c all FAILED trust gates.
- 2026-05-27 (`3c60252`) fix(sleeves): `vault_address` sub-routing + fresh-entry one-slot + N+1 API fix; vault scan & forensics tools.
- 2026-05-26 (`2bb3d41`) feat: `hl-tracker-scan.service` — change-triggered findings log (free HL+Pyth).
- 2026-05-25 (`d1019ca`) Stage 2 zero-copy-lag (`FRESH_ENTRY_ONLY`): only open on a fresh flat→pos/flip within 0.5×ATR; never adopt stale holds.

---

## Known HL SDK quirks
- `Exchange.market_open()` — no `reduce_only` param; use `.order()` for reduce-only.
- `Exchange.market_close()` makes an extra `user_state` API call internally.
- SDK always returns `{"status":"ok"}` even on errors — must check `response.data.statuses[0]`.
- WS channel `userFills:{addr}` replays recent fills on reconnect — was a churn source; copier no longer subscribes (state-based). Don't reintroduce it.
