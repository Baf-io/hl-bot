"""
Telegram alerts. Non-blocking — failures here must never affect trading.
"""
import asyncio
from loguru import logger


class TelegramAlerter:
    def __init__(self, token: str, chat_id: str,
                 ntfy_topic: str = "", ntfy_server: str = "https://ntfy.sh"):
        self._token = token
        self._chat_id = chat_id
        self._enabled = bool(token and chat_id)
        if not self._enabled:
            logger.warning("Telegram not configured — alerts disabled")

        # ntfy phone push — reserved for HIGH-SIGNAL events only (halt / bad action /
        # daily summary). Never used for routine per-trade fills (no phone spam).
        self._ntfy_topic  = ntfy_topic
        self._ntfy_server = ntfy_server.rstrip("/")
        self._ntfy_enabled = bool(ntfy_topic)
        if not self._ntfy_enabled:
            logger.info("ntfy not configured — phone push disabled")

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

    async def ntfy(self, message: str, title: str = "HL-Bot",
                   priority: str = "default", tags: str = ""):
        """
        Push a HIGH-SIGNAL notification to the phone via ntfy. Non-blocking; never
        raises. priority: min|low|default|high|urgent. tags: comma-sep emoji shortcodes.
        """
        if not self._ntfy_enabled:
            return
        try:
            import aiohttp
            headers = {"Title": title, "Priority": priority}
            if tags:
                headers["Tags"] = tags
            async with aiohttp.ClientSession() as session:
                await session.post(
                    f"{self._ntfy_server}/{self._ntfy_topic}",
                    data=message.encode("utf-8"),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5),
                )
        except Exception as e:
            logger.warning(f"ntfy push failed (non-critical): {e}")

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
        # Phone: the one routine daily buzz the user asked for.
        await self.ntfy(
            f"PnL ${pnl:+,.2f} ({pct:+.2%}) | {risk_status['open_positions']} open{halted}",
            title="HL-Bot daily summary", priority="low", tags="bar_chart",
        )

    async def halt_alert(self, daily_pnl_pct: float):
        await self.send(
            f"⛔ *TRADING HALTED*\n"
            f"Daily loss: `{daily_pnl_pct:.2%}` exceeded limit.\n"
            f"Will resume at midnight UTC."
        )
        # Phone: THE critical "bot stopped trading because we're down" alert.
        await self.ntfy(
            f"Daily loss {daily_pnl_pct:.2%} hit the limit — bot has STOPPED trading "
            f"until midnight UTC.",
            title="⛔ HL-Bot HALTED", priority="urgent", tags="octagonal_sign",
        )

    async def force_close_alert(self, coin: str, kind: str, detail: str, pnl_usd: float):
        """A bad action taken: guardian force-closed a position (zombie/nuclear)."""
        await self.ntfy(
            f"{coin}: {detail} | realized ${pnl_usd:+,.2f}",
            title=f"⚠️ HL-Bot {kind.upper()} force-close",
            priority="high", tags="warning",
        )
