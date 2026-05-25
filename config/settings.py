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

# ── Risk limits ── RISK-POLICY-CONTRACT CLAMP (2026-05-25) ───────────────────
# Hard institutional limits. ≤3x, MAX 1 open position, kill-switch -3%/day & -5%/week,
# no averaging, PROBE-only until a source is KEEP-validated (B). See ENTRY_EXIT_PLAN / contract.
MAX_OPEN_POSITIONS      = 1             # CONTRACT: max 1 open position
MAX_POSITION_SIZE_PCT   = 0.05          # tight per-pos margin cap (probe cap dominates anyway)
DAILY_LOSS_HALT_PCT     = 0.03          # CONTRACT: halt new entries at -3%/day realized
WEEKLY_LOSS_HALT_PCT    = 0.05          # CONTRACT: halt at -5%/week realized
MAX_LEVERAGE            = 3             # CONTRACT: max 3x
PORTFOLIO_DELTA_MAX     = 0.50          # tightened (1 small position anyway)
MIN_POSITION_NOTIONAL   = 20            # lowered so a ≤$50 probe clears the floor
PROBE_MAX_NOTIONAL      = 50            # CONTRACT: WATCH/unvalidated source → ≤$50 exposure (probe)
PROBE_MAX_RISK_USD      = 5             # CONTRACT: ≤$5 risk on a probe

# ── Per-strategy slot caps (CONTRACT: only leaderboard, max 1) ────────────────
STRATEGY_MAX_POSITIONS = {
    "leaderboard": 1,
    "funding":      0,
    "cascade":      0,
    "squeeze":      0,
    "arb":          0,
}

# ── Strategy toggles ──────────────────────────────────────────────────────────
STRATEGY_FUNDING_CARRY    = os.getenv("STRATEGY_FUNDING_CARRY", "false").lower() == "true"   # CLAMP: off (contract = leaderboard-only)
STRATEGY_LEADERBOARD_COPY = os.getenv("STRATEGY_LEADERBOARD_COPY", "true").lower() == "true"
STRATEGY_CASCADE          = os.getenv("STRATEGY_CASCADE", "false").lower() == "true"          # CLAMP: off
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

# ── Scale-out profit-taking on copy positions (supersedes the flat trail) ───────
# Two tranches, VOLATILITY-NORMALIZED (daily-ATR) and LEVERAGE-AWARE:
#   • TP1 banks SCALEOUT_TP1_FRACTION of the position when the favorable price
#     excursion clears  max(MIN_ATR_MULT × dailyATR%,  TP1_MARGIN_RET / leverage).
#       - the ATR term is a noise floor: a move must be real *for this coin's vol*
#         (fixes the flat +8% that's ~3 ATRs on BTC but <1 ATR on a small-cap);
#       - the TP1_MARGIN_RET/leverage term is where LEVERAGE enters: a 10x position
#         reaches +15% return-on-margin at a smaller price move than a 2x one, so it
#         banks sooner. We trigger on the LARGER of the two (never trim on noise).
#   • The runner (the rest) rides an ATR trailing stop: exit on a
#     RUNNER_TRAIL_ATR × dailyATR% retrace from peak — keeps us in the trader's
#     macro thesis (the actual edge) instead of dumping 100% on a wiggle.
# When enabled this REPLACES the flat TRAIL_* logic for copy positions.
SCALEOUT_ENABLED        = os.getenv("SCALEOUT_ENABLED", "true").lower() == "true"
# Tightened for MORE ACTIVE exits: we adopt positions at the trader's EXTENDED entry
# (copy-lag) with no profit cushion, so banking forward gains beats riding their thesis.
SCALEOUT_TP1_FRACTION   = 0.60   # bank 60% at the first tranche (was 0.50)
SCALEOUT_TP1_MARGIN_RET = 0.10   # ...target +10% return on MARGIN (was 0.15)
SCALEOUT_MIN_ATR_MULT   = 0.5    # ...noise floor lowered to 0.5 × daily ATR (was 0.8)
SCALEOUT_RUNNER_TRAIL_ATR = 1.0  # runner exits on a 1.0 × daily-ATR retrace from peak (was 1.5)
ATR_PERIOD              = 14     # daily candles used for the ATR estimate

