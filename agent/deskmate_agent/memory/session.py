"""Session (mid-term) memory backed by aiosqlite.

V10 L3-B6:
- ``journal_mode = WAL`` for concurrent read/write without blocking.
- ``synchronous = NORMAL`` (10x faster fsync behaviour than FULL, still
  crash-safe within the WAL contract).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from ..logging_setup import get_logger

_LOG = get_logger("deskmate_agent.memory.session")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    summary     TEXT NOT NULL,
    started_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    ended_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
    ON sessions(updated_at);
"""


@dataclass
class SessionSummary:
    session_id: str
    summary: str
    started_at_ms: int
    updated_at_ms: int
    ended_at_ms: int | None = None


class SessionMemory:
    """Async SQLite-backed store of session summaries."""

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
        # foreign_keys is cheap to flip on even if unused today.
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> SessionMemory:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def upsert(self, row: SessionSummary) -> None:
        conn = self._require()
        await conn.execute(
            """
            INSERT INTO sessions (id, summary, started_at, updated_at, ended_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                summary = excluded.summary,
                updated_at = excluded.updated_at,
                ended_at = excluded.ended_at
            """,
            (
                row.session_id,
                row.summary,
                row.started_at_ms,
                row.updated_at_ms,
                row.ended_at_ms,
            ),
        )
        await conn.commit()

    async def get(self, session_id: str) -> SessionSummary | None:
        conn = self._require()
        async with conn.execute(
            "SELECT id, summary, started_at, updated_at, ended_at "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ) as cur:
            row = await cur.fetchone()
            return self._row_to_summary(row) if row else None

    async def list_updated_since(self, cutoff_ms: int) -> list[SessionSummary]:
        """V10 L2-#3: used by the startup restore reader to hydrate the iron
        list of recent sessions without reviving stale notifications."""
        conn = self._require()
        async with conn.execute(
            "SELECT id, summary, started_at, updated_at, ended_at "
            "FROM sessions WHERE updated_at >= ? ORDER BY updated_at DESC",
            (cutoff_ms,),
        ) as cur:
            rows = await cur.fetchall()
            return [self._row_to_summary(r) for r in rows]

    async def pragma(self, name: str) -> str | int | None:
        """Expose a PRAGMA result for diagnostics / acceptance tests."""
        conn = self._require()
        async with conn.execute(f"PRAGMA {name}") as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_summary(row: tuple) -> SessionSummary:
        return SessionSummary(
            session_id=row[0],
            summary=row[1],
            started_at_ms=row[2],
            updated_at_ms=row[3],
            ended_at_ms=row[4],
        )

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SessionMemory used before open()")
        return self._conn


def now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["SCHEMA_SQL", "SessionMemory", "SessionSummary", "now_ms"]
