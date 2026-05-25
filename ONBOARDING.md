# Onboarding — News-Driven Crypto Signal Brain (LXC) → hl-bot Execution

You are an agent on an **LXC container** (the "news brain"). Your job: turn fast news
into structured crypto trade signals and hand them to a **separate execution bot
(`hl-bot`) on another host**. You build/run the news pipeline here; you do NOT execute
trades here.

## Two-box architecture
- **THIS LXC** = news/signal brain. Has `bafscraper` (an X-feed via a Pi5 = low latency,
  a geopolitical scraper, a YT transcriber, a Claude-API filter, RSS, Telegram-ready).
  Structure exists; **no trading setup yet — build it from scratch on top.** On Tailscale.
- **`hl-bot`** (separate host, LIVE Hyperliquid MAINNET, ~$1,300) = execution: a
  state-based copy bot + risk manager + executor. It will *receive your signals and trade*.
- **Bridge** (coordinate with the hl-bot side): emit signals over Tailscale — e.g. you POST
  to a small intake endpoint the hl-bot exposes, or a shared queue. Schema below.

## ⚠️ FIRST: re-aim the sources to crypto
`bafscraper` is currently **geopolitical + generic X — the WRONG fuel for crypto**
(geopolitical→crypto is weak, noisy, and owned by faster macro algos). Re-point it at
**crypto-specific, tradable events:**
- **Exchange listings** (Binance/Coinbase/Upbit/OKX announce → instant pump of the listed
  coin) — highest-edge classic.
- **Hacks/exploits** (→ dump), **ETF/regulatory** decisions, **token unlocks**, big
  partnerships.
- **Fast crypto-X accounts** (exchange official accounts, breaking-news aggregators, key
  analysts) + **Telegram listing bots / alpha channels**.
Your reusable high-value core = the **Pi5 fast X-feed + Telegram**. The YT transcriber +
geopolitical Claude-filter are ~irrelevant for crypto trading.

## Pipeline to build
1. **Inventory** what `bafscraper` outputs today (format, latency, sources). Document it.
2. **Re-aim sources**: crypto X accounts + Telegram listing/alpha + crypto-news RSS.
3. **Signal extractor**: news item → `{coin, direction(long/short), event_type, confidence,
   source, ts_event, ts_emitted}`. Use the Claude API (you already filter with it) to
   classify: tradable? which ticker? bullish/bearish? confidence 0–1?
4. **Latency**: measure + log event→signal end-to-end at every stage. **This is everything —
   news edge decays in seconds-to-minutes.**
5. **Trigger rules**: map event_type → action (e.g. "confirmed Binance spot listing of $X" →
   long X now; "exploit of protocol Y" → short Y). Only HIGH-confidence, unambiguous events fire.
6. **Bridge to hl-bot**: transport signals to the execution host (coordinate the endpoint/queue).
7. **VALIDATE before live**: shadow-mode — log each signal + the subsequent price move and
   would-be PnL *net of fees* for several days BEFORE any real capital is risked.

## Hard-won lessons on this project (do NOT relitigate)
- Copyable trading edge = slow directional = **regime beta**. Algo/arb/scalp alpha is
  **uncopyable** (latency, fees, hidden hedge legs).
- **Simple TA has no out-of-sample edge net of fees here** — tested exhaustively (MACD looked
  good in-sample on a bull window, failed validation across regimes/coins/params).
- Funding-arb is real but ~9%/yr (≈ USDC staking; not worth the overhead at small size).
- **News is the one promising UNEXPLORED direction** — because it's an *information* edge,
  not an arbitraged price-pattern. That's the whole reason this box exists.
- **"40%/month" is not achievable via edge** — that's leverage/gambling (negative EV) and has
  already bled the live account. Target a *real, validated* edge with honest returns.

## Constraints / honesty
- News trading is a **latency race** (listing bots are fast). Be fast or be selective.
- `hl-bot` is **live mainnet money (~$1,300)** — your signals move real funds. Be conservative;
  validate first; size small (the risk manager caps per-position margin).
- Fees: taker ~0.045%/side → a signal must clear ~0.09% round-trip to be worth acting.
- Keep secrets (Telegram tokens, API keys) in `.env`, never in code or chat.

## Coordinate with the hl-bot side on
- Signal transport (endpoint/queue over Tailscale) + the signal schema above.
- How the executor sizes/risks a news trade (per-position margin cap, leverage).
- Small account (~$1.3k) → small positions; one news trade at a time to start.
