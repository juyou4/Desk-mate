"""Reminder scheduler (V10 L2-#4).

Turns :class:`ReminderStore` state into :class:`CompanionIntent` emissions
at the right wall-clock moment. The runtime async loop is thin — the
interesting logic lives in :meth:`process_due`, which is a pure async
function that unit tests call directly with a fixed ``now_ms``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

from ..logging_setup import get_logger
from ..protocol.intents import CompanionIntent, IntentKind
from ..protocol.state import BubbleAction, BubbleKind, BubbleSpec
from .model import Reminder
from .store import ReminderStore, ReminderStoreEvent

_LOG = get_logger("deskmate_agent.reminders.scheduler")


IntentSink = Callable[[CompanionIntent], Awaitable[None]]
Clock = Callable[[], int]
IdFactory = Callable[[], str]


def _default_clock() -> int:
    return int(time.time() * 1000)


def _default_id_factory() -> str:
    return "bubble-" + uuid.uuid4().hex[:12]


class ReminderScheduler:
    """Async runloop that fires reminders as they come due."""

    def __init__(
        self,
        store: ReminderStore,
        intent_sink: IntentSink,
        *,
        clock: Clock = _default_clock,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        self._store = store
        self._sink = intent_sink
        self._clock = clock
        self._id_factory = id_factory
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._unsubscribe = store.subscribe(self._on_store_event)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process_due(self, now_ms: int) -> int:
        """Fire every PENDING reminder whose due time has passed.

        Returns the count fired. Safe to call repeatedly; ``mark_fired``
        gates on status so a fired reminder is never fired twice.
        """
        fired = 0
        for reminder in self._store.list_due(now_ms):
            bubble_id = self._id_factory()
            updated = self._store.mark_fired(
                reminder.reminder_id, now_ms, bubble_id
            )
            if updated is None:
                continue
            try:
                await self._sink(self._build_intent(updated))
                fired += 1
            except Exception as exc:  # noqa: BLE001
                # Sink failures are logged but must not poison the
                # store — the reminder stays FIRED; UI loss is a
                # separate incident.
                _LOG.warning(
                    "reminders.sink_failed",
                    reminder_id=updated.reminder_id,
                    error=str(exc),
                )
        return fired

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="reminder-scheduler")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        task = self._task
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except (TimeoutError, asyncio.CancelledError):
                task.cancel()
        self._task = None
        self._unsubscribe()

    # ------------------------------------------------------------------
    # Runloop
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            now = self._clock()
            await self.process_due(now)
            if self._stopping:
                return

            next_at = self._store.next_due_at()
            if next_at is None:
                # Nothing to do — block until store wakes us.
                await self._wake.wait()
                self._wake.clear()
                continue

            delay = (next_at - self._clock()) / 1000
            if delay <= 0:
                continue  # already due, loop back immediately
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                self._wake.clear()
            except TimeoutError:
                pass  # planned fire time arrived

    def _on_store_event(self, _event: ReminderStoreEvent) -> None:
        # Any mutation invalidates our sleep schedule → kick the loop.
        self._wake.set()

    # ------------------------------------------------------------------
    # Intent building
    # ------------------------------------------------------------------

    @staticmethod
    def _build_intent(reminder: Reminder) -> CompanionIntent:
        assert reminder.bubble_id is not None, "fired reminder must have bubble_id"
        bubble = BubbleSpec(
            id=reminder.bubble_id,
            kind=BubbleKind.REMINDER,
            text=reminder.text,
            priority=reminder.priority,
            ttl_ms=None,  # reminders stay until the user dismisses them
            actions=[
                BubbleAction(
                    label="Done",
                    interaction_kind="surface.dismiss",
                    payload={
                        "reminder_id": reminder.reminder_id,
                        "bubble_id": reminder.bubble_id,
                    },
                ),
            ],
        )
        return CompanionIntent(
            kind=IntentKind.SHOW_PET_BUBBLE,
            payload={
                "bubble": bubble.model_dump(mode="json"),
                "reminder_id": reminder.reminder_id,
            },
        )


__all__ = ["IntentSink", "ReminderScheduler"]
