"""Tests for ProfileStore delayed-commit semantics (V10 L3-B6)."""

from __future__ import annotations

import sqlite3

import pytest

from deskmate_agent.approvals import ApprovalDecision, ApprovalStore
from deskmate_agent.memory import ProfileStore
from deskmate_agent.memory.suggestions import (
    MemorySuggestion,
    create_memory_suggestion_approval,
    extract_memory_suggestion,
    memory_suggestion_streaming_composer,
    resolve_memory_suggestion,
    suggest_memory_from_text,
)


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


@pytest.mark.asyncio
async def test_memory_suggestion_writes_fact_only_after_allow(tmp_path) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        approval = create_memory_suggestion_approval(
            MemorySuggestion(
                key="Preferred IDE",
                value="Cursor",
                reason="User uses it often.",
            ),
            approval_store=approvals,
            now_ms=1_000,
            approval_id="mem-approval",
        )

        assert profile.get("memories.facts", {}) == {}
        assert approval.prompt == "Remember preferred_ide: Cursor?"
        assert approval.extras["kind"] == "memory_suggestion"

        resolved = approvals.resolve(
            "mem-approval",
            ApprovalDecision.ALLOW,
            ts_ms=2_000,
        )
        assert resolved is not None
        message = await resolve_memory_suggestion(
            resolved,
            profile_store=profile,
            clock=lambda: 3_000,
        )

        assert message == "Remembered preferred_ide: Cursor."
        facts = profile.get("memories.facts")
        assert facts["preferred_ide"]["value"] == "Cursor"
        assert facts["preferred_ide"]["updated_at_ms"] == 3_000
        assert facts["preferred_ide"]["approved_at_ms"] == 2_000
        assert facts["preferred_ide"]["approval_id"] == "mem-approval"


@pytest.mark.asyncio
async def test_memory_suggestion_deny_does_not_write_fact(tmp_path) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        create_memory_suggestion_approval(
            MemorySuggestion(key="snack", value="mochi"),
            approval_store=approvals,
            now_ms=1_000,
            approval_id="mem-deny",
        )
        resolved = approvals.resolve(
            "mem-deny",
            ApprovalDecision.DENY,
            ts_ms=2_000,
        )
        assert resolved is not None

        message = await resolve_memory_suggestion(
            resolved,
            profile_store=profile,
        )

        assert message == "Skipped memory: snack."
        assert profile.get("memories.facts", {}) == {}


@pytest.mark.asyncio
async def test_memory_suggestion_updates_existing_fact_with_old_value(
    tmp_path,
) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "VSCode",
                    "updated_at_ms": 500,
                }
            },
        )
        await profile.flush()

        approval = create_memory_suggestion_approval(
            MemorySuggestion(
                key="preferred_ide",
                value="Cursor",
                reason="User corrected the preference.",
            ),
            approval_store=approvals,
            profile_store=profile,
            now_ms=1_000,
            approval_id="mem-update",
        )

        assert approval.prompt == "Update preferred_ide from VSCode to Cursor?"
        assert approval.extras["memory_operation"] == "update"
        assert approval.extras["memory_old_value"] == "VSCode"

        resolved = approvals.resolve(
            "mem-update",
            ApprovalDecision.ALLOW,
            ts_ms=2_000,
        )
        assert resolved is not None
        message = await resolve_memory_suggestion(
            resolved,
            profile_store=profile,
            clock=lambda: 3_000,
        )

        assert message == "Updated preferred_ide: VSCode -> Cursor."
        facts = profile.get("memories.facts")
        assert facts["preferred_ide"]["value"] == "Cursor"
        assert facts["preferred_ide"]["previous_value"] == "VSCode"
        assert facts["preferred_ide"]["approval_id"] == "mem-update"


@pytest.mark.asyncio
async def test_memory_suggestion_deny_update_keeps_existing_fact(tmp_path) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        profile.set(
            "memories.facts",
            {
                "preferred_ide": {
                    "key": "preferred_ide",
                    "value": "VSCode",
                    "updated_at_ms": 500,
                }
            },
        )
        await profile.flush()

        create_memory_suggestion_approval(
            MemorySuggestion(key="preferred_ide", value="Cursor"),
            approval_store=approvals,
            profile_store=profile,
            now_ms=1_000,
            approval_id="mem-update-deny",
        )
        resolved = approvals.resolve(
            "mem-update-deny",
            ApprovalDecision.DENY,
            ts_ms=2_000,
        )
        assert resolved is not None

        message = await resolve_memory_suggestion(
            resolved,
            profile_store=profile,
        )

        assert message == "Skipped memory update: preferred_ide stays VSCode."
        facts = profile.get("memories.facts")
        assert facts["preferred_ide"]["value"] == "VSCode"
        assert facts["preferred_ide"].get("approval_id") is None


def test_extract_memory_suggestion_from_stable_preference() -> None:
    direct = extract_memory_suggestion("My favorite editor is Cursor")
    assert direct is not None
    assert direct.key == "favorite_editor"
    assert direct.value == "Cursor"
    assert direct.source == "auto"

    workflow = extract_memory_suggestion("I usually use Ghostty for terminal work")
    assert workflow is not None
    assert workflow.key == "preferred_terminal_work"
    assert workflow.value == "Ghostty"

    assert extract_memory_suggestion("remember my favorite editor is Cursor") is None
    assert extract_memory_suggestion("Can you open Terminal?") is None


@pytest.mark.asyncio
async def test_suggest_memory_from_text_dedupes_existing_and_pending(
    tmp_path,
) -> None:
    approvals = ApprovalStore()
    async with ProfileStore(tmp_path / "profile.db") as profile:
        first = suggest_memory_from_text(
            "My favorite editor is Cursor",
            approval_store=approvals,
            profile_store=profile,
            now_ms=1_000,
        )
        assert first is not None
        assert len(approvals.list_pending()) == 1

        duplicate_pending = suggest_memory_from_text(
            "My favorite editor is Cursor",
            approval_store=approvals,
            profile_store=profile,
            now_ms=2_000,
        )
        assert duplicate_pending is None
        assert len(approvals.list_pending()) == 1

        resolved = approvals.resolve(
            first.approval_id,
            ApprovalDecision.ALLOW,
            ts_ms=3_000,
        )
        assert resolved is not None
        await resolve_memory_suggestion(
            resolved,
            profile_store=profile,
            clock=lambda: 4_000,
        )

        duplicate_existing = suggest_memory_from_text(
            "My favorite editor is Cursor",
            approval_store=approvals,
            profile_store=profile,
            now_ms=5_000,
        )
        assert duplicate_existing is None
        assert len(approvals.list_pending()) == 0


@pytest.mark.asyncio
async def test_streaming_memory_suggestion_wrapper_forwards_chunks_and_suggests(
    tmp_path,
) -> None:
    approvals = ApprovalStore()

    async def fallback(_text: str):
        yield "ok"
        yield " done"

    async with ProfileStore(tmp_path / "profile.db") as profile:
        composer = memory_suggestion_streaming_composer(
            approval_store=approvals,
            profile_store=profile,
            clock=lambda: 6_000,
            fallback=fallback,
        )

        chunks = [chunk async for chunk in composer("My favorite editor is Cursor")]

        assert chunks == ["ok", " done"]
        assert profile.get("memories.facts", {}) == {}
        pending = approvals.list_pending()
        assert len(pending) == 1
        assert pending[0].prompt == "Remember favorite_editor: Cursor?"
        assert pending[0].extras["memory_source"] == "auto"
