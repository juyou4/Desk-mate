"""V10 Phase 6 — approval router routes PERMISSION_RESOLVE."""

from __future__ import annotations

from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalRouter,
    ApprovalStatus,
    ApprovalStore,
)
from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)


def _approval(store: ApprovalStore, aid: str = "a1") -> None:
    store.add(Approval(approval_id=aid, prompt="ok?", created_at_ms=1_000))


def _action(
    *,
    allow: bool | None = None,
    decision: str | None = None,
    approval_id: str | None = "a1",
    kind: InteractionKind = InteractionKind.PERMISSION_RESOLVE,
    target: ActionTarget = ActionTarget.SYSTEM,
) -> InteractionAction:
    payload: dict[str, object] = {}
    if approval_id is not None:
        payload["approval_id"] = approval_id
    if allow is not None:
        payload["allow"] = allow
    if decision is not None:
        payload["decision"] = decision
    return InteractionAction(
        source=ActionSource.ISLAND,
        target=target,
        kind=kind,
        payload=payload,
    )


def test_allow_boolean_resolves_as_allow() -> None:
    store = ApprovalStore()
    _approval(store)
    router = ApprovalRouter(store, clock=lambda: 5_000)

    result = router.handle(_action(allow=True))

    assert result.handled
    assert result.effect == "approval.resolve.accepted"
    assert result.decision is ApprovalDecision.ALLOW
    got = store.get("a1")
    assert got is not None
    assert got.status is ApprovalStatus.RESOLVED
    assert got.decision is ApprovalDecision.ALLOW
    assert got.resolved_at_ms == 5_000


def test_allow_boolean_false_resolves_as_deny() -> None:
    store = ApprovalStore()
    _approval(store)
    router = ApprovalRouter(store, clock=lambda: 1_000)
    result = router.handle(_action(allow=False))
    assert result.handled
    assert result.decision is ApprovalDecision.DENY
    assert store.get("a1").decision is ApprovalDecision.DENY


def test_decision_string_form_is_accepted() -> None:
    store = ApprovalStore()
    _approval(store)
    router = ApprovalRouter(store, clock=lambda: 1_000)
    result = router.handle(_action(decision="allow", allow=None))
    assert result.decision is ApprovalDecision.ALLOW


def test_decision_string_invalid_is_rejected() -> None:
    store = ApprovalStore()
    _approval(store)
    router = ApprovalRouter(store)
    result = router.handle(_action(decision="maybe", allow=None))
    assert not result.handled
    assert result.effect == "missing_decision"


def test_missing_approval_id_is_rejected() -> None:
    store = ApprovalStore()
    router = ApprovalRouter(store)
    result = router.handle(_action(approval_id=None, allow=True))
    assert not result.handled
    assert result.effect == "missing_approval_id"


def test_unknown_approval_id_is_still_handled() -> None:
    store = ApprovalStore()
    router = ApprovalRouter(store)
    result = router.handle(_action(allow=True, approval_id="ghost"))
    # Router owns the verb; business mismatch is a handled-but-ineffective case.
    assert result.handled
    assert result.effect == "approval.resolve.unknown_id"


def test_already_terminal_is_handled_but_noop() -> None:
    store = ApprovalStore()
    _approval(store)
    store.resolve("a1", ApprovalDecision.ALLOW, 1_000)

    router = ApprovalRouter(store, clock=lambda: 2_000)
    result = router.handle(_action(allow=True))
    assert result.handled
    assert result.effect == "approval.resolve.already_terminal"
    # Decision remains the original ALLOW, unchanged.
    assert store.get("a1").decision is ApprovalDecision.ALLOW


def test_wrong_kind_is_not_handled() -> None:
    store = ApprovalStore()
    router = ApprovalRouter(store)
    result = router.handle(
        _action(allow=True, kind=InteractionKind.SESSION_JUMP)
    )
    assert not result.handled
    assert result.effect == "unknown_kind"
