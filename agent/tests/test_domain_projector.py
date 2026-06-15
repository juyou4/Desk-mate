"""V10 Phase 7 — DomainStateProjector fans store changes into intents."""

from __future__ import annotations

import pytest

from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalStore,
)
from deskmate_agent.projector import DomainStateProjector
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.protocol.state import AgentMood, Priority
from deskmate_agent.sessions import (
    SessionInfo,
    SessionPhase,
    SessionState,
    SessionStore,
)


def _approval(aid: str, *, created: int = 1_000) -> Approval:
    return Approval(approval_id=aid, prompt="ok?", created_at_ms=created)


def _session(sid: str, *, updated: int = 1_000) -> SessionInfo:
    return SessionInfo(
        session_id=sid,
        title=sid,
        state=SessionState.ACTIVE,
        updated_at_ms=updated,
    )


class _CapturingSink:
    """Async sink that records everything it receives."""

    def __init__(self) -> None:
        self.intents: list[CompanionIntent] = []
        self.fail_next = 0  # number of upcoming calls that should raise

    async def __call__(self, intent: CompanionIntent) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated sink failure")
        self.intents.append(intent)


def _build() -> tuple[
    ApprovalStore, SessionStore, DomainStateProjector, _CapturingSink
]:
    approvals = ApprovalStore()
    sessions = SessionStore()
    sink = _CapturingSink()
    projector = DomainStateProjector(
        approval_store=approvals,
        session_store=sessions,
        intent_sink=sink,
    )
    return approvals, sessions, projector, sink


# ---------------------------------------------------------------------------
# Basic change notification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_approval_emits_update_domain_state() -> None:
    approvals, _, projector, sink = _build()
    projector.start()

    approvals.add(_approval("a1"))
    await projector.flush()

    assert len(sink.intents) == 1
    intent = sink.intents[0]
    assert intent.kind is IntentKind.UPDATE_DOMAIN_STATE
    assert intent.payload["domain_state"]["pending_approvals"] == ["a1"]


@pytest.mark.asyncio
async def test_resolve_approval_emits_empty_pending() -> None:
    approvals, _, projector, sink = _build()
    projector.start()
    approvals.add(_approval("a1"))
    await projector.flush()
    assert sink.intents[-1].payload["domain_state"]["pending_approvals"] == ["a1"]

    approvals.resolve("a1", ApprovalDecision.ALLOW, 2_000)
    await projector.flush()

    assert len(sink.intents) == 2
    assert sink.intents[-1].payload["domain_state"]["pending_approvals"] == []


@pytest.mark.asyncio
async def test_session_store_event_also_triggers_emit() -> None:
    _, sessions, projector, sink = _build()
    projector.start()

    sessions.upsert(_session("s1", updated=5_000))
    await projector.flush()

    assert len(sink.intents) == 1
    assert sink.intents[0].payload["domain_state"]["active_session_id"] == "s1"


@pytest.mark.asyncio
async def test_projection_marks_pending_approval_as_p0_alert() -> None:
    approvals, _, projector, sink = _build()
    projector.start()

    approvals.add(_approval("a1"))
    await projector.flush()

    payload = sink.intents[-1].payload["domain_state"]
    assert payload["current_priority"] == Priority.P0.value
    assert payload["agent_mood"] == AgentMood.ALERT.value


@pytest.mark.asyncio
async def test_projection_derives_attention_from_active_session_phase() -> None:
    _, sessions, projector, sink = _build()
    projector.start()

    sessions.upsert(
        SessionInfo(
            session_id="s1",
            title="Codex edit",
            state=SessionState.ACTIVE,
            priority=Priority.P2,
            phase=SessionPhase.RUNNING_TOOL,
            source="codex",
            updated_at_ms=5_000,
        )
    )
    await projector.flush()

    payload = sink.intents[-1].payload["domain_state"]
    assert payload["current_priority"] == Priority.P1.value
    assert payload["agent_mood"] == AgentMood.WORKING.value


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_identical_projection_is_not_emitted_twice() -> None:
    approvals, _, projector, sink = _build()
    projector.start()

    approvals.add(_approval("a1"))
    await projector.flush()
    assert len(sink.intents) == 1

    # Re-adding the same id with same prompt yields the same projection,
    # because pending_approvals is still ["a1"] — no wire spam.
    approvals.add(_approval("a1"))
    await projector.flush()

    assert len(sink.intents) == 1