# ── EXIT ENGINE: HARD MARGIN STOP + BANK-AND-RIDE (see docs/ENTRY_EXIT_PLAN.md) ──
# Overhauled 2026-05-25 after the live audit showed the old `max(margin, ATR)` stop was
# really -47% to -62% of margin and fired 0 times; losses came from RESIZE churn instead.
#   • STOP  — HARD: full-exit the instant loss hits STOP_LOSS_MARGIN_PCT of margin
#             (price move = pct / leverage). NO ATR floor — the floor is gone; the dollar
#             cap is enforced. Applies EVEN IF ATR is unavailable (atr only gates the trail).
#   • BANK  — bank BANK_FRACTION of the position at +BANK_AT_MARGIN_RET margin return
#             (= +2R when stop is -20%), then let the runner ride. ("take profits sooner")
#   • RIDE  — runner trails: once it clears RIDE_ACTIVATE_ATR × dailyATR%, exit on a
#             RIDE_GIVEBACK_ATR × dailyATR% retrace from peak.
RIDE_WINNERS_ENABLED   = os.getenv("RIDE_WINNERS_ENABLED", "true").lower() == "true"
STOP_LOSS_MARGIN_PCT   = 0.09   # HARD: cut a loser at -9% of its margin (tightened from -20%)
RIDE_ACTIVATE_ATR      = 1.0    # winner must clear +1× daily-ATR before the trail engages
RIDE_GIVEBACK_ATR      = 1.0    # then exit on a 1.0× daily-ATR retrace from peak (bank closer)
BANK_FRACTION          = 0.50   # bank 50% of the position at the target…
BANK_AT_MARGIN_RET     = 0.25   # …i.e. +25% return on margin (R = 25/9 ≈ 2.78)
# Specialist conviction coins (a trader's `specialty` coin) ride WITH the trader — give them
# room instead of the tight -9% noise-chop. Their stop = WIDER of (-9% margin, this×ATR), so
# we don't get whipsawed out of an elite trader's high-conviction multi-day hold (e.g. feec88
# +$411k SOL). The bank (+25%) + following the trader's exit are the primary controls here.
SPECIALIST_STOP_ATR    = 1.0    # specialist coins stop no tighter than 1.0× daily-ATR

# Vol-scaled leverage: tune each coin's copy leverage to its ATR so the -20% margin stop
# always lands ≥ STOP_NOISE_ATR daily-ATRs away (a real move, not noise). Volatile coins get
# smaller/safer positions; the dollar stop is identical everywhere. Textbook vol-targeting.
VOL_SCALED_LEV         = os.getenv("VOL_SCALED_LEV", "true").lower() == "true"
STOP_NOISE_ATR         = 0.5    # stop distance must be >= 0.5× daily-ATR → lev_cap = STOP/(0.5·atr)

# Churn controls (the -$133 was mostly RESIZE close-and-reopen + fleeting alt copies):
RESIZE_ENABLED            = os.getenv("RESIZE_ENABLED", "false").lower() == "true"  # phase-1: OFF (no close+reopen)
COPY_ENTRY_DEBOUNCE_TICKS = int(os.getenv("COPY_ENTRY_DEBOUNCE_TICKS", "2"))        # (legacy path) trader must hold a NEW coin this many ticks before we copy

# ── Zero-copy-lag: FRESH-ENTRY-ONLY (Stage 2) ───────────────────────────────────
# Only OPEN a coin when we OBSERVE the trader open it (flat→position, or a flip) AND price is
# still within FRESH_ENTRY_MAX_ATR daily-ATRs of where they opened. NEVER adopt a position they
# already hold — that stale adoption at a worse price is the copy-lag leak (cost us ETH −$15.61
# while the source sat +$560k; SOL −3% vs his +74%). First poll after restart = baseline only
# (don't adopt anything stale on boot). Grandfathered held positions are unaffected (they ride).
FRESH_ENTRY_ONLY     = os.getenv("FRESH_ENTRY_ONLY", "true").lower() == "true"
FRESH_ENTRY_MAX_ATR  = 0.5    # max distance from the trader's observed open price to still enter
FRESH_ENTRY_EXPIRE_S = 600    # a fresh-open opportunity expires after this long if unfilled

