"""Low-noise nudges for stale persistent Deskmate tasks."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable

from .island_notifications import IslandNotificationPublisher
from .logging_setup import get_logger
from .memory import DeskmateTaskRecord, DeskmateTaskStep, DeskmateTaskStore
from .protocol.intents import CompanionIntent, IntentKind
from .protocol.state import BubbleKind, BubbleSpec, Priority

_LOG = get_logger("deskmate_agent.task_nudges")

IntentSink = Callable[[CompanionIntent], Awaitable[None]]
Clock = Callable[[], int]
IdFactory = Callable[[], str]


def _default_clock() -> int:
    return int(time.time() * 1000)


def _default_id_factory() -> str:
    return "task-nudge-" + uuid.uuid4().hex[:12]


class TaskNudgeWatcher:
    """Periodically nudge about active tasks that have gone stale.

    This is deliberately separate from the perception-based proactive chain:
    task freshness is a durable-store concern, not a frontmost-app signal. The
    watcher emits at most one task per scan and keeps an in-memory cooldown per
    task id so a long-open task does not spam the user.
    """

    def __init__(
        self,
        task_store: DeskmateTaskStore,
        intent_sink: IntentSink,
        *,
        island_notifications: IslandNotificationPublisher | None = None,
        conversation_id: str = "default",
        stale_after_ms: int = 4 * 60 * 60 * 1000,
        cooldown_ms: int = 6 * 60 * 60 * 1000,
        poll_s: float = 5 * 60,
        clock: Clock = _default_clock,
        id_factory: IdFactory = _default_id_factory,
    ) -> None:
        self._task_store = task_store
        self._sink = intent_sink
        self._island_notifications = island_notifications
        self._conversation_id = conversation_id or "default"
        self._stale_after_ms = max(1, int(stale_after_ms))
        self._cooldown_ms = max(1, int(cooldown_ms))
        self._poll_s = max(0.1, float(poll_s))
        self._clock = clock
        self._id_factory = id_factory
        self._last_nudged_at: dict[str, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def process_once(self, now_ms: int | None = None) -> DeskmateTaskRecord | None:
        """Emit one stale task nudge if due, returning the nudged task."""
        now = int(now_ms if now_ms is not None else self._clock())
        task = await self._pick_stale_task(now)
        if task is None:
            return None
        await self._emit_task_nudge(task)
        self._last_nudged_at[task.task_id] = now
        return task

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="task-nudge-watcher")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.process_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "task_nudges.process_failed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
            await asyncio.sleep(self._poll_s)

    async def _pick_stale_task(self, now_ms: int) -> DeskmateTaskRecord | None:
        cutoff_ms = now_ms - self._stale_after_ms
        active = await self._task_store.list(
            self._conversation_id,
            status="active",
            limit=50,
        )
        stale = [
            task
            for task in active
            if task.updated_at_ms <= cutoff_ms and self._cooldown_elapsed(task, now_ms)
        ]
        if not stale:
            return None
        stale.sort(
            key=lambda task: (
                0 if task.status == "in_progress" else 1,
                task.updated_at_ms,
            )
        )
        return stale[0]

    def _cooldown_elapsed(self, task: DeskmateTaskRecord, now_ms: int) -> bool:
        last = self._last_nudged_at.get(task.task_id)
        if last is None:
            return True
        if task.updated_at_ms > last:
            return True
        return (now_ms - last) >= self._cooldown_ms

    async def _emit_task_nudge(self, task: DeskmateTaskRecord) -> None:
        steps = await self._task_steps(task)
        text = _task_nudge_text(task, steps=steps)
        bubble = BubbleSpec(
            id=self._id_factory(),
            kind=BubbleKind.STATUS,
            text=text,
            ttl_ms=10_000,
            priority=Priority.P2,
            source_event_id=f"task:{task.task_id}",
        )
        await self._sink(
            CompanionIntent(
                kind=IntentKind.SHOW_PET_BUBBLE,
                payload={
                    "bubble": bubble.model_dump(mode="json"),
                    "task_id": task.task_id,
                },
            )
        )
        if self._island_notifications is not None:
            await self._island_notifications.show_notification(
                activity_id=f"task-nudge-{task.task_id}",
                priority=Priority.P2,
                detail=text,
            )

    async def _task_steps(self, task: DeskmateTaskRecord) -> list[DeskmateTaskStep]:
        try:
            return await self._task_store.list_steps(
                task.task_id,
                conversation_id=self._conversation_id,
            )
        except Exception as exc:  # noqa: BLE001 — stale nudges are best-effort
            _LOG.warning(
                "task_nudges.steps_read_failed",
                task_id=task.task_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return []


def _task_nudge_text(
    task: DeskmateTaskRecord,
    *,
    steps: list[DeskmateTaskStep] | None = None,
) -> str:
    title = task.title.strip()
    if len(title) > 96:
        title = title[:93].rstrip() + "..."
    step = _task_step_hint(steps or [])
    suffix = f" - {step}" if step else ""
    if task.status == "in_progress":
        return f"Still working on: {title}{suffix}"
    return f"Still on your list: {title}{suffix}"


def _task_step_hint(steps: list[DeskmateTaskStep]) -> str:
    if not steps:
        return ""
    candidate = next((step for step in steps if step.status == "in_progress"), None)
    if candidate is None:
        candidate = next((step for step in steps if step.status == "pending"), None)
    if candidate is None:
        return ""
    text = candidate.active_form if candidate.status == "in_progress" and candidate.active_form else candidate.content
    text = " ".join(text.split())
    if not text:
        return ""
    if len(text) > 72:
        text = text[:69].rstrip() + "..."
    return f"step: {text}"


__all__ = ["TaskNudgeWatcher"]
