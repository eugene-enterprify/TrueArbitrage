"""SQLite: історія спредів, runtime-налаштування, CSV-експорт."""
from __future__ import annotations

import csv
import io
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from src.config import SamplingConfig
from src.models import SpreadView

_SCHEMA = """
CREATE TABLE IF NOT EXISTS spreads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    time TEXT NOT NULL,
    base TEXT NOT NULL,
    long_exchange TEXT NOT NULL,
    short_exchange TEXT NOT NULL,
    long_ask REAL NOT NULL,
    short_bid REAL NOT NULL,
    gross_pct REAL NOT NULL,
    net_pct REAL NOT NULL,
    funding_long_pct REAL,
    funding_short_pct REAL,
    vol_long_musd REAL,
    vol_short_musd REAL
);
CREATE INDEX IF NOT EXISTS idx_spreads_base_ts ON spreads(base, ts);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    base TEXT NOT NULL,
    opened_time TEXT NOT NULL,
    closed_time TEXT NOT NULL,
    duration_sec INTEGER NOT NULL,
    max_gross_pct REAL NOT NULL,
    max_net_pct REAL NOT NULL,
    long_exchange TEXT NOT NULL,
    short_exchange TEXT NOT NULL,
    opened_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(opened_ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# конвертація старого формату: сирі частки/долари -> %, млн $, людський час
_MIGRATE_OLD = """
DROP INDEX IF EXISTS idx_spreads_base_ts;
ALTER TABLE spreads RENAME TO spreads_old;
"""

_MIGRATE_COPY = """
INSERT INTO spreads (ts, time, base, long_exchange, short_exchange, long_ask, short_bid,
                     gross_pct, net_pct, funding_long_pct, funding_short_pct,
                     vol_long_musd, vol_short_musd)
SELECT CAST(ts AS INTEGER),
       datetime(ts, 'unixepoch', 'localtime'),
       base, long_exchange, short_exchange, long_ask, short_bid,
       ROUND(gross_pct, 4), ROUND(net_pct, 4),
       ROUND(funding_long * 100, 4), ROUND(funding_short * 100, 4),
       ROUND(vol_long_usd / 1e6, 2), ROUND(vol_short_usd / 1e6, 2)
