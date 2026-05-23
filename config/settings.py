"""
Central config. Values pulled from .env — never hardcode secrets here.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ── Hyperliquid connection ─────────────────────────────────────────────────────
HL_WALLET_ADDRESS   = os.getenv("HL_WALLET_ADDRESS", "")
HL_PRIVATE_KEY      = os.getenv("HL_PRIVATE_KEY", "")
HL_TESTNET          = os.getenv("HL_TESTNET", "true").lower() == "true"   # ALWAYS start on testnet

# ── Risk limits ───────────────────────────────────────────────────────────────
MAX_OPEN_POSITIONS      = 5
MAX_POSITION_SIZE_PCT   = 0.08      # 8% of portfolio per position
DAILY_LOSS_HALT_PCT     = 0.03      # halt all trading if -3% day
MAX_LEVERAGE            = 10        # hard cap, override per-strategy
PORTFOLIO_DELTA_MAX     = 0.15      # net delta can't exceed 15% of portfolio

# ── Strategy toggles ──────────────────────────────────────────────────────────
STRATEGY_FUNDING_CARRY    = os.getenv("STRATEGY_FUNDING_CARRY", "true").lower() == "true"
STRATEGY_LEADERBOARD_COPY = os.getenv("STRATEGY_LEADERBOARD_COPY", "false").lower() == "true"
STRATEGY_CASCADE          = os.getenv("STRATEGY_CASCADE", "false").lower() == "true"

# ── Funding carry params ──────────────────────────────────────────────────────
FUNDING_ENTRY_THRESHOLD   = 0.0005  # 0.05% per 8h
FUNDING_EXIT_THRESHOLD    = 0.0002  # exit when funding drops below this
FUNDING_MAX_POSITIONS     = 3       # max simultaneous carry positions

# ── Leaderboard copy params ───────────────────────────────────────────────────
COPY_MIN_ACCOUNT_AGE_DAYS = 45
COPY_MIN_REALIZED_PNL_USD = 150_000
COPY_MIN_WIN_RATE         = 0.58
COPY_MAX_DRAWDOWN         = 0.22
COPY_MAX_AVG_LEVERAGE     = 12
COPY_MIN_TRADE_COUNT      = 500
COPY_SIZE_SCALE           = 0.01    # copy at 1% of their notional (tune to your capital)
COPY_MAX_LAG_MS           = 500     # discard signal if we're >500ms behind

# ── Cascade params ────────────────────────────────────────────────────────────
CASCADE_OI_PERCENTILE     = 90      # OI must be above 90th pct (30d)
CASCADE_FUNDING_THRESHOLD = 0.0003
CASCADE_MOVE_1H_PCT       = 0.018   # 1.8% move in 1h
CASCADE_IMBALANCE_MIN     = 0.70    # bid/ask imbalance

# ── Monitoring ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