# ---------------------------------------------------------------------------
# Burst coalescing (drain pattern)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rapid_changes_collapse_to_final_state() -> None:
    """Multiple mutations inside the same sync burst must produce at
    most one intent reflecting the *final* projection."""
    approvals, _, projector, sink = _build()
    projector.start()

    approvals.add(_approval("a1"))
    approvals.add(_approval("a2"))
    approvals.resolve("a1", ApprovalDecision.ALLOW, 10)
    await projector.flush()

    # Exactly one emission with the final pending set [a2].
    assert len(sink.intents) == 1
    assert sink.intents[-1].payload["domain_state"]["pending_approvals"] == ["a2"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    approvals, _, projector, sink = _build()
    projector.start()
    projector.start()  # second call must not double-subscribe

    approvals.add(_approval("a1"))
    await projector.flush()

    assert len(sink.intents) == 1  # not 2


@pytest.mark.asyncio
async def test_stop_unsubscribes_from_all_stores() -> None:
    approvals, sessions, projector, sink = _build()
    projector.start()
    await projector.stop()

    approvals.add(_approval("a1"))
    sessions.upsert(_session("s1"))
    # No event loop task should be scheduled; await a tick anyway to be sure.
    await projector.flush()

    assert sink.intents == []


@pytest.mark.asyncio
async def test_stop_before_start_is_safe() -> None:
    _, _, projector, _ = _build()
    await projector.stop()  # must not raise


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_future_emissions() -> None:
    approvals, _, projector, sink = _build()
    sink.fail_next = 1
    projector.start()

    approvals.add(_approval("a1"))
    await projector.flush()
    # First emit raised → not recorded.
    assert sink.intents == []

    approvals.resolve("a1", ApprovalDecision.ALLOW, 2_000)
    await projector.flush()

    assert len(sink.intents) == 1
    assert sink.intents[-1].payload["domain_state"]["pending_approvals"] == []


# ---------------------------------------------------------------------------
# Phase 15-i: coding-today rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_coding_today_ms_emits_intent_with_value() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_ms(12_345)
    await projector.flush()
    assert len(sink.intents) == 1
    payload = sink.intents[0].payload["domain_state"]
    assert payload["coding_today_ms"] == 12_345


@pytest.mark.asyncio
async def test_set_coding_today_ms_dedups_identical_values() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_ms(5_000)
    await projector.flush()
    projector.set_coding_today_ms(5_000)
    await projector.flush()
    assert len(sink.intents) == 1


@pytest.mark.asyncio
async def test_set_coding_today_ms_negative_clamps_to_zero() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_ms(-500)
    await projector.flush()
    # ``-500`` clamps to ``0`` which equals the initial value; no
    # intent should fire.
    assert sink.intents == []


@pytest.mark.asyncio
async def test_coding_today_ms_included_in_approval_triggered_projection() -> None:
    approvals, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_ms(9_999)
    approvals.add(_approval("a1"))
    await projector.flush()

    payloads = [intent.payload["domain_state"] for intent in sink.intents]
    assert any(p["coding_today_ms"] == 9_999 for p in payloads)


@pytest.mark.asyncio
async def test_set_coding_today_by_ide_emits_sorted_breakdown() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_by_ide({"Xcode": 3_600_000, "Zed": 900_000})
    await projector.flush()
    payload = sink.intents[-1].payload["domain_state"]
    assert payload["coding_today_by_ide"] == {
        "Xcode": 3_600_000,
        "Zed": 900_000,
    }


@pytest.mark.asyncio
async def test_set_coding_today_by_ide_drops_zero_entries() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_by_ide({"Xcode": 1_000, "Ghost": 0})
    await projector.flush()
    payload = sink.intents[-1].payload["domain_state"]
    assert payload["coding_today_by_ide"] == {"Xcode": 1_000}


@pytest.mark.asyncio
async def test_set_degradation_level_emits_intent() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_degradation_level(3)
    await projector.flush()
    payload = sink.intents[-1].payload["domain_state"]
    assert payload["degradation_level"] == 3


@pytest.mark.asyncio
async def test_set_degradation_level_clamps_and_dedups() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_degradation_level(-5)  # clamps to 0 → no change
    await projector.flush()
    assert sink.intents == []

    projector.set_degradation_level(99)  # clamps to 6
    await projector.flush()
    assert sink.intents[-1].payload["domain_state"]["degradation_level"] == 6

    projector.set_degradation_level(6)  # same value → no new intent
    await projector.flush()
    assert len(sink.intents) == 1


@pytest.mark.asyncio
async def test_set_coding_today_by_ide_dedups_identical_maps() -> None:
    _, _, projector, sink = _build()
    projector.start()

    projector.set_coding_today_by_ide({"Xcode": 5_000})
    await projector.flush()
    projector.set_coding_today_by_ide({"Xcode": 5_000})
    await projector.flush()
    # Only one intent, even though we called the setter twice.
    by_ide_payloads = [
        intent.payload["domain_state"]["coding_today_by_ide"]
        for intent in sink.intents
    ]
    assert by_ide_payloads == [{"Xcode": 5_000}]
