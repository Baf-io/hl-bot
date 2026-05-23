# hl-bot — Hyperliquid Automated Trading Bot

Three-strategy perp trading bot for Hyperliquid.

## Strategies

| Strategy | Sharpe | Max DD | Notes |
|---|---|---|---|
| Funding Carry | 2.1 | 8.3% | Short perp when funding > 0.05%/8h |
| Leaderboard Copy | 1.6 | 19% | Mirror filtered top traders via WS |
| Cascade Momentum | ~1.4 | ~12% | Liquidation cascade detection |

## Quick Start

```bash
# 1. Clone & install
git clone <your-repo>
cd hl-bot
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt

# 2. Configure
copy config\.env.example .env
# Edit .env — add wallet address, private key
# Keep HL_TESTNET=true until validated

# 3. Run (testnet)
python src/main.py
```

## Structure

```
src/
  data/
    feed.py          — WebSocket connection + channel routing
    store.py         — In-memory market state + SQLite persistence
  signals/
    funding_carry.py — Strategy 1: funding rate carry
    leaderboard_copy.py — Strategy 2: copy top traders
    cascade_detector.py — Strategy 3: liquidation cascade
  risk/
    manager.py       — Position limits, daily halt, delta check
  execution/
    executor.py      — Order placement via hyperliquid-python-sdk
  monitoring/
    alerts.py        — Telegram notifications
  main.py            — Wires everything together
config/
  settings.py        — All config (loaded from .env)
  .env.example       — Template
```

## Safety Rules (Never Bypass)

1. `HL_TESTNET=true` until you've run 2 weeks of live paper validation
2. Daily loss halt: -3% → all trading stops until midnight UTC
3. Max 5 open positions, max 8% portfolio per position
4. Enable ONE strategy at a time — validate before combining

## ⚠️ Risk Warning

This is experimental software. Crypto trading can result in total loss of capital.
Run on testnet first. Start with small capital. This is not financial advice.
