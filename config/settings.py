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

# ── Risk limits ── SCALED FOR $260 + HIGH SIGNAL VOLUME ──────────────────────
MAX_OPEN_POSITIONS      = 15            # enough slots for copy trading volume
MAX_POSITION_SIZE_PCT   = 0.07          # 7% per position (~$18 on $260)
DAILY_LOSS_HALT_PCT     = 0.20          # halt at -20% ($52 on $260)
MAX_LEVERAGE            = 20            # allow higher leverage for leaderboard copies
PORTFOLIO_DELTA_MAX     = 0.90          # allow high directional exposure

# ── Strategy toggles ──────────────────────────────────────────────────────────
STRATEGY_FUNDING_CARRY    = os.getenv("STRATEGY_FUNDING_CARRY", "true").lower() == "true"
STRATEGY_LEADERBOARD_COPY = os.getenv("STRATEGY_LEADERBOARD_COPY", "true").lower() == "true"
STRATEGY_CASCADE          = os.getenv("STRATEGY_CASCADE", "true").lower() == "true"

# ── Funding carry params ──────────────────────────────────────────────────────
FUNDING_ENTRY_THRESHOLD   = 0.0003      # lower bar — catch more opportunities
FUNDING_EXIT_THRESHOLD    = 0.0001
FUNDING_MAX_POSITIONS     = 1           # only 1 carry at a time (capital is small)

# ── Leaderboard copy params ── loosened for more signals ─────────────────────
COPY_MIN_ACCOUNT_AGE_DAYS = 20          # lowered from 45
COPY_MIN_REALIZED_PNL_USD = 50_000      # lowered from 150k
COPY_MIN_WIN_RATE         = 0.52        # lowered from 0.58
COPY_MAX_DRAWDOWN         = 0.35        # more lenient
COPY_MAX_AVG_LEVERAGE     = 20          # allow higher leverage traders
COPY_MIN_TRADE_COUNT      = 200         # lowered from 500
COPY_SIZE_SCALE           = 0.005       # 0.5% of their notional (scales to $100 portfolio)
COPY_MAX_LAG_MS           = 300         # tighter — only copy if fast enough

# ── Cascade params ── more sensitive triggers ─────────────────────────────────
CASCADE_OI_PERCENTILE     = 75          # lowered from 90 — fires more often
CASCADE_FUNDING_THRESHOLD = 0.0002      # more sensitive
CASCADE_MOVE_1H_PCT       = 0.012       # 1.2% move triggers (was 1.8%)
CASCADE_IMBALANCE_MIN     = 0.62        # lower bar (was 0.70)

# ── Monitoring ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