FROM spreads_old;
DROP TABLE spreads_old;
"""

_CSV_COLUMNS = [
    "time", "base", "long_exchange", "short_exchange", "long_ask", "short_bid",
    "gross_pct", "net_pct", "funding_long_pct", "funding_short_pct",
    "vol_long_musd", "vol_short_musd",
]


def _pct(fraction: float | None) -> float | None:
    """Частка (0.0001) -> відсотки (0.01)."""
    return round(fraction * 100, 4) if fraction is not None else None


def _musd(usd: float | None) -> float | None:
    """Долари -> мільйони доларів."""
    return round(usd / 1e6, 2) if usd is not None else None


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


@dataclass
class _Episode:
    """Відкритий епізод: спред тримається вище порога запису."""
    opened_ts: float
    last_seen_ts: float
    max_gross_pct: float
    net_at_max_pct: float
    long_exchange: str
    short_exchange: str


# монета зникла з розрахунку (застарілі котирування) довше ніж на N сек — епізод закривається
_EPISODE_GRACE_SEC = 30


class Storage:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # семплінг: останній записаний спред по монеті
        self._last_written: dict[str, tuple[float, float]] = {}  # base -> (ts, gross_pct)
        # відкриті епізоди спредів
        self._episodes: dict[str, _Episode] = {}

    async def open(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        cursor = await self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='spreads'"
        )
        exists = await cursor.fetchone() is not None
        old_format = False
        if exists:
            cursor = await self._db.execute("PRAGMA table_info(spreads)")
            cols = {row[1] for row in await cursor.fetchall()}
            old_format = "liquid" in cols
        if old_format:
            await self._db.executescript(_MIGRATE_OLD)
        await self._db.executescript(_SCHEMA)
        if old_format:
            await self._db.executescript(_MIGRATE_COPY)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def record_spreads(self, views: list[SpreadView], sampling: SamplingConfig) -> None:
        """Пише лише корисні спреди: ліквідні, від min_record_gross_pct.

        Додатково семплінг: запис при зміні понад delta_pct або раз на interval_sec.
        Паралельно веде епізоди: один рядок у episodes = час життя одного спреду.
        """
        assert self._db is not None
        now = time.time()
        rows = []
        episode_rows = []
        seen: set[str] = set()

        for v in views:
            seen.add(v.base)
            active = v.gross_pct >= sampling.min_record_gross_pct and (
                v.liquid or not sampling.liquid_only
            )
            ep = self._episodes.get(v.base)

            if not active:
                # спред зник — забуваємо семплінг, щоб нову появу записати одразу
                self._last_written.pop(v.base, None)
                if ep is not None:
                    episode_rows.append(self._episode_row(v.base, ep, closed_ts=now))
                    del self._episodes[v.base]
                continue

            if ep is None:
                self._episodes[v.base] = _Episode(
                    opened_ts=now, last_seen_ts=now,
                    max_gross_pct=v.gross_pct, net_at_max_pct=v.net_pct,
                    long_exchange=v.long_exchange, short_exchange=v.short_exchange,
                )
            else:
                ep.last_seen_ts = now
                if v.gross_pct > ep.max_gross_pct:
                    ep.max_gross_pct = v.gross_pct
                    ep.net_at_max_pct = v.net_pct
                    ep.long_exchange = v.long_exchange
                    ep.short_exchange = v.short_exchange

            last = self._last_written.get(v.base)
            if last is not None:
                last_ts, last_gross = last
                if now - last_ts < sampling.interval_sec and abs(v.gross_pct - last_gross) < sampling.delta_pct:
                    continue
            self._last_written[v.base] = (now, v.gross_pct)
            rows.append((
                int(now), _fmt_time(now),
                v.base, v.long_exchange, v.short_exchange, v.long_ask, v.short_bid,
                round(v.gross_pct, 4), round(v.net_pct, 4),
                _pct(v.funding_long), _pct(v.funding_short),
                _musd(v.vol_long_usd), _musd(v.vol_short_usd),
            ))

        # монета взагалі зникла з розрахунку — закриваємо епізод після grace-періоду
        for base in list(self._episodes):
            ep = self._episodes[base]
            if base not in seen and now - ep.last_seen_ts > _EPISODE_GRACE_SEC:
                episode_rows.append(self._episode_row(base, ep, closed_ts=ep.last_seen_ts))
                del self._episodes[base]

        if rows:
            await self._db.executemany(
                "INSERT INTO spreads (ts, time, base, long_exchange, short_exchange, long_ask, short_bid,"
                " gross_pct, net_pct, funding_long_pct, funding_short_pct, vol_long_musd, vol_short_musd)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        if episode_rows:
            await self._db.executemany(
                "INSERT INTO episodes (base, opened_time, closed_time, duration_sec,"
                " max_gross_pct, max_net_pct, long_exchange, short_exchange, opened_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                episode_rows,
            )
        if rows or episode_rows:
            await self._db.commit()

    @staticmethod
    def _episode_row(base: str, ep: _Episode, closed_ts: float) -> tuple:
        return (
            base, _fmt_time(ep.opened_ts), _fmt_time(closed_ts),
            max(1, round(closed_ts - ep.opened_ts)),
            round(ep.max_gross_pct, 4), round(ep.net_at_max_pct, 4),
            ep.long_exchange, ep.short_exchange, int(ep.opened_ts),
        )

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

    async def recent_episodes(self, limit: int = 15) -> list[tuple]:
        """Останні завершені епізоди, найновіші першими."""
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT base, opened_time, duration_sec, max_gross_pct, max_net_pct,"
            " long_exchange, short_exchange"
            " FROM episodes ORDER BY opened_ts DESC LIMIT ?",
            (limit,),
        )
        return await cursor.fetchall()

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
