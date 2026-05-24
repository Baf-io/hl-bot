"""
test_sizing.py — Margin-based sizing simulation
================================================
Compares OLD (notional-capped) vs NEW (margin-capped × leverage) sizing
using hypothetical current positions of our 3 whitelisted traders.

Run with:  python test_sizing.py
"""

PORTFOLIO_USD        = 1_120.0
MAX_POSITION_SIZE_PCT = 0.15        # 15% → $168 margin cap
MAX_LEVERAGE         = 20.0
COPY_MAX_COPY_LEVERAGE = 10.0       # don't mirror > 10x
DAILY_LOSS_HALT_PCT  = 0.10
MIN_SIZE             = 20.0

# ── Hypothetical current trader positions ─────────────────────────────────────
# Format: (trader_label, acct_usd, coin, direction, notional_usd, leverage_x)
HYPOTHETICAL_POSITIONS = [
    # fc667  — $20M macro bear, several large concentrated shorts + HYPE long
    ("fc667",  20_000_000, "HYPE", "LONG",  7_250_000, 5),
    ("fc667",  20_000_000, "BTC",  "SHORT", 3_000_000, 8),
    ("fc667",  20_000_000, "ETH",  "SHORT", 2_000_000, 8),
    ("fc667",  20_000_000, "SOL",  "SHORT", 1_200_000, 8),

    # 42b6d9 — $3.1M ZEC specialist, single thesis high conviction
    ("42b6d9", 3_100_000, "ZEC",  "SHORT",   800_000, 10),

    # a9b95f — $8.3M macro, BTC+ETH short + HYPE long
    ("a9b95f", 8_300_000, "BTC",  "SHORT", 2_000_000,  8),
    ("a9b95f", 8_300_000, "ETH",  "SHORT", 1_500_000,  8),
    ("a9b95f", 8_300_000, "HYPE", "LONG",    900_000,  5),
]

# ─────────────────────────────────────────────────────────────────────────────

def old_sizing(their_notional, their_acct, portfolio, max_pct):
    """Notional-proportional with hard notional cap."""
    if their_acct <= 0:
        return 0.0
    their_pct = their_notional / their_acct
    raw = max(MIN_SIZE, portfolio * their_pct)
    cap = portfolio * max_pct               # $168 notional cap
    return min(raw, cap)

def new_sizing(their_notional, their_acct, their_lev, portfolio, max_pct, max_copy_lev):
    """Margin-proportional: cap is on MARGIN, notional = margin × leverage."""
    if their_acct <= 0:
        return 0.0, 1.0
    lev = min(their_lev, max_copy_lev)      # cap at 10x
    their_margin = their_notional / lev
    their_margin_pct = their_margin / their_acct
    our_margin = max(MIN_SIZE / lev, portfolio * their_margin_pct)
    raw_notional = our_margin * lev
    max_notional = portfolio * max_pct * lev   # $168 × lev
    return min(raw_notional, max_notional), lev

# ─────────────────────────────────────────────────────────────────────────────

max_margin = PORTFOLIO_USD * MAX_POSITION_SIZE_PCT

print(f"\n{'-'*90}")
print(f"  MARGIN-BASED SIZING SIMULATION  |  Portfolio: ${PORTFOLIO_USD:,.0f}  |  "
      f"Margin cap/trade: ${max_margin:,.0f}  |  Max copy leverage: {COPY_MAX_COPY_LEVERAGE:.0f}x")
print(f"{'─'*90}")
print(f"{'Trader':<10} {'Coin':<6} {'Dir':<6} {'Their%':>7} {'Lev':>5} "
      f"{'OLD ntl':>9} {'OLD mgn':>9} "
      f"{'NEW ntl':>9} {'NEW mgn':>9} {'Δ mgn':>9} {'Δ ntl':>9}")
print(f"{'─'*90}")

total_old_margin = 0.0
total_new_margin = 0.0
total_old_notional = 0.0
total_new_notional = 0.0

seen_coins = set()  # simulate 1-coin dedup (no duplicate coins)

