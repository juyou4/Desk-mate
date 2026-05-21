"""V10 L2-#4 — reminder store mechanics."""

from __future__ import annotations

from deskmate_agent.protocol.state import Priority
from deskmate_agent.reminders import Reminder, ReminderStatus, ReminderStore


def _reminder(
    rid: str = "r1",
    *,
    due: int = 10_000,
    priority: Priority = Priority.P1,
    status: ReminderStatus = ReminderStatus.PENDING,
) -> Reminder:
    return Reminder(
        reminder_id=rid,
        text=f"remind {rid}",
        due_at_ms=due,
        created_at_ms=due - 1_000,
        priority=priority,
        status=status,
    )


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_add_inserts_reminder() -> None:
    store = ReminderStore()
    store.add(_reminder("r1"))
    assert store.get("r1") is not None


def test_add_replaces_on_duplicate_id() -> None:
    store = ReminderStore()
    store.add(_reminder("r1", due=1_000))
    store.add(_reminder("r1", due=2_000))
    got = store.get("r1")
    assert got is not None
    assert got.due_at_ms == 2_000
    assert len(store) == 1


def test_mark_fired_transitions_pending_only() -> None:
    store = ReminderStore()
    store.add(_reminder("r1"))
    first = store.mark_fired("r1", 9_999, "b1")
    assert first is not None
    assert first.status is ReminderStatus.FIRED
    assert first.bubble_id == "b1"
    assert first.fired_at_ms == 9_999
    # Second call is a no-op because status is already FIRED.
    assert store.mark_fired("r1", 10_000, "b2") is None


def test_mark_fired_missing_returns_none() -> None:
    store = ReminderStore()
    assert store.mark_fired("ghost", 1_000, "b1") is None


def test_mark_dismissed_requires_fired() -> None:
    store = ReminderStore()
    store.add(_reminder("r1"))
    # Can't dismiss a PENDING reminder (use cancel for that).
    assert store.mark_dismissed("r1", 1_000) is None
    store.mark_fired("r1", 1_000, "b1")
    done = store.mark_dismissed("r1", 2_000)
    assert done is not None
    assert done.status is ReminderStatus.DISMISSED
    assert done.resolved_at_ms == 2_000


def test_cancel_marks_pending_or_fired() -> None:
    store = ReminderStore()
    store.add(_reminder("r1"))
    cancelled = store.cancel("r1", 100)
    assert cancelled is not None
    assert cancelled.status is ReminderStatus.CANCELLED


def test_cancel_ignores_already_dismissed() -> None:
    store = ReminderStore()
    store.add(_reminder("r1"))
    store.mark_fired("r1", 1_000, "b1")
    store.mark_dismissed("r1", 2_000)
    assert store.cancel("r1") is None


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_list_due_returns_only_pending_ready() -> None:
    store = ReminderStore()
    store.add(_reminder("early", due=1_000))
    store.add(_reminder("later", due=10_000))
    store.add(_reminder("fired", due=500))
    store.mark_fired("fired", 501, "b1")

    due_now = store.list_due(2_000)
    ids = [r.reminder_id for r in due_now]
    assert ids == ["early"]  # fired one excluded even though due <= now


def test_list_sorts_by_due_at() -> None:
    store = ReminderStore()
    store.add(_reminder("a", due=10_000))
    store.add(_reminder("b", due=5_000))
    store.add(_reminder("c", due=7_000))
    ids = [r.reminder_id for r in store.list()]
    assert ids == ["b", "c", "a"]


def test_next_due_at_picks_smallest_pending() -> None:
    store = ReminderStore()
    assert store.next_due_at() is None
    store.add(_reminder("a", due=1_000))
    store.add(_reminder("b", due=500))
    assert store.next_due_at() == 500
    store.mark_fired("b", 600, "bubble")
    assert store.next_due_at() == 1_000


def test_list_filters_by_status() -> None:
    store = ReminderStore()
    store.add(_reminder("p"))
    store.add(_reminder("f"))
    store.mark_fired("f", 1_000, "b1")
    pending_ids = [r.reminder_id for r in store.list(status=ReminderStatus.PENDING)]
    assert pending_ids == ["p"]


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------


def test_subscribers_see_kind_transitions() -> None:
    store = ReminderStore()
    events: list[tuple[str, str]] = []
    unsub = store.subscribe(lambda e: events.append((e.kind, e.reminder_id)))

    store.add(_reminder("r1"))
    store.mark_fired("r1", 1_000, "b1")
    store.mark_dismissed("r1", 2_000)
    unsub()
    store.cancel("r1")  # should not produce an event after unsubscribe

    assert events == [
        ("add", "r1"),
        ("fire", "r1"),
        ("dismiss", "r1"),
    ]


def test_subscriber_error_does_not_break_store() -> None:
    store = ReminderStore()

    def broken(_event: object) -> None:
        raise RuntimeError("boom")

    store.subscribe(broken)
    store.add(_reminder("r1"))
    assert store.get("r1") is not None
