"""V10 Phase 7 — ApprovalSurfacePublisher maps approval lifecycle to intents."""

from __future__ import annotations

import pytest

from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalStore,
    ApprovalSurfacePublisher,
)
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind


def _approval(aid: str = "a1") -> Approval:
    return Approval(approval_id=aid, prompt="Allow X?", created_at_ms=1_000)


class _CapturingSink:
    def __init__(self) -> None:
        self.intents: list[CompanionIntent] = []
        self.fail_next = 0

    async def __call__(self, intent: CompanionIntent) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("sink boom")
        self.intents.append(intent)


def _build() -> tuple[ApprovalStore, ApprovalSurfacePublisher, _CapturingSink]:
    store = ApprovalStore()
    sink = _CapturingSink()
    publisher = ApprovalSurfacePublisher(store, sink)
    return store, publisher, sink


# ---------------------------------------------------------------------------
# Show on add
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_emits_show_pet_bubble_with_approval_hint_spec() -> None:
    store, publisher, sink = _build()
    publisher.start()

    store.add(_approval("a1"))
    await publisher.flush()

    assert len(sink.intents) == 1
    intent = sink.intents[0]
    assert intent.kind is IntentKind.SHOW_PET_BUBBLE
    bubble = intent.payload["bubble"]
    assert bubble["id"] == "approval-a1"
    assert bubble["kind"] == "approval_hint"
    assert bubble["text"] == "Allow X?"
    assert bubble["ttl_ms"] is None  # must not auto-hide

    actions = bubble["actions"]
    assert len(actions) == 2
    labels = [a["label"] for a in actions]
    assert labels == ["Allow", "Deny"]
    for a in actions:
        assert a["interaction_kind"] == "permission.resolve"
        assert a["payload"]["approval_id"] == "a1"
    assert actions[0]["payload"]["allow"] is True
    assert actions[1]["payload"]["allow"] is False


@pytest.mark.asyncio
async def test_bubble_id_is_deterministic() -> None:
    # Important so reconnect reconciliation doesn't produce duplicates.
    assert ApprovalSurfacePublisher.bubble_id_for("xyz") == "approval-xyz"


# ---------------------------------------------------------------------------
# Dismiss on terminal transitions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_emits_dismiss_pet_bubble() -> None:
    store, publisher, sink = _build()
    publisher.start()
    store.add(_approval("a1"))
    await publisher.flush()
    assert len(sink.intents) == 1

    store.resolve("a1", ApprovalDecision.ALLOW, 2_000)
    await publisher.flush()

    assert len(sink.intents) == 2
    dismiss = sink.intents[-1]
    assert dismiss.kind is IntentKind.DISMISS_PET_BUBBLE
    assert dismiss.payload["bubble_id"] == "approval-a1"
    assert dismiss.payload["approval_id"] == "a1"


@pytest.mark.asyncio
async def test_expire_also_dismisses_bubble() -> None:
    store, publisher, sink = _build()
    publisher.start()
    store.add(_approval("a1"))
    await publisher.flush()

    store.expire("a1", 3_000)
    await publisher.flush()

    assert sink.intents[-1].kind is IntentKind.DISMISS_PET_BUBBLE
    assert sink.intents[-1].payload["bubble_id"] == "approval-a1"


@pytest.mark.asyncio
async def test_cancel_also_dismisses_bubble() -> None:
    store, publisher, sink = _build()
    publisher.start()
    store.add(_approval("a1"))
    await publisher.flush()

    store.cancel("a1", 4_000)
    await publisher.flush()

    assert sink.intents[-1].kind is IntentKind.DISMISS_PET_BUBBLE


# ---------------------------------------------------------------------------
# Lifecycle + robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    store, publisher, sink = _build()
    publisher.start()
    publisher.start()  # second subscribe would double the intents
    store.add(_approval("a1"))
    await publisher.flush()
    assert len(sink.intents) == 1


@pytest.mark.asyncio
async def test_stop_unsubscribes() -> None:
    store, publisher, sink = _build()
    publisher.start()
    await publisher.stop()

    store.add(_approval("a1"))
    # give the (absent) task a chance to run
    await publisher.flush()

    assert sink.intents == []


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_future_emissions() -> None:
    store, publisher, sink = _build()
    sink.fail_next = 1
    publisher.start()

    store.add(_approval("a1"))
    await publisher.flush()
    assert sink.intents == []  # first (show) intent raised

    store.resolve("a1", ApprovalDecision.ALLOW, 2_000)
    await publisher.flush()

    # Dismiss intent was captured despite earlier failure.
    assert len(sink.intents) == 1
    assert sink.intents[-1].kind is IntentKind.DISMISS_PET_BUBBLE