# ── ADD-MIRRORING (scale IN when a trader adds to a held position, TREND-confirmed) ──────
# Analysis of the roster's recent adds/trims: their ADDS have forward edge (esp. in trends —
# f83858 BTC adds 90%/6h, 69b HYPE adds ~100%); their TRIMS are early/negative-edge (they cut
# winners). So we mirror ADDS ONLY (scale in), never trims — exits stay ours (bank/ride/close).
# Trend-gated because the add-edge is regime-dependent: only add when the coin's daily trend
# aligns with the position (notify + enter). Bounded by the per-position margin cap.
ADD_MIRROR_ENABLED = os.getenv("ADD_MIRROR_ENABLED", "false").lower() == "true"  # CLAMP: no averaging
ADD_MIN_FRAC       = 0.10   # the trader's notional must grow >=10% poll-over-poll to trigger
ADD_STEP_MAX       = 0.50   # our add = min(their %-increase, this) of our current size, per step
TREND_SMA_DAYS     = 5      # daily-SMA window for the trend filter (close vs SMA = up/down)

# ── PROBABLE-TP SCALP exit ("buy when they add → sell at +1% → repeat") ──────────────
# Backtest: their adds run +1.5% median in 6h; a +1% TP hits 80% of the time. We take that
# as the profit roof — FULL exit at +COPY_TP_PCT, paired with a TIGHT SL (a +1% TP needs a
# small SL or the wins can't cover the losses). NO trail-lock on TP/SL → we re-enter on the
# trader's NEXT add (the "repeat"). Trend-gated entries/adds keep it on the right side.
# Supersedes the +25% bank / ride-trail for copy positions (those constants now unused).
COPY_TP_PCT        = 0.01   # sell at +1% favorable price move (≈ the 80%-probable roof)
COPY_SL_PCT        = 0.03   # cut at -3% adverse (3× the +1% TP — wide berth for the ~80% WR; 1:3 R:R, breakeven 75%)
COPY_REOPEN_ON_ADD = os.getenv("COPY_REOPEN_ON_ADD", "false").lower() == "true"  # CLAMP: no re-add
# Scalp leverage: FIXED leverage for copy entries (overrides the trader's lev + the old vol-cap),
# so a +1% TP is meaningful $ on our small book. At 12x: +1%TP=+12% margin, -1.5%SL=-18% margin,
# liq ≈ -7.9% price (the 1.5% soft-SL has buffer). Bounded by daily-loss-halt. NOTE: soft SL is
# polled every 45s — going much above ~12x risks a fast move gapping past it (would need native stops).
SCALP_LEVERAGE     = int(os.getenv("SCALP_LEVERAGE", "3"))   # CLAMP: contract max 3x

# ── Tracker coins: reserved for the manual "lev-guy" tracker, OFF-LIMITS to copy ─
# The copy engine never syncs, manages, or desires these — they're a separate manual
# (isolated-margin) sleeve mirroring trader 0x78aa… So a restart won't auto-close the
# tracker, and the copier won't open an opposing position that nets against it on HL.
TRACKER_COINS = {c.strip().upper() for c in
                 os.getenv("TRACKER_COINS", "BTC").split(",") if c.strip()}

# ── MANUAL coins: the USER trades these by hand — the copier must NEVER touch them ──
# (e.g. a discretionary HYPE short the user opened). Excluded from sizing, desired,
# startup sync, and fresh-detection — same as tracker coins. Clear when the bot should
# resume managing them. 2026-05-25: HYPE = user's manual discretionary short.
MANUAL_COINS = {c.strip().upper() for c in
                os.getenv("MANUAL_COINS", "").split(",") if c.strip()}   # HYPE unblocked 2026-05-25 (user closed manual short)
# Coins the COPIER skips entirely (tracker sleeve + user-manual). The 78aa tracker still
# uses TRACKER_COINS for what IT trades; this is only the copier's skip set.
COPIER_SKIP_COINS = TRACKER_COINS | MANUAL_COINS

# ── Lev-tracker sleeve: mirror ONE trader's DIRECTION on TRACKER_COINS, ISOLATED ─
# Walled off from the copy engine. Holds a fixed TRACKER_MARGIN_USD of ISOLATED
# margin per tracker coin in the SAME direction as the source trader; follows his
# open/close/flip (not his size). Isolated → max loss per coin = the margin staked.
TRACKER_ENABLED      = os.getenv("TRACKER_ENABLED", "false").lower() == "true"  # CLAMP: 40x tracker violates 3x + 1-pos
TRACKER_DRY_RUN      = os.getenv("TRACKER_DRY_RUN", "false").lower() == "true"  # log only
TRACKER_SOURCE_ADDR  = os.getenv("TRACKER_SOURCE_ADDR",
                                 "0x78aa6328eae8028a089c35d2819f79c78de2a7e5")  # the 40x guy
