"""Tests for ProfileStore delayed-commit semantics (V10 L3-B6)."""

from __future__ import annotations

import sqlite3

import pytest

from deskmate_agent.memory import ProfileStore


@pytest.mark.asyncio
async def test_set_is_in_memory_until_flush(tmp_path) -> None:
    db = tmp_path / "profile.db"
    store = ProfileStore(db)
    await store.open()
    try:
        store.set("name", "Pixie")
        assert store.get("name") == "Pixie"
        assert "name" in store.dirty_keys

        # Raw SQLite read must NOT see the value yet — no commit happened.
        with sqlite3.connect(db) as raw:
            rows = raw.execute("SELECT key FROM profile").fetchall()
        assert rows == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_flush_commits_all_dirty_keys(tmp_path) -> None:
    db = tmp_path / "profile.db"
    async with ProfileStore(db) as store:
        store.update_many({"tone": "gentle", "work_hours": "10-19"})
        written = await store.flush()
        assert written == 2
        assert store.dirty_keys == frozenset()

    # Fresh store sees persisted values.
    async with ProfileStore(db) as second:
        assert second.get("tone") == "gentle"
        assert second.get("work_hours") == "10-19"


@pytest.mark.asyncio
async def test_close_auto_flushes_pending(tmp_path) -> None:
    db = tmp_path / "profile.db"
    store = ProfileStore(db)
    await store.open()
    store.set("interests", ["ML", "photography"])
    await store.close()

    async with ProfileStore(db) as second:
        assert second.get("interests") == ["ML", "photography"]


@pytest.mark.asyncio
async def test_set_same_value_is_noop(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as store:
        store.set("tone", "casual")
        await store.flush()
        assert store.dirty_keys == frozenset()
        # Re-setting the same value must not mark dirty.
        store.set("tone", "casual")
        assert store.dirty_keys == frozenset()


@pytest.mark.asyncio
async def test_snapshot_returns_isolated_copy(tmp_path) -> None:
    async with ProfileStore(tmp_path / "profile.db") as store:
        store.set("a", 1)
        snap = store.snapshot()
        snap["a"] = 999  # mutating snapshot must not affect store
        assert store.get("a") == 1


@pytest.mark.asyncio
async def test_get_and_set_before_open_raise(tmp_path) -> None:
    store = ProfileStore(tmp_path / "profile.db")
    with pytest.raises(RuntimeError):
        store.get("x")
    with pytest.raises(RuntimeError):
        store.set("x", 1)
