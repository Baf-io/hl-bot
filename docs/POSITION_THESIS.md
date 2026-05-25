# HL-Bot — Live Position Thesis & Ledger

> **Purpose:** a single, *accurate* record of what we hold, why, and how each
> trade reasons from BOTH the copied trader's perspective and ours.
> **Rule for this file: 100% API-sourced data only. No invented numbers.**
> Every figure below was pulled live from `https://api.hyperliquid.xyz/info`.
> Sections are tagged **[DATA]** (verbatim from the API) or **[THESIS]** (inference — clearly labelled).

**Snapshot taken:** 2026-05-25 ~12:25–12:35 UTC (our book + all 5 source traders pulled within the same 10-min window).

### How to refresh this file (keep it honest)
```bash
# our book + closes:
ADDR=$(grep -E '^HL_WALLET_ADDRESS=' .env | cut -d= -f2 | tr -d '"'"'"'"')
python3 -c "import requests;print(requests.post('https://api.hyperliquid.xyz/info',json={'type':'clearinghouseState','user':'$ADDR'}).json()['assetPositions'])"
# any source trader (swap addr): {'type':'clearinghouseState','user':'<addr>'} and {'type':'userFills','user':'<addr>'}
```
Re-run the 4 trader sub-agent pulls + the our-state pull, then update the tables below. **Do not edit numbers by hand from memory.**

---

## 1. Account & true PnL  **[DATA]**

| item | value |
|---|---|
| Net deposited (ledger `send` transfers from `0x6b9e…`) | **$1,391.39** (+ ~$15.78 USOL spot) |
| Current equity (perp accountValue) | **$1,246.75** |
| **Net PnL** | **≈ −$131 to −$144 (−9.4% to −10.4%)** |
| Realized PnL, last 149 closes in fill window | **−$133.08** |
| Fees paid in that window | **$36.46** (27% of the loss) |
| Margin in use | $938.51 (75%) · withdrawable $308.24 |

Deposits: $259.61 + $854.85 (May 23) + $276.93 (May 24). Cross-check: realized −$133 ≈ ledger-derived net −$131. **The two methods agree → the −$130-ish loss is real and accurate.**

---

## 2. Our open positions  **[DATA]** (snapshot 12:34 UTC — prices drift live)

| coin | side | notional | lev | mode | our entry | uPnL | liqPx | source |
|---|---|---|---|---|---|---|---|---|
| BTC | LONG | $4,012 | 40x | **isolated** | $77,098.00 | +$12.19 | $76,148 (−1.2%) | **78aa tracker** |
| HYPE | LONG | $1,350 | 10x | cross | $61.8788 | +$22.88 | $14.97 | a9b95f |
| ETH | SHORT | $1,290 | 10x | cross | $2,102.40 | −$8.47 | $3,687 | a9b95f |
| SOL | SHORT | $1,869 | 10x | cross | $85.8790 | −$0.28 | $129.70 | feec88 (specialist) |
| TON | SHORT | $189 | 1x | cross | $1.8044 | +$0.15 | $10.69 | 4f7634 |
| ZEC | SHORT | $188 | 1x | cross | $664.0300 | −$2.09 | $3,994 | 4f7634 (specialist) |

Net: **2 longs (BTC, HYPE) vs 4 alt shorts (ETH, SOL, TON, ZEC).**

---

## 3. Source traders — live stance  **[DATA]**

Conviction = (their position notional ÷ their leverage) ÷ their account value = committed margin as % of their account. Our copy gate = **≥3%** (below that they don't copy *and* don't vote on direction).

