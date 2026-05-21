"""Persisted coding-session log (V10 Phase 15-i).

Mirrors :class:`SessionMemory`'s SQLite conventions (WAL journal,
NORMAL synchronous, :func:`aiosqlite.connect`) so the two stores can
share a file when the caller opts in.

Rows are immutable once written — the tracker records a session
when it dismisses, carrying (ide, start, end). The duration is
materialized so daily rollup queries stay single-table.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..logging_setup import get_logger

_LOG = get_logger("deskmate_agent.memory.coding_session")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS coding_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ide             TEXT NOT NULL,
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER NOT NULL,
    duration_ms     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coding_sessions_ended_at
    ON coding_sessions(ended_at_ms);
"""


@dataclass(frozen=True)
class CodingSession:
    id: int
    ide: str
    started_at_ms: int
    ended_at_ms: int
    duration_ms: int


class CodingSessionStore:
    """Append-only log of finished coding sessions."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> CodingSessionStore:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def record(
        self, *, ide: str, started_at_ms: int, ended_at_ms: int
    ) -> CodingSession:
        """Append one session. Returns the stored row with its id."""
        if ended_at_ms < started_at_ms:
            raise ValueError(
                f"ended_at_ms < started_at_ms: {ended_at_ms} < {started_at_ms}"
            )
        duration = ended_at_ms - started_at_ms
        conn = self._require()
        async with conn.execute(
            """
            INSERT INTO coding_sessions
                (ide, started_at_ms, ended_at_ms, duration_ms)
            VALUES (?, ?, ?, ?)
            RETURNING id
            """,
            (ide, started_at_ms, ended_at_ms, duration),
        ) as cur:
            row = await cur.fetchone()
        await conn.commit()
        return CodingSession(
            id=int(row[0]),
            ide=ide,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            duration_ms=duration,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def duration_since_ms(self, since_ms: int) -> int:
        """Sum ``duration_ms`` for sessions ending at/after ``since_ms``."""
        conn = self._require()
        async with conn.execute(
            """
            SELECT COALESCE(SUM(duration_ms), 0)
            FROM coding_sessions
            WHERE ended_at_ms >= ?
            """,
            (since_ms,),
        ) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def today_duration_ms(
        self, *, now_ms: int | None = None, tz_offset_s: int = 0
    ) -> int:
        """Sum sessions ending since local midnight.

        ``tz_offset_s`` is the caller's offset from UTC in seconds
        (positive east). macOS callers can compute it via
        :func:`time.localtime().tm_gmtoff`; tests pin a fixed offset
        for determinism.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        midnight_ms = _local_midnight_ms(now_ms, tz_offset_s)
        return await self.duration_since_ms(midnight_ms)

    async def today_duration_by_ide(
        self, *, now_ms: int | None = None, tz_offset_s: int = 0
    ) -> dict[str, int]:
        """Per-IDE breakdown of today's coding time.

        Returns ``{ide_name: duration_ms, ...}`` sorted descending by
        duration — the menu bar can just iterate and render rows.
        """
        if now_ms is None:
            now_ms = int(time.time() * 1000)
        midnight_ms = _local_midnight_ms(now_ms, tz_offset_s)
        conn = self._require()
        async with conn.execute(
            """
            SELECT ide, SUM(duration_ms) AS total
            FROM coding_sessions
            WHERE ended_at_ms >= ?
            GROUP BY ide
            ORDER BY total DESC
            """,
            (midnight_ms,),
        ) as cur:
            rows = await cur.fetchall()
        return {str(r[0]): int(r[1]) for r in rows}

    async def recent(
        self, limit: int = 20
    ) -> list[CodingSession]:
        conn = self._require()
        async with conn.execute(
            """
            SELECT id, ide, started_at_ms, ended_at_ms, duration_ms
            FROM coding_sessions
            ORDER BY ended_at_ms DESC
            LIMIT ?
            """,
            (int(limit),),
        ) as cur:
            rows = await cur.fetchall()
        return [
            CodingSession(
                id=int(r[0]),
                ide=r[1],
                started_at_ms=int(r[2]),
                ended_at_ms=int(r[3]),
                duration_ms=int(r[4]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("CodingSessionStore used before open()")
        return self._conn


def _local_midnight_ms(now_ms: int, tz_offset_s: int) -> int:
    """Return the ms-since-epoch of the most recent local midnight.

    Pure-Python arithmetic so callers can test with any offset; no
    ``datetime`` round-trips needed.
    """
    day_ms = 24 * 60 * 60 * 1000
    offset_ms = tz_offset_s * 1000
    # Shift into "local time", then floor to the day boundary, then
    # shift back out.
    local_ms = now_ms + offset_ms
    local_midnight_ms = local_ms - (local_ms % day_ms)
    return local_midnight_ms - offset_ms


__all__ = [
    "SCHEMA_SQL",
    "CodingSession",
    "CodingSessionStore",
]
