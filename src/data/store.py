"""
Lightweight in-memory + SQLite store for market state.
Keeps rolling windows the signal engines query.
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import aiosqlite
from loguru import logger


DB_PATH = "data/bot.db"


@dataclass
class FundingSnapshot:
    coin: str
    rate_8h: float          # as decimal, e.g. 0.0005 = 0.05%
    open_interest: float    # USD notional
    mark_price: float
    ts: datetime = field(default_factory=datetime.utcnow)


@dataclass
class TradeSignal:
    strategy: str           # "funding_carry" | "leaderboard" | "cascade"
    coin: str
    direction: str          # "long" | "short"
    size_usd: float
    confidence: float       # 0-1
    meta: dict = field(default_factory=dict)
    ts: datetime = field(default_factory=datetime.utcnow)


class MarketStore:
    """
    In-memory rolling store with async SQLite persistence.
    Signal engines READ from here; the feed WRITES to here.
    """

    def __init__(self):
        # Rolling 500-tick windows per coin
        self.funding: dict[str, deque[FundingSnapshot]]   = {}
        self.mid_prices: dict[str, deque[float]]           = {}
        self.oi_history: dict[str, deque[float]]           = {}
        self.orderbook: dict[str, dict]                    = {}  # latest L2 snapshot
        self._db: Optional[aiosqlite.Connection]           = None

    async def init_db(self):
        self._db = await aiosqlite.connect(DB_PATH)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                strategy TEXT,
                coin TEXT,
                direction TEXT,
                size_usd REAL,
                entry_price REAL,
                exit_price REAL,
                pnl_usd REAL,
                status TEXT DEFAULT 'open'
            )
        """)
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS funding_log (
                ts TEXT,
                coin TEXT,
                rate_8h REAL,
                open_interest REAL
            )
        """)
        await self._db.commit()
        logger.info("DB initialised")

    def update_funding(self, snap: FundingSnapshot):
        q = self.funding.setdefault(snap.coin, deque(maxlen=500))
        q.append(snap)

        q2 = self.oi_history.setdefault(snap.coin, deque(maxlen=500))
        q2.append(snap.open_interest)

    def update_mid(self, coin: str, price: float):
        q = self.mid_prices.setdefault(coin, deque(maxlen=500))
        q.append(price)

    def update_orderbook(self, coin: str, snapshot: dict):
        self.orderbook[coin] = snapshot

    def latest_funding(self, coin: str) -> Optional[FundingSnapshot]:
        q = self.funding.get(coin)
        return q[-1] if q else None

    def latest_mid(self, coin: str) -> Optional[float]:
        q = self.mid_prices.get(coin)
        return q[-1] if q else None

    def oi_percentile(self, coin: str, pct: int = 90) -> Optional[float]:
        """Return the Nth percentile of OI over stored history."""
        q = self.oi_history.get(coin)
        if not q or len(q) < 10:
            return None
        import numpy as np
        return float(np.percentile(list(q), pct))

    async def log_trade(self, strategy, coin, direction, size_usd, entry_price):
        if not self._db:
            return
        await self._db.execute(
            "INSERT INTO trades (ts,strategy,coin,direction,size_usd,entry_price) VALUES (?,?,?,?,?,?)",
            (datetime.utcnow().isoformat(), strategy, coin, direction, size_usd, entry_price),
        )
        await self._db.commit()

    async def close_trade(self, trade_id: int, exit_price: float, pnl_usd: float):
        if not self._db:
            return
        await self._db.execute(
            "UPDATE trades SET exit_price=?,pnl_usd=?,status='closed' WHERE id=?",
            (exit_price, pnl_usd, trade_id),
        )
        await self._db.commit()
