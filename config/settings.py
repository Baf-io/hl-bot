"""
Central config. Values pulled from .env — never hardcode secrets here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Hyperliquid connection ─────────────────────────────────────────────────────
HL_WALLET_ADDRESS   = os.getenv("HL_WALLET_ADDRESS", "")
HL_PRIVATE_KEY      = os.getenv("HL_PRIVATE_KEY", "")
HL_TESTNET          = os.getenv("HL_TESTNET", "true").lower() == "true"

# ── Risk limits ── FULL STACK MODE (~$1120 portfolio) ────────────────────────
# Per-position cap is on MARGIN: MAX_POSITION_SIZE_PCT × portfolio = max margin/position
# (e.g. 15% × $1120 = ~$168 margin; at the trader's leverage that's a larger notional).
MAX_OPEN_POSITIONS      = 12            # global position-count ceiling (copy dominates)
MAX_POSITION_SIZE_PCT   = 0.15          # 15% of portfolio per position, as MARGIN
DAILY_LOSS_HALT_PCT     = 0.10          # halt the bot at -10% live-equity drawdown on the day
MAX_LEVERAGE            = 20
# Net margin-delta cap as a fraction of portfolio. NOTE: 0.95 is intentionally wide —
# it allows a near-fully directional book (these copied traders are often all-short
# majors at once). This is a deliberate risk choice, NOT a neutral hedge. Tighten toward
# ~0.5-0.6 if you want the bot to refuse to stack one-way exposure.
PORTFOLIO_DELTA_MAX     = 0.95
MIN_POSITION_NOTIONAL   = 50            # reject signals below $50 notional — not worth a slot

# ── Per-strategy slot caps (position COUNT, not dollars) ──────────────────────
# leaderboard dominates; the rest are kept small so they don't fight the copy book.
STRATEGY_MAX_POSITIONS = {
    "leaderboard": 10,
    "funding":      1,
    "cascade":      2,
    "squeeze":      2,
    "arb":          2,
}

# ── Strategy toggles ──────────────────────────────────────────────────────────
STRATEGY_FUNDING_CARRY    = os.getenv("STRATEGY_FUNDING_CARRY", "true").lower() == "true"
STRATEGY_LEADERBOARD_COPY = os.getenv("STRATEGY_LEADERBOARD_COPY", "true").lower() == "true"
STRATEGY_CASCADE          = os.getenv("STRATEGY_CASCADE", "true").lower() == "true"
# Phase 2-4 — disabled until calibrated; set to "true" in .env to enable
STRATEGY_OI_SQUEEZE       = os.getenv("STRATEGY_OI_SQUEEZE", "false").lower() == "true"
STRATEGY_STAT_ARB         = os.getenv("STRATEGY_STAT_ARB", "false").lower() == "true"
STRATEGY_MOMENTUM         = os.getenv("STRATEGY_MOMENTUM", "false").lower() == "true"

# ── Funding carry params ──────────────────────────────────────────────────────
FUNDING_ENTRY_THRESHOLD   = 0.0003      # lower bar — catch more opportunities
FUNDING_EXIT_THRESHOLD    = 0.0001
FUNDING_MAX_POSITIONS     = 1           # only 1 carry at a time (capital is small)

# ── Leaderboard copy params ──────────────────────────────────────────────────
# State-based reconcile: poll each trader's NET position this often and mirror only real
# position changes. Traders hold for days, so 45s latency is irrelevant — and this is what
# kills the fee-bleeding fill-stream churn (was reacting to every TWAP/trim fill).
COPY_RECONCILE_INTERVAL_S = 45

# Auto-compound: size off LIVE account equity instead of a frozen PORTFOLIO_USD, so gains
# roll into bigger positions and drawdowns shrink them. Per-position (15%) + delta caps
# still bound risk. PORTFOLIO_USD becomes the initial seed only. Independent of the
# daily-loss halt, which always tracks live equity regardless of this flag.
PORTFOLIO_COMPOUND = os.getenv("PORTFOLIO_COMPOUND", "true").lower() == "true"

# Margin-based sizing cap: cap is on MARGIN (not notional).
# max_notional = (portfolio × MAX_POSITION_SIZE_PCT) × their_leverage
# e.g. $1120 × 15% × 10x = $1,680 notional — but only $168 of real margin committed.
# Prevents blindly copying 50x gamblers; real traders use 5-10x.
COPY_MAX_COPY_LEVERAGE    = 10          # don't mirror leverage above 10x
# Minimum margin (as % of portfolio) we must commit to open a copy position.
# Prevents high-leverage traders from creating slots worth only a few dollars.
# e.g. a9b95f 20x ETH: our_notional=$165 passes $50 notional floor but our_margin=$8 — skip.
# At 1% of $1120 = $11.20 minimum margin.  ZEC/PAXG at ~$17 margin still allowed.
COPY_MIN_MARGIN_PCT       = 0.03        # 3% of portfolio (~$34 on $1120) — no dust; only
                                        # meaningful-margin trades. Excludes e.g. fc667's PAXG
                                        # (1.4% of their acct -> ~$15 margin for us).

# Whitelist: if set, ONLY copy from these trader addresses (comma-separated).
# Leave empty to copy from all qualified traders.
# Use this to focus on your top 1-2 performers after reviewing position_log.
# Example in .env: COPY_TRADER_WHITELIST=0xabc123...,0xdef456...
_whitelist_raw = os.getenv("COPY_TRADER_WHITELIST", "")
COPY_TRADER_WHITELIST: set[str] = (
    {a.strip().lower() for a in _whitelist_raw.split(",") if a.strip()}
    if _whitelist_raw.strip() else set()
)

# ── Cascade params ── more sensitive triggers ─────────────────────────────────
CASCADE_OI_PERCENTILE     = 75          # lowered from 90 — fires more often
CASCADE_FUNDING_THRESHOLD = 0.0002      # more sensitive
CASCADE_MOVE_1H_PCT       = 0.012       # 1.2% move triggers (was 1.8%)
CASCADE_IMBALANCE_MIN     = 0.62        # lower bar (was 0.70)

# ── Monitoring ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")

# ── ntfy phone push (high-signal only: halt / force-close / daily summary) ──────
NTFY_TOPIC          = os.getenv("NTFY_TOPIC", "")          # set in .env to enable
NTFY_SERVER         = os.getenv("NTFY_SERVER", "https://ntfy.sh")
