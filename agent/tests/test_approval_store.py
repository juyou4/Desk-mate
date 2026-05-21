"""V10 Phase 6 — approval store mechanics."""

from __future__ import annotations

from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalStatus,
    ApprovalStore,
)


def _approval(
    aid: str = "a1",
    *,
    prompt: str = "Allow clipboard read?",
    created: int = 1_000,
    expires: int | None = None,
) -> Approval:
    return Approval(
        approval_id=aid,
        prompt=prompt,
        created_at_ms=created,
        expires_at_ms=expires,
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_add_inserts_new_approval() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    assert store.get("a1") is not None
    assert len(store) == 1


def test_add_with_same_id_replaces_prompt() -> None:
    store = ApprovalStore()
    store.add(_approval("a1", prompt="old"))
    store.add(_approval("a1", prompt="new"))
    got = store.get("a1")
    assert got is not None
    assert got.prompt == "new"


def test_resolve_flips_to_resolved_with_decision() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    updated = store.resolve("a1", ApprovalDecision.ALLOW, 5_000)
    assert updated is not None
    assert updated.status is ApprovalStatus.RESOLVED
    assert updated.decision is ApprovalDecision.ALLOW
    assert updated.resolved_at_ms == 5_000


def test_resolve_refuses_decision_none() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    assert store.resolve("a1", ApprovalDecision.NONE, 1_000) is None
    assert store.get("a1").status is ApprovalStatus.PENDING


def test_resolve_ignores_unknown_id() -> None:
    store = ApprovalStore()
    assert store.resolve("ghost", ApprovalDecision.ALLOW, 1_000) is None


def test_resolve_second_time_is_noop() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    store.resolve("a1", ApprovalDecision.ALLOW, 1_000)
    assert store.resolve("a1", ApprovalDecision.DENY, 2_000) is None


def test_expire_marks_expired() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    updated = store.expire("a1", 7_777)
    assert updated is not None
    assert updated.status is ApprovalStatus.EXPIRED
    assert updated.resolved_at_ms == 7_777


def test_cancel_marks_cancelled() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    updated = store.cancel("a1", 9_999)
    assert updated is not None
    assert updated.status is ApprovalStatus.CANCELLED


def test_terminal_states_are_immutable() -> None:
    store = ApprovalStore()
    store.add(_approval("a1"))
    store.resolve("a1", ApprovalDecision.ALLOW, 1_000)
    assert store.expire("a1", 2_000) is None
    assert store.cancel("a1", 2_000) is None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_list_pending_excludes_terminal() -> None:
    store = ApprovalStore()
    store.add(_approval("p", created=1_000))
    store.add(_approval("r", created=2_000))
    store.resolve("r", ApprovalDecision.DENY, 2_100)
    pending_ids = [a.approval_id for a in store.list_pending()]
    assert pending_ids == ["p"]


def test_pending_ids_returns_creation_order() -> None:
    store = ApprovalStore()
    store.add(_approval("b", created=2_000))
    store.add(_approval("a", created=1_000))
    assert store.pending_ids() == ["a", "b"]


def test_expire_due_flips_only_timed_out() -> None:
    store = ApprovalStore()
    store.add(_approval("early", expires=1_000))
    store.add(_approval("later", expires=10_000))
    store.add(_approval("no_ttl", expires=None))

    flipped = store.expire_due(5_000)
    ids = [a.approval_id for a in flipped]
    assert ids == ["early"]
    assert store.get("later").status is ApprovalStatus.PENDING
    assert store.get("no_ttl").status is ApprovalStatus.PENDING


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def test_subscribe_emits_expected_kinds() -> None:
    store = ApprovalStore()
    events: list[tuple[str, str]] = []
    unsub = store.subscribe(lambda e: events.append((e.kind, e.approval_id)))

    store.add(_approval("a"))
    store.add(_approval("b"))
    store.resolve("a", ApprovalDecision.ALLOW, 1_000)
    store.cancel("b", 2_000)
    unsub()
    store.add(_approval("c"))

    assert events == [
        ("add", "a"),
        ("add", "b"),
        ("resolve", "a"),
        ("cancel", "b"),
    ]


def test_subscriber_error_does_not_break_store() -> None:
    store = ApprovalStore()

    def broken(_event: object) -> None:
        raise RuntimeError("boom")

    store.subscribe(broken)
    store.add(_approval("a"))
    assert store.get("a") is not None
