"""SQLite: історія спредів, runtime-налаштування, CSV-експорт."""
from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import aiosqlite

from src.models import SpreadView

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spreads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    base TEXT NOT NULL,
    long_exchange TEXT NOT NULL,
    short_exchange TEXT NOT NULL,
    long_ask REAL NOT NULL,
    short_bid REAL NOT NULL,
    gross_pct REAL NOT NULL,
    net_pct REAL NOT NULL,
    funding_long REAL,
    funding_short REAL,
    vol_long_usd REAL,
    vol_short_usd REAL,
    liquid INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spreads_base_ts ON spreads(base, ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CSV_COLUMNS = [
    "ts", "base", "long_exchange", "short_exchange", "long_ask", "short_bid",
    "gross_pct", "net_pct", "funding_long", "funding_short",
    "vol_long_usd", "vol_short_usd", "liquid",
]


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # семплінг: останній записаний спред по монеті
        self._last_written: dict[str, tuple[float, float]] = {}  # base -> (ts, gross_pct)

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def record_spreads(self, views: list[SpreadView], delta_pct: float, interval_sec: int) -> None:
        """Пише спред, лише якщо він змінився понад delta_pct або минуло interval_sec."""
        assert self._db is not None
        now = time.time()
        rows = []
        for v in views:
            last = self._last_written.get(v.base)
            if last is not None:
                last_ts, last_gross = last
                if now - last_ts < interval_sec and abs(v.gross_pct - last_gross) < delta_pct:
                    continue
            self._last_written[v.base] = (now, v.gross_pct)
            rows.append((
                now, v.base, v.long_exchange, v.short_exchange, v.long_ask, v.short_bid,
                v.gross_pct, v.net_pct, v.funding_long, v.funding_short,
                v.vol_long_usd, v.vol_short_usd, int(v.liquid),
            ))
        if rows:
            await self._db.executemany(
                "INSERT INTO spreads (ts, base, long_exchange, short_exchange, long_ask, short_bid,"
                " gross_pct, net_pct, funding_long, funding_short, vol_long_usd, vol_short_usd, liquid)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            await self._db.commit()

    async def export_csv(self, hours: float = 24.0) -> tuple[bytes, int]:
        """CSV за останні N годин. Повертає (вміст, кількість рядків)."""
        assert self._db is not None
        since = time.time() - hours * 3600
        cursor = await self._db.execute(
            f"SELECT {', '.join(_CSV_COLUMNS)} FROM spreads WHERE ts >= ? ORDER BY ts", (since,)
        )
        rows = await cursor.fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(_CSV_COLUMNS)
        writer.writerows(rows)
        return buf.getvalue().encode("utf-8-sig"), len(rows)

    async def count_rows(self) -> int:
        assert self._db is not None
        cursor = await self._db.execute("SELECT COUNT(*) FROM spreads")
        (n,) = await cursor.fetchone()
        return n

    # ---- runtime-налаштування (перекривають config.yaml після рестарту) ----

    async def set_setting(self, key: str, value: str) -> None:
        assert self._db is not None
        await self._db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self._db.commit()

    async def get_settings(self) -> dict[str, str]:
        assert self._db is not None
        cursor = await self._db.execute("SELECT key, value FROM settings")
        return dict(await cursor.fetchall())
