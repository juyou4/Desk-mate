"""Tests for the mid-term session SQLite store (V10 L3-B6 / L2-#3)."""

from __future__ import annotations

import pytest

from deskmate_agent.memory import SessionMemory, SessionSummary


async def _open(tmp_path) -> SessionMemory:
    mem = SessionMemory(tmp_path / "sessions.db")
    await mem.open()
    return mem


@pytest.mark.asyncio
async def test_journal_mode_is_wal(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        mode = await mem.pragma("journal_mode")
        assert str(mode).lower() == "wal"


@pytest.mark.asyncio
async def test_synchronous_is_normal(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        # NORMAL is represented as 1 in SQLite pragma output.
        sync = await mem.pragma("synchronous")
        assert int(sync) == 1


@pytest.mark.asyncio
async def test_upsert_then_get_round_trip(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        row = SessionSummary(
            session_id="sess-1",
            summary="user started debugging",
            started_at_ms=1_000_000,
            updated_at_ms=1_001_000,
        )
        await mem.upsert(row)
        fetched = await mem.get("sess-1")
        assert fetched == row


@pytest.mark.asyncio
async def test_upsert_overwrites_existing(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        await mem.upsert(
            SessionSummary("s", "first", started_at_ms=1, updated_at_ms=1)
        )
        await mem.upsert(
            SessionSummary("s", "second", started_at_ms=1, updated_at_ms=2)
        )
        got = await mem.get("s")
        assert got is not None
        assert got.summary == "second"
        assert got.updated_at_ms == 2


@pytest.mark.asyncio
async def test_list_updated_since_orders_desc(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        await mem.upsert(
            SessionSummary("a", "old", started_at_ms=1, updated_at_ms=1_000)
        )
        await mem.upsert(
            SessionSummary("b", "new", started_at_ms=2, updated_at_ms=5_000)
        )
        await mem.upsert(
            SessionSummary("c", "mid", started_at_ms=3, updated_at_ms=3_000)
        )
        rows = await mem.list_updated_since(2_000)
        assert [r.session_id for r in rows] == ["b", "c"]


@pytest.mark.asyncio
async def test_get_missing_returns_none(tmp_path) -> None:
    async with SessionMemory(tmp_path / "sessions.db") as mem:
        assert await mem.get("nope") is None
