"""
Lev-tracker sleeve — mirror ONE source trader's DIRECTION on TRACKER_COINS in
ISOLATED margin, walled off from the copy engine.

Why separate from the copier:
  - The copy engine is STATE-BASED, conviction-gated, equal-weight, cross-margin,
    and deliberately caps leverage at 10x. This sleeve does the opposite: it
    follows a single high-leverage momentum trader (default 0x78aa…, ~40x) 1:1 on
    DIRECTION, in ISOLATED margin, with a fixed margin stake per coin. Isolation
    means the most this sleeve can ever lose on a coin is the margin it staked —
    it can never touch the cross-margin copy book.
  - TRACKER_COINS are excluded from the copier (settings + executor + reconcile),
    so the two sleeves never fight over the same coin's single net HL position.

Behaviour (per coin in TRACKER_COINS, every TRACKER_POLL_S):
  - Read the source trader's net position → (direction, leverage).
  - Read ours. If they match, do nothing.
  - If the source OPENED  → open ours (isolated, his leverage capped at MAX_LEV,
                            TRACKER_MARGIN_USD of margin).
  - If the source CLOSED  → close ours (reduce-only).
  - If the source FLIPPED → close ours, then open the new side.
We follow his open/close/flip, NOT his size — a bounded, fixed-stake shadow.
"""
from __future__ import annotations

import asyncio
import time

import eth_account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from loguru import logger

from config import settings


class LevTracker:
    def __init__(self, alerter=None):
        self._alert = alerter
        self._ex: Exchange | None = None
        self._info: Info | None = None
        self._meta_sz: dict[str, int] = {}
        self.src = settings.TRACKER_SOURCE_ADDR
        self.coins = settings.TRACKER_COINS
        self._cooldown: dict[str, float] = {}   # coin -> monotonic ts of last TP (re-open cooldown)

    def _connect(self):
        wallet = eth_account.Account.from_key(settings.HL_PRIVATE_KEY)
        base = constants.TESTNET_API_URL if settings.HL_TESTNET else constants.MAINNET_API_URL
        self._ex = Exchange(wallet, base, account_address=settings.HL_WALLET_ADDRESS)
        self._info = Info(base, skip_ws=True)
        self._meta_sz = {u["name"]: u["szDecimals"] for u in self._info.meta()["universe"]}
        logger.info(
            f"[LevTracker] tracking {self.src[:10]}… on {sorted(self.coins)} | "
            f"${settings.TRACKER_MARGIN_USD:.0f} isolated/coin, ≤{settings.TRACKER_MAX_LEV}x, "
            f"poll {settings.TRACKER_POLL_S}s{' [DRY-RUN]' if settings.TRACKER_DRY_RUN else ''}"
        )

    @staticmethod
    def _net(state: dict, coin: str) -> tuple[str | None, float]:
        """(direction, leverage) of `coin` in a clearinghouseState, or (None, 0)."""
        for p in state.get("assetPositions", []):
            pos = p["position"]
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi", 0))
            if szi == 0:
                return None, 0.0
            return ("long" if szi > 0 else "short"), float(pos["leverage"]["value"])
        return None, 0.0

    def _ok(self, resp: dict) -> bool:
        """The SDK returns status=ok even on errors — verify the inner fill/status."""
        try:
            st = resp["response"]["data"]["statuses"][0]
            if "error" in st:
                logger.error(f"[LevTracker] order error: {st['error']}")
                return False
            return True
        except Exception:
            logger.error(f"[LevTracker] unparseable order response: {resp}")
            return False

    def _open(self, coin: str, direction: str, lev: float):
        lev = int(max(1, min(lev or settings.TRACKER_MAX_LEV, settings.TRACKER_MAX_LEV)))
        px = float(self._info.all_mids()[coin])
        notional = settings.TRACKER_MARGIN_USD * lev
        size = round(notional / px, self._meta_sz.get(coin, 4))
        is_buy = direction == "long"
        if settings.TRACKER_DRY_RUN:
            logger.info(f"[LevTracker] DRY would OPEN {direction} {coin} {size} (~${notional:,.0f} @ {lev}x)")
            return
        self._ex.update_leverage(lev, coin, False)  # False = isolated
        resp = self._ex.market_open(coin, is_buy, size, None, 0.01)
        if self._ok(resp):
            logger.warning(f"[LevTracker] OPENED {direction} {coin} {size} (~${notional:,.0f} @ {lev}x isolated)")
            self._notify(f"📈 Lev-tracker OPEN {direction} {coin} ~${notional:,.0f} @ {lev}x")

    def _close(self, coin: str):
        if settings.TRACKER_DRY_RUN:
            logger.info(f"[LevTracker] DRY would CLOSE {coin}")
            return
        resp = self._ex.market_close(coin, slippage=0.01)
        if resp and self._ok(resp):
            logger.warning(f"[LevTracker] CLOSED {coin}")
            self._notify(f"📉 Lev-tracker CLOSE {coin} (source exited)")

    def _notify(self, msg: str):
        if self._alert:
            try:
                asyncio.create_task(self._alert.send(msg))
            except Exception:
                pass

    @staticmethod
    def _our_pos(state: dict, coin: str):
        """(direction, entryPx, szi) of our `coin` position, or None."""
        for p in state.get("assetPositions", []):
            pos = p["position"]
            if pos.get("coin") != coin:
                continue
            szi = float(pos.get("szi", 0))
            if szi == 0:
                return None
            return ("long" if szi > 0 else "short"), float(pos.get("entryPx", 0)), szi
        return None

    async def tick(self):
        src = self._info.user_state(self.src)
        mine = self._info.user_state(settings.HL_WALLET_ADDRESS)
        mids = self._info.all_mids()
        now = time.monotonic()
        for coin in self.coins:
            # ── PROBABLE-TP: bank the isolated sleeve at +TRACKER_TP_PCT favorable ──
            ours = self._our_pos(mine, coin)
            if ours:
                d, entry, _ = ours
                px = float(mids.get(coin, 0)) or entry
                fav = (px - entry) / entry if d == "long" else (entry - px) / entry
                if entry > 0 and fav >= settings.TRACKER_TP_PCT:
                    logger.warning(f"[LevTracker] 🎯 TP {coin} {d} +{fav:.2%} — banking, cooldown {settings.TRACKER_REOPEN_COOLDOWN_S}s")
                    self._notify(f"🎯 Lev-tracker TP {coin} +{fav:.2%} — banked")
                    self._close(coin); self._cooldown[coin] = now
                    continue                      # don't re-open same tick

            their_dir, their_lev = self._net(src, coin)
            our_dir, _ = self._net(mine, coin)
            if their_dir == our_dir:
                continue  # in sync (incl. both flat)
            if our_dir is not None:               # close stale side first (handles flip + exit)
                self._close(coin)
            if their_dir is not None:             # open / re-open to match the source
                if now - self._cooldown.get(coin, 0) < settings.TRACKER_REOPEN_COOLDOWN_S:
                    continue                      # post-TP cooldown — wait before re-buying
                logger.info(f"[LevTracker] {coin} drift: source={their_dir} ours={our_dir} → opening")
                self._open(coin, their_dir, their_lev)

    async def run(self):
        self._connect()
        while True:
            try:
                await self.tick()
            except Exception as e:
                logger.error(f"[LevTracker] tick failed: {e}")
            await asyncio.sleep(settings.TRACKER_POLL_S)
