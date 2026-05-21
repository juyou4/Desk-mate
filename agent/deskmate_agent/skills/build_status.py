"""Build / test status island skill (V10 Phase 14-i).

External tools (Makefile, CI wrapper, editor build task, …) report
progress by dropping a JSON line into
``~/.deskmate/build-status.json``; the :class:`BuildStatusWatcher`
polls that path and drives the skill, which in turn emits
``PRESENT_ISLAND`` / ``UPDATE_ISLAND`` / ``DISMISS_ISLAND`` intents.

Wire contract (JSON schema, all strings unless noted):

- ``state``: ``"started"`` | ``"progress"`` | ``"done"`` | ``"failed"``
- ``task``: free-form human-friendly task name (becomes part of the
  island's activity id)
- ``progress`` (optional, float 0..1, only for ``progress``)
- ``message`` (optional, free-form tail)

Priority & lifecycle:

- ``started`` / ``progress`` present a ``live_activity`` at P1 so the
  coding-session pill (P2) steps aside for the duration of the build.
- ``done`` / ``failed`` flip the detail to a ✅ / ❌ summary, then
  auto-dismiss after a short TTL (5s default for success, 10s for
  failures — so a failing build lingers long enough to read).
- A second ``started`` while another build is already showing first
  dismisses the old one so the state machine morphs cleanly rather
  than stacking.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

from ..dispatcher import IntentSink
from ..logging_setup import get_logger
from ..protocol.intents import CompanionIntent, IntentKind

_LOGGER = get_logger(__name__)


def _activity_id_for(task: str) -> str:
    return f"build-{task}"


def _format_detail(
    *,
    emoji: str,
    task: str,
    branch: str | None,
    tail: str | None,
) -> str:
    """Compose the island detail string.

    Order: ``<emoji> <task>[ · <branch>][ · <tail>]``. The branch slot
    always comes right after the task so the user sees the
    "context" piece before the "progress" piece.
    """
    parts = [f"{emoji} {task}"]
    if branch:
        parts.append(branch)
    if tail:
        parts.append(tail)
    return " · ".join(parts)


@dataclass
class BuildStatusSkill:
    intent_sink: IntentSink
    success_ttl_ms: int = 5_000
    failure_ttl_ms: int = 10_000

    def __post_init__(self) -> None:
        self._current_activity_id: str | None = None
        self._pending_dismiss: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API — each call is idempotent relative to the current
    # activity_id, so replaying the same "started" twice is a clean
    # replace rather than a stack of PRESENTs.
    # ------------------------------------------------------------------

    async def on_build_start(
        self, task: str, *, branch: str | None = None
    ) -> None:
        await self._cancel_pending_dismiss()
        if self._current_activity_id is not None:
            # Replacing an in-flight build: dismiss the old first so
            # the state machine morphs cleanly.
            await self._dismiss(self._current_activity_id)
        activity_id = _activity_id_for(task)
        self._current_activity_id = activity_id
        detail = _format_detail(
            emoji="🔨", task=task, branch=branch, tail=None
        )
        await self.intent_sink(
            CompanionIntent(
                kind=IntentKind.PRESENT_ISLAND,
                payload={
                    "surface": "live_activity",
                    "activity_id": activity_id,
                    "priority": "p1",
                    "detail": detail,
                },
            )
        )

    async def on_build_progress(
        self,
        task: str,
        progress: float,
        message: str | None = None,
        *,
        branch: str | None = None,
    ) -> None:
        activity_id = _activity_id_for(task)
        if activity_id != self._current_activity_id:
            # Out of sync — usually means the user cleared / dismissed
            # the island or a different build overtook this one.
            # Dropping is safer than resurrecting stale activity ids.
            return
        pct = max(0, min(100, int(round(progress * 100))))
        tail_parts = [f"{pct}%"]
        if message:
            tail_parts.append(message)
        detail = _format_detail(
            emoji="🔨",
            task=task,
            branch=branch,
            tail=" · ".join(tail_parts),
        )
        await self.intent_sink(
            CompanionIntent(
                kind=IntentKind.UPDATE_ISLAND,
                payload={"activity_id": activity_id, "detail": detail},
            )
        )

    async def on_build_done(
        self,
        task: str,
        *,
        success: bool,
        message: str | None = None,
        branch: str | None = None,
    ) -> None:
        activity_id = _activity_id_for(task)
        if activity_id != self._current_activity_id:
            return
        emoji = "✅" if success else "❌"
        detail = _format_detail(
            emoji=emoji, task=task, branch=branch, tail=message
        )
        await self.intent_sink(
            CompanionIntent(
                kind=IntentKind.UPDATE_ISLAND,
                payload={"activity_id": activity_id, "detail": detail},
            )
        )
        ttl_ms = self.success_ttl_ms if success else self.failure_ttl_ms
        self._pending_dismiss = asyncio.create_task(
            self._delayed_dismiss(activity_id, ttl_ms)
        )

    async def on_external_dismiss(self) -> None:
        """Called by the watcher when the CLI writes an explicit
        ``"state": "dismiss"`` payload; kept separate from the TTL
        path so tests can exercise it deterministically."""
        await self._cancel_pending_dismiss()
        if self._current_activity_id is not None:
            await self._dismiss(self._current_activity_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _delayed_dismiss(self, activity_id: str, ttl_ms: int) -> None:
        try:
            await asyncio.sleep(ttl_ms / 1000)
        except asyncio.CancelledError:
            return
        # Only dismiss if we're still the front-of-queue activity;
        # another build may have kicked us off in the meantime.
        if self._current_activity_id == activity_id:
            await self._dismiss(activity_id)

    async def _dismiss(self, activity_id: str) -> None:
        self._current_activity_id = None
        await self.intent_sink(
            CompanionIntent(
                kind=IntentKind.DISMISS_ISLAND,
                payload={"id": activity_id},
            )
        )

    async def _cancel_pending_dismiss(self) -> None:
        task = self._pending_dismiss
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._pending_dismiss = None


__all__ = ["BuildStatusSkill"]