| trader | acct value | their position(s) we care about | conviction | their uPnL |
|---|---|---|---|---|
| **a9b95f** | $5,426,687 | **HYPE LONG** (entry $39.63) | **57.3%** | +$11.55M (ROE 5.9x) |
| | | **BTC SHORT** (entry $72,401) | **17.8%** | −$1.24M |
| | | **ETH SHORT** (entry $2,314) | **5.85%** | +$0.59M |
| **feec88** | $558,344 | **SOL SHORT** (entry $95.28) | **~100%** | +$422k (ROE +68%) |
| | | HYPE SHORT (entry $48.83) | 1.22% (below gate) | −$15.6k |
| **4f7634** | $286,253 | **ZEC SHORT** (entry $629.10) | **54.25%** | −$10.6k |
| | | **TON SHORT** (entry $1.96096) | **47.1%** | +$12.3k |
| **fc667** | $20,969,651 | BTC LONG 3.40% · HYPE LONG 2.25% · SOL SHORT 2.07% · ETH SHORT 1.89% · PAXG 1.23% · XRP 0.15% | **only BTC ≥3%** | mixed |
| **78aa** (tracker) | $100,359 | **BTC LONG 40x** (entry $74,501) | 2.9% margin / 116% notional | +$4,420 |

---

## 4. Position-by-position thesis

### BTC LONG (isolated tracker — 78aa)
- **[DATA]** Our entry $77,098, 40x isolated, $108 margin, liq $76,148 (a 1.2% BTC drop liquidates the $108). 78aa entry $74,501, holding, +$4,420.
- **[THESIS — theirs]** 78aa is a 95%-long momentum trend-follower; BTC is his single best coin (+$21k/50d) and current core. He rides — median BTC hold 5.6 days. See `[[lev-tracker-sleeve]]`.
- **[THESIS — ours]** We mirror his *direction only*, fixed $100 isolated, walled off from the copy core. We're long from a *higher* price than him ($77,098 vs $74,501), so we capture only the forward move. Closes automatically within ~10s of his close (poll tightened 60→10s).
- **⚠️ Conflict to note:** a9b95f — a trader we *do* copy — is **SHORT BTC at 17.8% conviction** (our 2nd-strongest signal anywhere) and losing on it. We deliberately defer to 78aa on BTC instead. Conscious choice, not a bug, but our biggest directional bet contradicts a high-conviction copy trader.

### HYPE LONG (a9b95f)
- **[DATA]** Our entry $61.8788, +$22.88. a9b95f entry $39.6337, conviction **57.3%**, +$11.55M.
- **[THESIS — theirs]** HYPE is a9b95f's max-conviction core (57% of account, ROE 5.9x). He is a structural HYPE bull.
- **[THESIS — ours]** Routed to a9b95f as highest-conviction long holder. fc667 (2.25%) and feec88's HYPE *short* (1.22%) are both below the 3% gate, so they neither contest nor vote → uncontested long. We entered 56% above his price, so we only ride the move past $61.88. Currently our best winner.

