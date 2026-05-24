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

# ── Risk limits ── FULL STACK MODE (~$1100 portfolio) ────────────────────────
# Position sizing: 15% per trade = ~$165 on $1100
# Max leaderboard slots: 6 × $165 = $990 deployed (90% stack)
# Remaining 10% = buffer for fees, SL slippage, funding
MAX_OPEN_POSITIONS      = 12            # global ceiling (leaderboard dominates now)
MAX_POSITION_SIZE_PCT   = 0.15          # 15% per position (~$165 on $1100)
DAILY_LOSS_HALT_PCT     = 0.10          # halt at -10% (~$110) — unchanged
MAX_LEVERAGE            = 20
PORTFOLIO_DELTA_MAX     = 0.95          # near-full directional exposure allowed
MIN_POSITION_NOTIONAL   = 50            # reject signals below $50 notional — not worth a slot

# ── Per-strategy slot caps ────────────────────────────────────────────────────
# leaderboard: 6 slots × $165 = $990 max (5 whitelisted traders, ~1-2 open each)
# funding:     1 slot  × $165 = $165 (one carry — avoid fighting copy trades)
# cascade:     2 slots × $165 = $330 (reduced — leaderboard is primary now)
STRATEGY_MAX_POSITIONS = {
    "leaderboard": 10,   # 4 traders × max 3 each = 12 theoretical; 10 covers 95%
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
COPY_MIN_ACCOUNT_AGE_DAYS = 20
COPY_MIN_REALIZED_PNL_USD = 50_000
COPY_MIN_WIN_RATE         = 0.52
COPY_MAX_DRAWDOWN         = 0.35
COPY_MAX_AVG_LEVERAGE     = 20
COPY_MIN_TRADE_COUNT      = 200
COPY_SIZE_SCALE           = 0.005       # 0.5% of their notional (scales to $100 portfolio)
COPY_MAX_LAG_MS           = 3000        # (legacy, fill-stream only) unused by state-based reconcile
# State-based reconcile: poll each trader's NET position this often and mirror only real
# position changes. Traders hold for days, so 45s latency is irrelevant — and this is what
# kills the fee-bleeding fill-stream churn (was reacting to every TWAP/trim fill).
COPY_RECONCILE_INTERVAL_S = 45

# Auto-compound: size off LIVE account equity instead of a frozen PORTFOLIO_USD, so gains
# roll into bigger positions and drawdowns shrink them. Per-position (15%) + delta caps
# still bound risk. PORTFOLIO_USD becomes the initial seed only.
PORTFOLIO_COMPOUND = os.getenv("PORTFOLIO_COMPOUND", "true").lower() == "true"

# Trailing-profit exit on copied positions (the traders only buy-and-hold, so this banks
# gains they'd give back). Arm once a position is +TRAIL_ARM_PCT in price, then exit if it
# retraces TRAIL_GIVEBACK of its peak run. A trail-exited coin is LOCKED from re-entry until
# the trader's net position resets (close/flip) — otherwise reconcile would instantly re-buy.
TRAIL_ENABLED  = os.getenv("TRAIL_ENABLED", "true").lower() == "true"
TRAIL_ARM_PCT  = 0.08    # only start protecting once +8% in price (a real move)
TRAIL_GIVEBACK = 0.30    # exit on a 30% retrace from the peak favorable excursion
COPY_MIN_THEIR_NOTIONAL   = 100         # position-aware tracking handles dedup; $100 = anti-dust
COPY_MAX_POSITIONS_PER_TRADER = 5       # allow up to 5 (a9b95f has 3, fc667 has 6)
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
