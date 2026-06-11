"""V10 Phase 6 — approval router routes PERMISSION_RESOLVE."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalRouter,
    ApprovalStatus,
    ApprovalStore,
)
from deskmate_agent.projector import DomainStateProjector
from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)
from deskmate_agent.protocol.intents import IntentKind
from deskmate_agent.sessions import SessionInfo, SessionPhase, SessionStore


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


async def _noop_sink(intent) -> None:
    """No-op intent sink for tests that don't inspect emitted intents."""


def _make_router(
    store: ApprovalStore, *, clock=None
) -> ApprovalRouter:
    """Build an ApprovalRouter with stub dependencies for unit tests."""
    session_store = SessionStore()
    domain_projector = DomainStateProjector(
        approval_store=store,
        session_store=session_store,
        intent_sink=_noop_sink,
    )
    kwargs: dict = {
        "intent_sink": _noop_sink,
        "session_store": session_store,
        "domain_projector": domain_projector,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return ApprovalRouter(store, **kwargs)


@pytest.mark.asyncio
async def test_allow_boolean_resolves_as_allow() -> None:
    store = ApprovalStore()
    _approval(store)
    router = _make_router(store, clock=lambda: 5_000)

    result = await router.handle(_action(allow=True))

    assert result.handled
    assert result.effect == "approval.resolve.accepted"
    assert result.decision is ApprovalDecision.ALLOW
    got = store.get("a1")
    assert got is not None
    assert got.status is ApprovalStatus.RESOLVED
    assert got.decision is ApprovalDecision.ALLOW
    assert got.resolved_at_ms == 5_000


@pytest.mark.asyncio
async def test_allow_boolean_false_resolves_as_deny() -> None:
    store = ApprovalStore()
    _approval(store)
    router = _make_router(store, clock=lambda: 1_000)
    result = await router.handle(_action(allow=False))
    assert result.handled
    assert result.decision is ApprovalDecision.DENY
    assert store.get("a1").decision is ApprovalDecision.DENY


@pytest.mark.asyncio
async def test_decision_string_form_is_accepted() -> None:
    store = ApprovalStore()
    _approval(store)
    router = _make_router(store, clock=lambda: 1_000)
    result = await router.handle(_action(decision="allow", allow=None))
    assert result.decision is ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_decision_string_invalid_is_rejected() -> None:
    store = ApprovalStore()
    _approval(store)
    router = _make_router(store)
    result = await router.handle(_action(decision="maybe", allow=None))
    assert not result.handled
    assert result.effect == "missing_decision"


@pytest.mark.asyncio
async def test_missing_approval_id_is_rejected() -> None:
    store = ApprovalStore()
    router = _make_router(store)
    result = await router.handle(_action(approval_id=None, allow=True))
    assert not result.handled
    assert result.effect == "missing_approval_id"


@pytest.mark.asyncio
async def test_unknown_approval_id_is_still_handled() -> None:
    store = ApprovalStore()
    router = _make_router(store)
    result = await router.handle(_action(allow=True, approval_id="ghost"))
    # Router owns the verb; business mismatch is a handled-but-ineffective case.
    assert result.handled
    assert result.effect == "approval.resolve.unknown_id"


@pytest.mark.asyncio
async def test_already_terminal_is_handled_but_noop() -> None:
    store = ApprovalStore()
    _approval(store)
    store.resolve("a1", ApprovalDecision.ALLOW, 1_000)

    router = _make_router(store, clock=lambda: 2_000)
    result = await router.handle(_action(allow=True))
    assert result.handled
    assert result.effect == "approval.resolve.already_terminal"
    # Decision remains the original ALLOW, unchanged.
    assert store.get("a1").decision is ApprovalDecision.ALLOW


@pytest.mark.asyncio
async def test_wrong_kind_is_not_handled() -> None:
    store = ApprovalStore()
    router = _make_router(store)
    result = await router.handle(
        _action(allow=True, kind=InteractionKind.SESSION_JUMP)
    )
    assert not result.handled
    assert result.effect == "unknown_kind"


@pytest.mark.asyncio
@settings(max_examples=50)
@given(
    approval_id=st.text(
        min_size=1,
        max_size=20,
        alphabet=st.characters(whitelist_categories=("L", "N")),
    )
)
async def test_resolve_unknown_id_emits_no_intents(approval_id):
    """Property 1: unknown id → [] intents, store unchanged.

    **Validates: Requirements R1.1, R1.5, R1.8**
    """
    store = ApprovalStore()
    emitted: list = []

    async def sink(intent):
        emitted.append(intent)

    session_store = SessionStore()
    projector = DomainStateProjector(
        approval_store=store,
        session_store=session_store,
        intent_sink=sink,
    )
    router = ApprovalRouter(
        store,
        intent_sink=sink,
        session_store=session_store,
        domain_projector=projector,
    )
    action = _action(allow=True, approval_id=approval_id)
    await router.handle(action)
    assert len(emitted) == 0
    assert store.get(approval_id) is None


@pytest.mark.asyncio
async def test_resolve_emits_dismiss_island_with_surface_id():
    """R1.1: resolve emits dismiss_island targeting surface_id."""
    store = ApprovalStore()
    store.add(
        Approval(
            approval_id="a1",
            prompt="ok?",
            created_at_ms=1000,
            surface_id="approval:a1",
            session_id="s1",
        )
    )
    emitted: list = []

    async def sink(intent):
        emitted.append(intent)

    session_store = SessionStore()
    session_store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            created_at_ms=1000,
            updated_at_ms=1000,
        )
    )
    projector = DomainStateProjector(
        approval_store=store,
        session_store=session_store,
        intent_sink=sink,
    )
    router = ApprovalRouter(
        store,
        intent_sink=sink,
        session_store=session_store,
        domain_projector=projector,
    )
    await router.handle(_action(allow=True))
    # Should have dismiss_island as first intent
    dismiss_intents = [i for i in emitted if i.kind == IntentKind.DISMISS_ISLAND]
    assert len(dismiss_intents) == 1
    assert dismiss_intents[0].payload["id"] == "approval:a1"
    # Session phase should be RUNNING
    assert session_store.get("s1").phase == SessionPhase.RUNNING