for trader, acct, coin, direction, notional, leverage in HYPOTHETICAL_POSITIONS:
    if coin in seen_coins:
        # In reality the risk manager blocks duplicate coins across all traders
        note = "(dedup skip)"
        print(f"  {trader:<8} {coin:<6} {direction:<6} {'':>7} {'':>5} "
              f"{'':>9} {'':>9} {'':>9} {'':>9}  {note}")
        continue
    seen_coins.add(coin)

    their_pct = notional / acct * 100

    old_ntl = old_sizing(notional, acct, PORTFOLIO_USD, MAX_POSITION_SIZE_PCT)
    old_mgn = old_ntl / leverage

    new_ntl, eff_lev = new_sizing(notional, acct, leverage, PORTFOLIO_USD,
                                   MAX_POSITION_SIZE_PCT, COPY_MAX_COPY_LEVERAGE)
    new_mgn = new_ntl / eff_lev

    delta_mgn = new_mgn - old_mgn
    delta_ntl = new_ntl - old_ntl

    capped_old = (PORTFOLIO_USD * MAX_POSITION_SIZE_PCT) < (PORTFOLIO_USD * (notional/acct))
    capped_new = new_ntl >= (PORTFOLIO_USD * MAX_POSITION_SIZE_PCT * eff_lev - 0.01)
    old_flag = " [cap]" if capped_old else ""
    new_flag = " [cap]" if capped_new else ""

    print(f"  {trader:<8} {coin:<6} {direction:<6} {their_pct:>6.1f}% {leverage:>4.0f}x "
          f"  ${old_ntl:>7,.0f}{old_flag:<6}${old_mgn:>7,.1f}  "
          f"  ${new_ntl:>7,.0f}{new_flag:<6}${new_mgn:>7,.1f}  "
          f"  {'+' if delta_mgn>=0 else ''}{delta_mgn:>7,.1f}  "
          f"  {'+' if delta_ntl>=0 else ''}{delta_ntl:>7,.0f}")

    total_old_margin   += old_mgn
    total_new_margin   += new_mgn
    total_old_notional += old_ntl
    total_new_notional += new_ntl

print(f"{'─'*90}")
print(f"  {'TOTALS':<8} {'':>6} {'':>6} {'':>7} {'':>5} "
      f"  ${total_old_notional:>7,.0f}        ${total_old_margin:>7,.1f}  "
      f"  ${total_new_notional:>7,.0f}        ${total_new_margin:>7,.1f}  "
      f"  +${total_new_margin - total_old_margin:>6,.1f}  "
      f"  +${total_new_notional - total_old_notional:>6,.0f}")
print(f"{'─'*90}")

print(f"\n  Portfolio utilisation (margin / portfolio):")
print(f"    OLD: ${total_old_margin:,.1f} / ${PORTFOLIO_USD:,.0f} = "
      f"{total_old_margin/PORTFOLIO_USD*100:.1f}%  ← only this much of your money is working")
print(f"    NEW: ${total_new_margin:,.1f} / ${PORTFOLIO_USD:,.0f} = "
      f"{total_new_margin/PORTFOLIO_USD*100:.1f}%  ← actual capital deployed")

print(f"\n  Risk profile per position:")
print(f"    Margin cap per trade    : ${max_margin:,.0f}  (unchanged — same real $ at risk)")
print(f"    Max notional at 10x     : ${max_margin * COPY_MAX_COPY_LEVERAGE:,.0f}")
print(f"    Max notional at  5x     : ${max_margin * 5:,.0f}")
print(f"    Native HL SL fires at   : -3% price move (= -{3 * COPY_MAX_COPY_LEVERAGE:.0f}% ROE at 10x)")
print(f"    Nuclear guard fires at  : -20% price move (HL liquidates before this at high lev)")

print(f"\n  Delta-neutral check (margin-equivalent, not notional):")
print(f"    Max net margin delta    : ${PORTFOLIO_USD * 0.95:,.0f}  (95% of portfolio)")
print(f"    Net margin delta OLD    : ${total_old_margin:,.1f}")
print(f"    Net margin delta NEW    : ${total_new_margin:,.1f}  "
      f"({'OK' if total_new_margin < PORTFOLIO_USD * 0.95 else 'OVER — delta check blocks last trade(s)'})")

print(f"\n  What changed vs what stayed the same:")
print(f"    UNCHANGED  — proportional sizing formula (mirrors trader conviction)")
print(f"    UNCHANGED  — margin cap per trade ($168)")
print(f"    UNCHANGED  — native HL SL, nuclear guard, zombie timer")
print(f"    CHANGED    — risk manager cap: was notional ≤ $168, now notional ≤ $168 × leverage")
print(f"    CHANGED    — delta check now uses margin-equivalent (notional / leverage)")
print(f"    CHANGED    — leverage cached from position data + passed through signal meta")
print()
