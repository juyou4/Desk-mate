"""Long-term user profile with delayed commit (V10 L3-B6).

The profile is a cheap key-value store. During a running session, ``set`` /
``update_many`` only mutate the in-memory cache. A background task (or
explicit ``flush`` call at session end) persists pending deltas in one
commit. This avoids a per-turn SQLite fsync on a hot path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS profile (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


class ProfileStore:
    """Key-value profile with in-memory cache and delayed persistence."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._cache: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._loaded = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

        # Load the whole profile into memory once — tiny payload by design.
        async with self._conn.execute(
            "SELECT key, value_json FROM profile"
        ) as cur:
            async for row in cur:
                self._cache[row[0]] = json.loads(row[1])
        self._loaded = True

    async def close(self) -> None:
        if self._dirty:
            await self.flush()
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> ProfileStore:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # In-memory operations (no I/O)
    # ------------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        self._require_loaded()
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._require_loaded()
        if self._cache.get(key) == value:
            return
        self._cache[key] = value
        self._dirty.add(key)

    def update_many(self, values: dict[str, Any]) -> None:
        for k, v in values.items():
            self.set(k, v)

    def snapshot(self) -> dict[str, Any]:
        self._require_loaded()
        return dict(self._cache)

    @property
    def dirty_keys(self) -> frozenset[str]:
        return frozenset(self._dirty)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    async def flush(self) -> int:
        """Persist pending mutations in a single commit. Returns rows written."""
        if not self._dirty:
            return 0
        conn = self._require_conn()
        now = _now_ms()
        rows = [
            (key, json.dumps(self._cache[key], ensure_ascii=False), now)
            for key in self._dirty
        ]
        await conn.executemany(
            """
            INSERT INTO profile (key, value_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        await conn.commit()
        written = len(rows)
        self._dirty.clear()
        return written

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("ProfileStore used before open()")

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ProfileStore used before open()")
        return self._conn


__all__ = ["ProfileStore", "SCHEMA_SQL"]
