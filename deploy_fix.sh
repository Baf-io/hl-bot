#!/usr/bin/env bash
# deploy_fix.sh
# Run this on the VPS to apply the full fix in one shot.
# Usage: bash deploy_fix.sh
set -e

WALLET="0xb33040b2618Ffb4AfAfbD1afDfEff29C3D08D3C8"
PRIVKEY="0x4100978239db5e72e763bd8b78092d2fb677dfcf029d54a5fc9b21b62a5807e7"

echo "=== STEP 1: Stop bot ==="
sudo systemctl stop hl-bot

echo "=== STEP 2: Close GMT + WLFI dust positions ==="
cd ~/hl-bot && python3 - <<'PYEOF'
import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants

WALLET   = "0xb33040b2618Ffb4AfAfbD1afDfEff29C3D08D3C8"
PRIVKEY  = "0x4100978239db5e72e763bd8b78092d2fb677dfcf029d54a5fc9b21b62a5807e7"

wallet = eth_account.Account.from_key(PRIVKEY)
exc    = Exchange(wallet, constants.MAINNET_API_URL, account_address=WALLET)
info   = Info(constants.MAINNET_API_URL, skip_ws=True)
state  = info.user_state(WALLET)

CLOSE_COINS = {"GMT", "WLFI"}
for p in state.get("assetPositions", []):
    pos  = p["position"]
    szi  = float(pos["szi"])
    coin = pos["coin"]
    ep   = float(pos.get("entryPx") or 0)
    ntl  = abs(szi) * ep
    if coin in CLOSE_COINS and szi != 0:
        print(f"Closing {coin}: szi={szi:.4f} notional=${ntl:.2f}")
        r = exc.market_open(coin, szi < 0, abs(szi), slippage=0.01)
        print(f"  Result: {r.get('status', r)}")
    elif coin not in CLOSE_COINS and szi != 0:
        print(f"Keeping {coin}: {'+' if szi>0 else '-'} ${ntl:.0f}")

print("Done closing unwanted positions.")
PYEOF

echo "=== STEP 3: Update whitelist (remove f51763) ==="
# Keep only: fc667 + 42b6d9 + a9b95f
ENV_FILE=~/hl-bot/.env

# Remove old whitelist line and add new one
grep -v "^COPY_TRADER_WHITELIST=" "$ENV_FILE" > /tmp/env_tmp && mv /tmp/env_tmp "$ENV_FILE"
echo "COPY_TRADER_WHITELIST=0xfc667adba8d4837586078f4fdcdc29804337ca06,0x42b6d907f36255d48f70db8b4a2684088a162634,0xa9b95f2a2e7ef219021efc5c04c32761b8553bbd" >> "$ENV_FILE"

echo "New whitelist:"
grep "COPY_TRADER_WHITELIST" "$ENV_FILE"

echo "=== STEP 4: Pull latest code ==="
cd ~/hl-bot && git pull

echo "=== STEP 5: Restart bot ==="
sudo systemctl start hl-bot
sleep 3
sudo systemctl status hl-bot --no-pager | tail -5

echo ""
echo "=== DONE === Bot restarted with:"
echo "  - GMT + WLFI closed"
echo "  - Whitelist: fc667 + 42b6d9 + a9b95f (f51763 removed)"
echo "  - Position-aware TWAP dedup"
echo "  - Zombie timer: 72h"
echo "  - Nuclear loss: -20%"
echo ""
echo "Tail logs with: sudo journalctl -u hl-bot -f"
