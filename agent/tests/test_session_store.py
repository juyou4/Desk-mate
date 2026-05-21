"""V10 L1-D / L2-#4 / L2-#5 — runtime session store."""

from __future__ import annotations

from deskmate_agent.protocol.state import Priority
from deskmate_agent.sessions import (
    SessionInfo,
    SessionListItem,
    SessionPhase,
    SessionState,
    SessionStore,
)


def _info(
    sid: str,
    *,
    state: SessionState = SessionState.ACTIVE,
    priority: Priority = Priority.P2,
    updated_at_ms: int = 1_000,
    created_at_ms: int = 1_000,
    title: str = "",
    summary: str = "",
    phase: SessionPhase = SessionPhase.RUNNING,
    parent_session_id: str | None = None,
    subagent_kind: str | None = None,
) -> SessionInfo:
    return SessionInfo(
        session_id=sid,
        state=state,
        priority=priority,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        title=title,
        summary=summary,
        phase=phase,
        parent_session_id=parent_session_id,
        subagent_kind=subagent_kind,
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_upsert_inserts_new_session() -> None:
    store = SessionStore()
    store.upsert(_info("s1"))
    assert len(store) == 1
    assert store.get("s1") is not None


def test_upsert_updates_existing_session() -> None:
    store = SessionStore()
    store.upsert(_info("s1", title="old", updated_at_ms=1_000))
    store.upsert(_info("s1", title="new", updated_at_ms=2_000))
    got = store.get("s1")
    assert got is not None
    assert got.title == "new"
    assert got.updated_at_ms == 2_000
    assert len(store) == 1


def test_upsert_preserves_created_at_when_incoming_is_zero() -> None:
    store = SessionStore()
    store.upsert(_info("s1", created_at_ms=5_000, updated_at_ms=5_000))
    # Partial update: caller doesn't know the original created_at.
    partial = SessionInfo(session_id="s1", updated_at_ms=10_000)
    store.upsert(partial)
    got = store.get("s1")
    assert got is not None
    assert got.created_at_ms == 5_000


def test_touch_updates_timestamp_and_state() -> None:
    store = SessionStore()
    store.upsert(_info("s1", updated_at_ms=1_000))
    touched = store.touch("s1", 2_000, new_state=SessionState.PAUSED)
    assert touched is not None
    assert touched.state is SessionState.PAUSED
    assert touched.updated_at_ms == 2_000


def test_touch_to_closed_sets_closed_at_once() -> None:
    store = SessionStore()
    store.upsert(_info("s1"))
    first = store.touch("s1", 5_000, new_state=SessionState.CLOSED)
    assert first is not None
    assert first.closed_at_ms == 5_000
    # A second touch shouldn't overwrite closed_at_ms.
    second = store.touch("s1", 9_000, new_state=SessionState.CLOSED)
    assert second is not None
    assert second.closed_at_ms == 5_000


def test_touch_missing_session_returns_none() -> None:
    store = SessionStore()
    assert store.touch("missing", 1_000) is None


def test_remove_returns_removed_info() -> None:
    store = SessionStore()
    store.upsert(_info("s1"))
    info = store.remove("s1")
    assert info is not None
    assert info.session_id == "s1"
    assert store.get("s1") is None
    assert store.remove("s1") is None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_list_orders_by_updated_at_desc_then_priority() -> None:
    store = SessionStore()
    store.upsert(_info("a", updated_at_ms=1_000, priority=Priority.P3))
    store.upsert(_info("b", updated_at_ms=2_000, priority=Priority.P3))
    store.upsert(_info("c", updated_at_ms=2_000, priority=Priority.P1))
    ids = [s.session_id for s in store.list()]
    # Ties on updated_at go to higher-priority (lower rank) first.
    assert ids == ["c", "b", "a"]


def test_list_filters_by_state() -> None:
    store = SessionStore()
    store.upsert(_info("a", state=SessionState.ACTIVE))
    store.upsert(_info("b", state=SessionState.CLOSED))
    assert [s.session_id for s in store.list_active()] == ["a"]
    assert [s.session_id for s in store.list(state=SessionState.CLOSED)] == ["b"]


def test_list_respects_limit() -> None:
    store = SessionStore()
    for i, sid in enumerate(["a", "b", "c", "d"]):
        store.upsert(_info(sid, updated_at_ms=1_000 + i))
    assert [s.session_id for s in store.list(limit=2)] == ["d", "c"]


def test_contains_checks_sid() -> None:
    store = SessionStore()
    store.upsert(_info("s1"))
    assert "s1" in store
    assert "missing" not in store
    assert 42 not in store  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def test_subscribe_fires_on_upsert_touch_remove() -> None:
    store = SessionStore()
    events: list[tuple[str, str]] = []
    store.subscribe(lambda e: events.append((e.kind, e.session_id)))

    store.upsert(_info("s1"))
    store.touch("s1", 2_000)
    store.remove("s1")

    assert events == [("upsert", "s1"), ("touch", "s1"), ("remove", "s1")]


def test_unsubscribe_stops_further_events() -> None:
    store = SessionStore()
    events: list[str] = []
    unsub = store.subscribe(lambda e: events.append(e.kind))

    store.upsert(_info("s1"))
    unsub()
    store.touch("s1", 2_000)

    assert events == ["upsert"]


def test_subscriber_error_does_not_break_store() -> None:
    store = SessionStore()

    def broken_sub(_event: object) -> None:
        raise RuntimeError("boom")

    store.subscribe(broken_sub)
    # Should not raise despite the broken subscriber.
    store.upsert(_info("s1"))
    assert store.get("s1") is not None


# ---------------------------------------------------------------------------
# V10 L2-#4: actionable-first ordering
# ---------------------------------------------------------------------------


def test_list_orders_by_phase_first() -> None:
    """A waiting_for_approval session beats a more-recently-updated
    running one — that's the whole point of actionable-first."""
    store = SessionStore()
    store.upsert(_info("running-recent", updated_at_ms=10_000))
    store.upsert(
        _info(
            "approval-stale",
            updated_at_ms=1_000,
            phase=SessionPhase.WAITING_FOR_APPROVAL,
        )
    )
    store.upsert(
        _info(
            "answer",
            updated_at_ms=2_000,
            phase=SessionPhase.WAITING_FOR_ANSWER,
        )
    )
    store.upsert(
        _info(
            "completed",
            updated_at_ms=20_000,  # very recent but done
            phase=SessionPhase.COMPLETED,
        )
    )
    ids = [s.session_id for s in store.list()]
    assert ids == ["approval-stale", "answer", "running-recent", "completed"]


def test_list_within_phase_falls_back_to_recency() -> None:
    """Two running sessions still order by ``-updated_at_ms`` —
    actionable-first is a *coarse* sort, not a replacement."""
    store = SessionStore()
    store.upsert(_info("a", updated_at_ms=1_000))
    store.upsert(_info("b", updated_at_ms=3_000))
    store.upsert(_info("c", updated_at_ms=2_000))
    ids = [s.session_id for s in store.list()]
    assert ids == ["b", "c", "a"]


def test_legacy_payloads_default_to_running_phase() -> None:
    """A SessionInfo built without ``phase=`` (e.g. an older snapshot
    payload) still appears in the listing and orders identically to
    other RUNNING sessions."""
    store = SessionStore()
    legacy = SessionInfo(session_id="legacy", updated_at_ms=2_000)
    new_running = _info("explicit", updated_at_ms=1_000)
    store.upsert(legacy)
    store.upsert(new_running)
    assert legacy.phase is SessionPhase.RUNNING
    ids = [s.session_id for s in store.list()]
    assert ids == ["legacy", "explicit"]


# ---------------------------------------------------------------------------
# V10 L2-#4: subagent fold
# ---------------------------------------------------------------------------


def test_default_listing_hides_subagents() -> None:
    """A subagent must not clutter the top-level session list."""
    store = SessionStore()
    store.upsert(_info("parent"))
    store.upsert(
        _info(
            "child",
            parent_session_id="parent",
            subagent_kind="tool_call",
        )
    )
    assert [s.session_id for s in store.list()] == ["parent"]
    assert [s.session_id for s in store.list_active()] == ["parent"]


def test_include_subagents_opt_in_returns_everything() -> None:
    store = SessionStore()
    store.upsert(_info("parent"))
    store.upsert(_info("child", parent_session_id="parent"))
    ids = {s.session_id for s in store.list(include_subagents=True)}
    assert ids == {"parent", "child"}


def test_list_subagents_of_returns_only_matching_children() -> None:
    store = SessionStore()
    store.upsert(_info("parent-a"))
    store.upsert(_info("parent-b"))
    store.upsert(_info("a-child-1", parent_session_id="parent-a"))
    store.upsert(_info("a-child-2", parent_session_id="parent-a"))
    store.upsert(_info("b-child-1", parent_session_id="parent-b"))
    a_kids = {s.session_id for s in store.list_subagents_of("parent-a")}
    assert a_kids == {"a-child-1", "a-child-2"}
    assert store.list_subagents_of("parent-b")[0].session_id == "b-child-1"
    # Unknown parent → empty.
    assert store.list_subagents_of("nobody") == []


def test_list_subagents_of_orders_actionable_first() -> None:
    store = SessionStore()
    store.upsert(_info("parent"))
    store.upsert(_info("running", parent_session_id="parent"))
    store.upsert(
        _info(
            "approval",
            parent_session_id="parent",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
        )
    )
    ids = [s.session_id for s in store.list_subagents_of("parent")]
    assert ids == ["approval", "running"]


def test_list_top_level_with_fold_attaches_count_and_summaries() -> None:
    store = SessionStore()
    store.upsert(_info("parent", title="Refactor"))
    store.upsert(
        _info(
            "child-1",
            parent_session_id="parent",
            title="grep loop",
            subagent_kind="tool_call",
        )
    )
    store.upsert(
        _info(
            "child-2",
            parent_session_id="parent",
            title="",  # title empty → falls back to subagent_kind
            subagent_kind="worktree",
        )
    )
    items = store.list_top_level_with_fold()
    assert len(items) == 1
    item = items[0]
    assert isinstance(item, SessionListItem)
    assert item.info.session_id == "parent"
    assert item.subagent_count == 2
    # Summaries pick title-or-kind, in actionable-first order
    # (both children are RUNNING here so insertion order via
    # updated_at_ms governs).
    assert set(item.subagent_summaries) == {"grep loop", "worktree"}


def test_list_top_level_with_fold_caps_summaries() -> None:
    store = SessionStore()
    store.upsert(_info("parent"))
    for i in range(5):
        store.upsert(
            _info(
                f"child-{i}",
                parent_session_id="parent",
                updated_at_ms=1_000 + i,
                title=f"sub-{i}",
            )
        )
    items = store.list_top_level_with_fold(max_summaries_per_parent=2)
    assert items[0].subagent_count == 5
    assert len(items[0].subagent_summaries) == 2


def test_list_top_level_with_fold_zero_summaries_still_counts() -> None:
    store = SessionStore()
    store.upsert(_info("parent"))
    store.upsert(_info("child", parent_session_id="parent", title="x"))
    items = store.list_top_level_with_fold(max_summaries_per_parent=0)
    assert items[0].subagent_count == 1
    assert items[0].subagent_summaries == ()


def test_list_top_level_with_fold_summary_label_fallback_chain() -> None:
    """``title.strip() or subagent_kind or session_id`` — proves the
    UI never gets an empty string in the summary list."""
    store = SessionStore()
    store.upsert(_info("parent"))
    store.upsert(
        _info(
            "only-kind",
            parent_session_id="parent",
            title="   ",  # whitespace → strip → empty → fallback
            subagent_kind="tool_call",
        )
    )
    store.upsert(
        _info(
            "only-id",
            parent_session_id="parent",
            title="",
            subagent_kind=None,
            updated_at_ms=999,
        )
    )
    item = store.list_top_level_with_fold()[0]
    assert "tool_call" in item.subagent_summaries
    assert "only-id" in item.subagent_summaries


def test_list_top_level_with_fold_state_filter_passes_through() -> None:
    store = SessionStore()
    store.upsert(_info("active", state=SessionState.ACTIVE))
    store.upsert(_info("closed", state=SessionState.CLOSED))
    store.upsert(_info("child", parent_session_id="active"))
    actives = store.list_top_level_with_fold(state=SessionState.ACTIVE)
    assert [i.info.session_id for i in actives] == ["active"]
    closed = store.list_top_level_with_fold(state=SessionState.CLOSED)
    assert [i.info.session_id for i in closed] == ["closed"]
    assert closed[0].subagent_count == 0  # the child belongs to "active"
