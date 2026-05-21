"""Observable, in-memory :class:`ReminderStore` (V10 L2-#4).

The store is the sole source of truth for reminder state. The scheduler
reads ``list_due`` / ``next_due_at`` to plan wakeups; subscribers (App,
diagnostics) listen to :class:`ReminderStoreEvent` for live updates.

Thread model: all mutations run on the asyncio event loop, so no locking
is needed. Subscriber callbacks must be sync + fast.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..logging_setup import get_logger
from .model import Reminder, ReminderStatus

_LOG = get_logger("deskmate_agent.reminders.store")


ReminderStoreEventKind = Literal["add", "fire", "dismiss", "cancel", "update"]


@dataclass
class ReminderStoreEvent:
    kind: ReminderStoreEventKind
    reminder_id: str
    reminder: Reminder | None
    ts_ms: int = 0


Subscription = Callable[[ReminderStoreEvent], None]
Unsubscribe = Callable[[], None]


class ReminderStore:
    """Observable runtime index of reminders."""

    def __init__(self) -> None:
        self._by_id: dict[str, Reminder] = {}
        self._subs: list[Subscription] = []

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(self, reminder: Reminder) -> Reminder:
        """Insert a new reminder. If ``reminder_id`` already exists, the
        entry is **replaced** (so callers can re-add after cancellation
        without needing a remove step)."""
        self._by_id[reminder.reminder_id] = reminder
        self._emit(ReminderStoreEvent(
            kind="add",
            reminder_id=reminder.reminder_id,
            reminder=reminder,
            ts_ms=reminder.created_at_ms,
        ))
        return reminder

    def mark_fired(
        self, reminder_id: str, ts_ms: int, bubble_id: str
    ) -> Reminder | None:
        existing = self._by_id.get(reminder_id)
        if existing is None or existing.status is not ReminderStatus.PENDING:
            return None
        updated = existing.model_copy(update={
            "status": ReminderStatus.FIRED,
            "fired_at_ms": ts_ms,
            "bubble_id": bubble_id,
        })
        self._by_id[reminder_id] = updated
        self._emit(ReminderStoreEvent(
            kind="fire", reminder_id=reminder_id, reminder=updated, ts_ms=ts_ms
        ))
        return updated

    def mark_dismissed(self, reminder_id: str, ts_ms: int) -> Reminder | None:
        existing = self._by_id.get(reminder_id)
        if existing is None or existing.status is not ReminderStatus.FIRED:
            return None
        updated = existing.model_copy(update={
            "status": ReminderStatus.DISMISSED,
            "resolved_at_ms": ts_ms,
        })
        self._by_id[reminder_id] = updated
        self._emit(ReminderStoreEvent(
            kind="dismiss",
            reminder_id=reminder_id,
            reminder=updated,
            ts_ms=ts_ms,
        ))
        return updated

    def cancel(self, reminder_id: str, ts_ms: int = 0) -> Reminder | None:
        existing = self._by_id.get(reminder_id)
        if existing is None:
            return None
        if existing.status is ReminderStatus.DISMISSED:
            return None  # already terminal, don't silently mutate
        updated = existing.model_copy(update={
            "status": ReminderStatus.CANCELLED,
            "resolved_at_ms": ts_ms or existing.resolved_at_ms,
        })
        self._by_id[reminder_id] = updated
        self._emit(ReminderStoreEvent(
            kind="cancel",
            reminder_id=reminder_id,
            reminder=updated,
            ts_ms=ts_ms,
        ))
        return updated

    def clear(self) -> None:
        self._by_id.clear()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, reminder_id: str) -> Reminder | None:
        return self._by_id.get(reminder_id)

    def list(
        self, *, status: ReminderStatus | None = None
    ) -> list[Reminder]:
        items = list(self._by_id.values())
        if status is not None:
            items = [r for r in items if r.status is status]
        items.sort(key=lambda r: r.due_at_ms)
        return items

    def list_due(self, now_ms: int) -> list[Reminder]:
        """Pending reminders whose ``due_at_ms`` has arrived."""
        return [
            r
            for r in self.list(status=ReminderStatus.PENDING)
            if r.due_at_ms <= now_ms
        ]

    def next_due_at(self) -> int | None:
        """Earliest ``due_at_ms`` among PENDING reminders, or ``None``."""
        pending = [
            r.due_at_ms
            for r in self._by_id.values()
            if r.status is ReminderStatus.PENDING
        ]
        return min(pending) if pending else None

    def __len__(self) -> int:
        return len(self._by_id)

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, cb: Subscription) -> Unsubscribe:
        self._subs.append(cb)

        def unsubscribe() -> None:
            if cb in self._subs:
                self._subs.remove(cb)

        return unsubscribe

    def _emit(self, event: ReminderStoreEvent) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "reminders.subscriber_error",
                    reminder_id=event.reminder_id,
                    kind=event.kind,
                    error=str(exc),
                )


__all__ = [
    "ReminderStore",
    "ReminderStoreEvent",
    "ReminderStoreEventKind",
    "Subscription",
    "Unsubscribe",
]
