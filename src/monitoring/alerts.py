"""
Telegram alerts. Non-blocking — failures here must never affect trading.
"""
import asyncio
from loguru import logger


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str):
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)
        if not self._enabled:
            logger.warning("Telegram not configured — alerts disabled")

    async def send(self, msg: str):
        if not self._enabled:
            return
        try:
            import aiohttp
            url = f"https://api.telegram.org/bot{self._token}/sendMessage"
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={
                    "chat_id": self._chat_id,
                    "text": msg,
                    "parse_mode": "Markdown",
                }, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as e:
            logger.warning(f"Telegram send failed (non-critical): {e}")

    async def fill_alert(self, strategy, coin, direction, size_usd, price):
        emoji = "🟢" if direction == "long" else "🔴"
        await self.send(
            f"{emoji} *{strategy.upper()}* | {direction.upper()} `{coin}`\n"
            f"Size: `${size_usd:,.0f}` @ `${price:,.2f}`"
        )

    async def daily_summary(self, risk_status: dict):
        pnl = risk_status["daily_pnl"]
        pct = risk_status["daily_pnl_pct"]
        emoji = "📈" if pnl >= 0 else "📉"
        halted = " ⛔ HALTED" if risk_status["halted"] else ""
        await self.send(
            f"{emoji} *Daily Summary*{halted}\n"
            f"PnL: `${pnl:+,.2f}` ({pct:+.2%})\n"
            f"Open positions: `{risk_status['open_positions']}`"
        )

    async def halt_alert(self, daily_pnl_pct: float):
        await self.send(
            f"⛔ *TRADING HALTED*\n"
            f"Daily loss: `{daily_pnl_pct:.2%}` exceeded limit.\n"
            f"Will resume at midnight UTC."
        )