### ETH SHORT (a9b95f)
- **[DATA]** Our entry $2,102.40, −$8.47. a9b95f entry $2,314, conviction 5.85%, +$0.59M.
- **[THESIS — theirs]** Part of a9b95f's "HYPE-outperformance, fade the majors" relative-value stance (long HYPE, short BTC+ETH).
- **[THESIS — ours]** Only a9b95f clears the gate on ETH (fc667's 1.89% is filtered out) → uncontested short.

### SOL SHORT (feec88 — specialist)
- **[DATA]** Our entry $85.8790, −$0.28, liq $129.70 (safe). feec88 entry $95.28, conviction **~100%**, +$422k, actively scaling in.
- **[THESIS — theirs]** feec88 is maximally short SOL (his entire account) and adding on strength — his single highest-conviction bet of any trader we follow.
- **[THESIS — ours]** SOL is feec88's *specialty* coin → routes to him alone, ignores fc667's 2.07% SOL short. Faithful copy of his strongest signal.

### TON SHORT (4f7634)
- **[DATA]** Our entry $1.8044, +$0.15, 1x. 4f7634 entry $1.96096, conviction 47.1%, +$12.3k.
- **[THESIS — theirs]** One leg of 4f7634's two-legged short book (ZEC+TON), run fully-collateralized at 1x (low liquidation risk). Freshly built 2026-05-22.
- **[THESIS — ours]** Generalist coin, only 4f7634 holds it → uncontested. We mirror his 1x (our 1x $189 is faithful, not a sizing bug). **Note:** the −$50.48 TON loss in §5 was a RESIZE closing+reopening this position, not a stop.

### ZEC SHORT (4f7634 — specialist)
- **[DATA]** Our entry $664.03, −$2.09, 1x. 4f7634 entry $629.10, conviction **54.25%** (his largest), currently −$10.6k (red).
- **[THESIS — theirs]** 4f7634's biggest position; he's short from $629 and underwater as ZEC rose to ~$664. He's holding (1x, liq $1,863 — far away).
- **[THESIS — ours]** ZEC is his specialty → routes to him alone. We're short from a better price ($664 vs his $629), so we're closer to break-even than he is.

---

## 5. Recent closes — what happened & why  **[DATA + reason]**

| time (UTC) | coin | PnL | reason (from logs) |
|---|---|---|---|
| 05-25 08:48 | TON | **−$50.48** | RESIZE ($1,326→$189): closed full pos, reopened smaller — locked the running loss |
| 05-25 08:48 | ZEC | −$8.16 | RESIZE ($1,327→$189) |
| 05-25 06:44 | SOL | −$20.96 | RESIZE ($929→$1,869, under-sized) |
| 05-25 01:50 | LIT | −$20.48 | trader_closed — a4dedd dropped from roster |
| 05-25 00:24–36 | ONDO/NEAR/WLD/FARTCOIN | ~−$17 total | small alt copies churned out at tiny losses + fees |
| 05-24 22:18 | BTC | −$11.18 | trader_closed (old copy-core BTC short, pre-tracker) |
| 05-24 21:40 | TON/ZEC | +$6.98 / +$3.56 | trader-driven, profitable |

- **[THESIS]** The −$133 is **mostly self-inflicted churn**, not bad trader calls: RESIZE close-and-reopen (TON −$50, SOL −$21, ZEC −$8) + a swarm of tiny alt copies + $36 fees. **The stop-loss fired 0 times** (the ATR-floor bug makes it effectively −47% to −55% of margin — see CLAUDE.md open issues). Losses were realized by resizes and trader-exits, never by a protective stop.

---

## 6. Key findings / red flags  **[THESIS]**

1. **fc667 currently contributes NOTHING.** Every one of his crypto bets is <3% conviction (diluted across a $21M multi-asset book heavy in synthetic equities — SP500, oil, gold, AAPL). His only ≥3% coin is BTC (3.40%), which is tracker-excluded. **We are effectively copying 3 traders (a9b95f, feec88, 4f7634) + the 78aa tracker, not 4+1.**
2. **Our book is net-bearish alts** (4 shorts: ETH/SOL/TON/ZEC) vs 2 longs (BTC/HYPE). If the alt rally you expect plays out, the 4 shorts bleed and only HYPE+BTC win. The copied specialists are high-conviction short alts (SOL 100%, ZEC 54%, TON 47%) — that's their alpha, but it fights a broadly-bullish lean.
3. **Biggest bet contradicts a copy trader:** BTC long (tracker) vs a9b95f's 17.8%-conviction BTC short.
4. **No deterministic stop is active.** All 6 ride on the broken stop + trader-exit + resize. The entry/exit overhaul (hard −X% margin stop, kill resize churn) is still pending.

---

## 7. Provenance & caveats
- All **[DATA]** pulled live 2026-05-25 12:25–12:35 UTC. Positions/uPnL/conviction drift with mark price between pulls.
- `userFills` caps at 2000 rows → realized-PnL and entry history beyond the window are **not available** (e.g. fc667's trail is only ~57 min; a9b95f's ~44 days). Lifetime PnL claims are NOT verifiable from this API.
- Conviction uses live mark-priced notional ÷ live accountValue; values near 100% (feec88 SOL) exceed it slightly because marginUsed > accountValue intra-tick.
