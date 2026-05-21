"""CodingSessionStore tests (V10 Phase 15-i)."""

from __future__ import annotations

from pathlib import Path

import pytest

from deskmate_agent.memory.coding_session_store import (
    CodingSessionStore,
    _local_midnight_ms,
)

# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def test_local_midnight_floors_to_day_boundary_in_utc() -> None:
    # 2024-06-15T10:30:00Z → 10h 30m * 60 * 1000 after that midnight.
    now_ms = 1_718_447_400_000
    # UTC midnight of the same day.
    midnight = _local_midnight_ms(now_ms, tz_offset_s=0)
    assert midnight == now_ms - (10 * 3600 + 30 * 60) * 1000


def test_local_midnight_honors_positive_offset() -> None:
    """UTC 2024-06-15T00:00:00 with +08:00 offset → local 08:00, so
    local midnight is 8h earlier (UTC 2024-06-14T16:00:00)."""
    utc_midnight = 1_718_409_600_000  # 2024-06-15T00:00:00Z
    midnight = _local_midnight_ms(utc_midnight, tz_offset_s=8 * 3600)
    expected = utc_midnight - 8 * 3600 * 1000
    assert midnight == expected


# ---------------------------------------------------------------------------
# Store semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_returns_row_with_assigned_id_and_duration(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        row = await store.record(
            ide="Xcode", started_at_ms=1_000, ended_at_ms=3_500
        )
        assert row.id >= 1
        assert row.ide == "Xcode"
        assert row.duration_ms == 2_500


@pytest.mark.asyncio
async def test_record_rejects_ended_before_started(tmp_path: Path) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        with pytest.raises(ValueError):
            await store.record(
                ide="VSCode", started_at_ms=100, ended_at_ms=50
            )


@pytest.mark.asyncio
async def test_duration_since_sums_all_eligible_rows(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        await store.record(ide="A", started_at_ms=0, ended_at_ms=1_000)
        await store.record(ide="A", started_at_ms=2_000, ended_at_ms=2_500)
        await store.record(ide="B", started_at_ms=3_000, ended_at_ms=4_000)
        total = await store.duration_since_ms(0)
        assert total == 1_000 + 500 + 1_000


@pytest.mark.asyncio
async def test_duration_since_cutoff_excludes_older_rows(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        await store.record(ide="A", started_at_ms=0, ended_at_ms=1_000)
        await store.record(ide="A", started_at_ms=5_000, ended_at_ms=6_000)
        assert await store.duration_since_ms(2_000) == 1_000


@pytest.mark.asyncio
async def test_today_duration_ms_uses_local_midnight(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        # base = UTC 2024-06-15T02:00:00; offset 0 so today starts at
        # UTC 2024-06-15T00:00:00.
        base = 1_718_416_800_000
        # Session that ended *before* UTC midnight (yesterday).
        await store.record(
            ide="Zed",
            started_at_ms=base - 5 * 3600 * 1000,  # -5h
            ended_at_ms=base - 3 * 3600 * 1000,    # -3h == yesterday 23:00
        )
        # Session that ended today.
        await store.record(
            ide="Zed",
            started_at_ms=base,
            ended_at_ms=base + 15 * 60 * 1000,
        )
        today = await store.today_duration_ms(
            now_ms=base + 30 * 60 * 1000, tz_offset_s=0
        )
        assert today == 15 * 60 * 1000


@pytest.mark.asyncio
async def test_today_duration_by_ide_groups_and_sorts_desc(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        base = 1_718_416_800_000  # after local midnight when tz=0
        await store.record(
            ide="Xcode", started_at_ms=base, ended_at_ms=base + 20 * 60_000
        )
        await store.record(
            ide="VSCode",
            started_at_ms=base + 21 * 60_000,
            ended_at_ms=base + 21 * 60_000 + 45 * 60_000,
        )
        await store.record(
            ide="Xcode",
            started_at_ms=base + 70 * 60_000,
            ended_at_ms=base + 70 * 60_000 + 10 * 60_000,
        )
        # A second-before-midnight session that must be excluded.
        await store.record(
            ide="VSCode",
            started_at_ms=base - 3 * 3600 * 1000,
            ended_at_ms=base - 3 * 3600 * 1000 + 10 * 60_000,
        )
        breakdown = await store.today_duration_by_ide(
            now_ms=base + 2 * 3600 * 1000, tz_offset_s=0
        )
        assert breakdown == {
            "VSCode": 45 * 60_000,
            "Xcode": (20 + 10) * 60_000,
        }
        # Dict preserves sort order (Python 3.7+).
        assert list(breakdown.keys()) == ["VSCode", "Xcode"]


@pytest.mark.asyncio
async def test_today_duration_by_ide_empty_when_no_sessions(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        assert await store.today_duration_by_ide(
            now_ms=1_718_416_800_000, tz_offset_s=0
        ) == {}


@pytest.mark.asyncio
async def test_recent_returns_newest_first_limited(
    tmp_path: Path,
) -> None:
    async with CodingSessionStore(tmp_path / "c.db") as store:
        for i in range(5):
            await store.record(
                ide=f"IDE-{i}",
                started_at_ms=i * 1_000,
                ended_at_ms=i * 1_000 + 500,
            )
        rows = await store.recent(limit=3)
        assert [r.ide for r in rows] == ["IDE-4", "IDE-3", "IDE-2"]


@pytest.mark.asyncio
async def test_store_persists_across_reopen(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    async with CodingSessionStore(db) as s1:
        await s1.record(
            ide="Cursor", started_at_ms=0, ended_at_ms=10_000
        )
    async with CodingSessionStore(db) as s2:
        rows = await s2.recent()
        assert len(rows) == 1
        assert rows[0].ide == "Cursor"
        assert rows[0].duration_ms == 10_000


@pytest.mark.asyncio
async def test_operations_before_open_raise(tmp_path: Path) -> None:
    store = CodingSessionStore(tmp_path / "c.db")
    with pytest.raises(RuntimeError):
        await store.duration_since_ms(0)
