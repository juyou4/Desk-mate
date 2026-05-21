"""V10 L2-#4 — reminder scheduler fires intents with the right shape."""

from __future__ import annotations

import pytest

from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.protocol.state import Priority
from deskmate_agent.reminders import (
    Reminder,
    ReminderScheduler,
    ReminderStatus,
    ReminderStore,
)


def _reminder(rid: str, due: int) -> Reminder:
    return Reminder(
        reminder_id=rid,
        text=f"remind {rid}",
        due_at_ms=due,
        created_at_ms=due - 1_000,
        priority=Priority.P1,
    )


def _build(store: ReminderStore, ids: list[str], now: int) -> tuple[
    ReminderScheduler, list[CompanionIntent]
]:
    captured: list[CompanionIntent] = []

    async def sink(intent: CompanionIntent) -> None:
        captured.append(intent)

    scheduler = ReminderScheduler(
        store,
        sink,
        clock=lambda: now,
        id_factory=lambda: f"bubble-{ids.pop(0)}",
    )
    return scheduler, captured


# ---------------------------------------------------------------------------
# process_due
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_due_fires_pending_reminders() -> None:
    store = ReminderStore()
    store.add(_reminder("r1", due=1_000))
    store.add(_reminder("r2", due=2_000))
    scheduler, captured = _build(store, ["one", "two"], now=2_500)

    fired = await scheduler.process_due(2_500)

    assert fired == 2
    assert [i.kind for i in captured] == [
        IntentKind.SHOW_PET_BUBBLE,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    assert store.get("r1").status is ReminderStatus.FIRED
    assert store.get("r2").status is ReminderStatus.FIRED


@pytest.mark.asyncio
async def test_process_due_skips_future_reminders() -> None:
    store = ReminderStore()
    store.add(_reminder("early", due=1_000))
    store.add(_reminder("later", due=10_000))
    scheduler, captured = _build(store, ["one"], now=2_000)

    fired = await scheduler.process_due(2_000)

    assert fired == 1
    assert len(captured) == 1
    assert store.get("later").status is ReminderStatus.PENDING


@pytest.mark.asyncio
async def test_process_due_is_idempotent() -> None:
    store = ReminderStore()
    store.add(_reminder("r1", due=1_000))
    scheduler, captured = _build(store, ["one"], now=2_000)

    assert await scheduler.process_due(2_000) == 1
    assert await scheduler.process_due(2_000) == 0  # already fired
    assert len(captured) == 1


# ---------------------------------------------------------------------------
# Intent payload shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fired_intent_is_show_pet_bubble_with_reminder_spec() -> None:
    store = ReminderStore()
    store.add(_reminder("r1", due=1_000))
    scheduler, captured = _build(store, ["one"], now=2_000)

    await scheduler.process_due(2_000)

    intent = captured[0]
    assert intent.kind is IntentKind.SHOW_PET_BUBBLE
    payload = intent.payload
    assert payload["reminder_id"] == "r1"
    bubble = payload["bubble"]
    assert bubble["kind"] == "reminder"
    assert bubble["id"] == "bubble-one"
    assert bubble["text"] == "remind r1"
    assert bubble["priority"] == "P1"
    assert bubble["ttl_ms"] is None  # reminders don't auto-dismiss
    assert len(bubble["actions"]) == 1
    action = bubble["actions"][0]
    assert action["interaction_kind"] == "surface.dismiss"
    assert action["payload"]["reminder_id"] == "r1"
    assert action["payload"]["bubble_id"] == "bubble-one"


@pytest.mark.asyncio
async def test_fired_reminder_stamps_bubble_id_and_fired_at() -> None:
    store = ReminderStore()
    store.add(_reminder("r1", due=1_000))
    scheduler, _ = _build(store, ["one"], now=5_000)

    await scheduler.process_due(5_000)

    got = store.get("r1")
    assert got is not None
    assert got.bubble_id == "bubble-one"
    assert got.fired_at_ms == 5_000


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_failure_does_not_break_subsequent_reminders() -> None:
    store = ReminderStore()
    store.add(_reminder("first", due=1_000))
    store.add(_reminder("second", due=1_000))

    captured: list[CompanionIntent] = []
    call_count = 0

    async def flaky_sink(intent: CompanionIntent) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient bridge error")
        captured.append(intent)

    scheduler = ReminderScheduler(
        store,
        flaky_sink,
        clock=lambda: 2_000,
        id_factory=lambda: "bubble-x",
    )
    # process_due should still report the successful fire count and
    # leave the flaky reminder in FIRED state (UI loss is a separate
    # concern, logged via _LOG).
    await scheduler.process_due(2_000)

    assert call_count == 2
    assert len(captured) == 1
    # Both reminders transitioned to FIRED despite the sink error.
    assert store.get("first").status is ReminderStatus.FIRED
    assert store.get("second").status is ReminderStatus.FIRED


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_stop_are_clean() -> None:
    store = ReminderStore()
    scheduler, _ = _build(store, [], now=0)
    await scheduler.start()
    await scheduler.stop()
    # Second stop must not raise.
    await scheduler.stop()