TRACKER_MARGIN_USD   = float(os.getenv("TRACKER_MARGIN_USD", "200"))   # margin staked per coin (applies to 78aa's NEXT entry; current open BTC pos rides untouched)
TRACKER_MAX_LEV      = int(os.getenv("TRACKER_MAX_LEV", "40"))          # cap (he runs ~40x)
TRACKER_POLL_S       = int(os.getenv("TRACKER_POLL_S", "10"))           # direction poll cadence (10s: tight follow on his open/close/flip; 2 API calls/tick = trivial rate-limit load)
TRACKER_TP_PCT       = 0.01    # sell the isolated BTC sleeve at +1% favorable, then re-sync re-buys (sell-on-1%-repeat)
TRACKER_REOPEN_COOLDOWN_S = 300  # after a TP, wait this long before re-opening (bounds 40x churn)
ATR_REFRESH_S           = 3600   # re-fetch a coin's ATR at most this often (it moves slowly)
COPY_MIN_THEIR_NOTIONAL   = 100         # position-aware tracking handles dedup; $100 = anti-dust
COPY_MAX_POSITIONS_PER_TRADER = 5       # allow up to 5 (a9b95f has 3, fc667 has 6)
# Margin-based sizing cap: cap is on MARGIN (not notional).
# max_notional = (portfolio × MAX_POSITION_SIZE_PCT) × their_leverage
# e.g. $1120 × 15% × 10x = $1,680 notional — but only $168 of real margin committed.
# Prevents blindly copying 50x gamblers; real traders use 5-10x.
COPY_MAX_COPY_LEVERAGE    = 3           # CLAMP: contract max 3x
# Minimum margin (as % of portfolio) we must commit to open a copy position.
# Prevents high-leverage traders from creating slots worth only a few dollars.
# e.g. a9b95f 20x ETH: our_notional=$165 passes $50 notional floor but our_margin=$8 — skip.
# At 1% of $1120 = $11.20 minimum margin.  ZEC/PAXG at ~$17 margin still allowed.
COPY_MIN_MARGIN_PCT       = 0.03        # (legacy proportional floor — unused by equal-weight model)

# ── Equal-weight sizing (decoupled from the trader's account size) ──────────────
# Old proportional model sized our position to THEIR margin-% of THEIR account, so a big
# fund's genuine bet (small % of a $15M acct) sized to dust for us. New model:
#   1. Only copy a coin if it's a real conviction bet FOR THE TRADER (their margin on it
#      >= COPY_MIN_CONVICTION_PCT of their account). This ALSO gates who "votes" on a
#      coin's direction, so tiny dabbles can't spuriously contest a coin.
#   2. Equal-weight OUR capital to COPY_TARGET_DEPLOY across the chosen coins, capped at
#      MAX_POSITION_SIZE_PCT each. Deploys fully regardless of the trader's account size.
COPY_MIN_CONVICTION_PCT   = 0.05        # their position must be >=5% of their acct to copy/vote (raised 0.03→0.05: fewer, stronger copies, less churn)
COPY_TARGET_DEPLOY        = 0.85        # (legacy equal-weight÷n model — superseded by COPY_POSITION_PCT)
# ── Active-generalist sizing (multi-coin swing traders + per-trader weight) ─────────
# Each copy position = FIXED fraction of the copy budget × the source trader's weight, NOT
# equal-weight÷n (which collapsed to dust with many-coin generalists and risked hitting a
# margin wall). Copy budget = equity MINUS the tracker reserve (so the isolated BTC sleeve
# and the copy book never fight for margin). Book is bounded by MAX_OPEN_POSITIONS + the risk
# manager's margin/delta caps, which gracefully stop new entries when full (no crash).
COPY_POSITION_PCT         = 0.12        # margin per FULL-weight position = 12% of copy budget (cap MAX_POSITION_SIZE_PCT)

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