@pytest.mark.asyncio
async def test_resolve_records_approval_decision_on_session():
    store = ApprovalStore()
    store.add(
        Approval(
            approval_id="a1",
            prompt="Allow shell?",
            created_at_ms=1000,
            surface_id="approval:a1",
            session_id="s1",
            extras={
                "risk_level": "high",
                "risk_summary": "Shell command may modify files.",
                "approval_preview": "cmd: sudo rm -rf build/cache",
                "tool_name": "Bash",
                "command": "sudo rm -rf build/cache",
            },
        )
    )
    emitted: list = []

    async def sink(intent):
        emitted.append(intent)

    session_store = SessionStore()
    session_store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            created_at_ms=1000,
            updated_at_ms=1000,
            extras={"kept": "yes"},
        )
    )
    projector = DomainStateProjector(
        approval_store=store,
        session_store=session_store,
        intent_sink=sink,
    )
    router = ApprovalRouter(
        store,
        intent_sink=sink,
        session_store=session_store,
        domain_projector=projector,
        clock=lambda: 5_000,
    )

    result = await router.handle(_action(allow=False))

    assert result.effect == "approval.resolve.accepted"
    session = session_store.get("s1")
    assert session is not None
    assert session.phase == SessionPhase.RUNNING
    assert session.updated_at_ms == 5_000
    assert session.extras["kept"] == "yes"
    assert session.extras["last_approval_id"] == "a1"
    assert session.extras["last_approval_decision"] == "deny"
    assert session.extras["last_approval_prompt"] == "Allow shell?"
    assert session.extras["last_approval_resolved_at_ms"] == "5000"
    assert session.extras["last_approval_risk_level"] == "high"
    assert session.extras["last_approval_preview"] == "cmd: sudo rm -rf build/cache"
    assert session.extras["last_approval_tool_name"] == "Bash"
    assert session.extras["last_approval_command"] == "sudo rm -rf build/cache"


@pytest.mark.asyncio
async def test_resolve_terminal_session_phase_unchanged():
    """R1.3: if session already COMPLETED, phase stays."""
    store = ApprovalStore()
    store.add(
        Approval(
            approval_id="a1",
            prompt="ok?",
            created_at_ms=1000,
            surface_id="approval:a1",
            session_id="s1",
        )
    )
    emitted: list = []

    async def sink(intent):
        emitted.append(intent)

    session_store = SessionStore()
    session_store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.COMPLETED,
            created_at_ms=1000,
            updated_at_ms=1000,
        )
    )
    projector = DomainStateProjector(
        approval_store=store,
        session_store=session_store,
        intent_sink=sink,
    )
    router = ApprovalRouter(
        store,
        intent_sink=sink,
        session_store=session_store,
        domain_projector=projector,
    )
    await router.handle(_action(allow=True))
    session = session_store.get("s1")
    assert session is not None
    assert session.phase == SessionPhase.COMPLETED
    assert session.extras["last_approval_id"] == "a1"
    assert session.extras["last_approval_decision"] == "allow"
