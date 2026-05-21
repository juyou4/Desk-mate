"""ApprovalSurfacePublisher (V10 Phase 7 / L1-F + I3).

The Pet bubble is the default UI surface for approvals: the pet speaks a
prompt with Allow / Deny actions. This module keeps that coupling out of
:class:`ApprovalStore` itself so the store stays a pure runtime index;
the publisher reads it and turns :class:`ApprovalStoreEvent` values into
typed :class:`CompanionIntent` emissions.

Design:

* **Deterministic bubble ids.** ``bubble_id = f"approval-{approval_id}"``
  so the same approval surfacing and dismissing round-trip cleanly even
  across reconnects — Swift can correlate without sidecar state.
* **Show on add.** Every ``add`` event emits a ``SHOW_PET_BUBBLE``
  containing a :class:`BubbleSpec` of kind ``APPROVAL_HINT`` with two
  :class:`BubbleAction` s mapping to :attr:`InteractionKind.PERMISSION_RESOLVE`.
* **Dismiss on terminal.** Any ``resolve / expire / cancel`` event emits
  a ``DISMISS_PET_BUBBLE`` so the Swift bubble queue removes the hint.
* **Coalesced async emission.** Like :class:`DomainStateProjector`, the
  publisher dispatches each emission as an asyncio task so the sync
  subscriber callback returns immediately and never blocks the store.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ..logging_setup import get_logger
from ..protocol.actions import InteractionKind
from ..protocol.intents import CompanionIntent, IntentKind
from ..protocol.state import BubbleAction, BubbleKind, BubbleSpec
from .model import Approval
from .store import ApprovalStore, ApprovalStoreEvent

_LOG = get_logger("deskmate_agent.approvals.surface")


IntentSink = Callable[[CompanionIntent], Awaitable[None]]


class ApprovalSurfacePublisher:
    """Surface :class:`Approval` lifecycle as pet bubble intents."""

    def __init__(
        self,
        store: ApprovalStore,
        intent_sink: IntentSink,
    ) -> None:
        self._store = store
        self._sink = intent_sink
        self._pending: set[asyncio.Task[None]] = set()
        self._started = False
        self._unsubscribe: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._unsubscribe = self._store.subscribe(self._on_event)

    async def stop(self) -> None:
        self._started = False
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("surface.unsubscribe_failed", error=str(exc))
            self._unsubscribe = None
        await self.flush()

    async def flush(self) -> None:
        """Wait for every queued emission to complete. Tests use this
        to observe intents deterministically."""
        while self._pending:
            task = next(iter(self._pending))
            try:
                await task
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("surface.task_failed", error=str(exc))
            self._pending.discard(task)

    # ------------------------------------------------------------------
    # Naming — deterministic so Swift can correlate without extra state
    # ------------------------------------------------------------------

    @staticmethod
    def bubble_id_for(approval_id: str) -> str:
        return f"approval-{approval_id}"

    # ------------------------------------------------------------------
    # Event routing
    # ------------------------------------------------------------------

    def _on_event(self, event: ApprovalStoreEvent) -> None:
        approval = event.approval
        if approval is None:
            return
        intent: CompanionIntent | None
        if event.kind == "add":
            intent = self._build_show_intent(approval)
        elif event.kind in ("resolve", "expire", "cancel"):
            intent = self._build_dismiss_intent(approval)
        else:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop → tests that exercise the store synchronously
            # (e.g. store-only tests) won't get surface side effects.
            return
        task = loop.create_task(self._emit(intent), name="approval-surface-emit")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _emit(self, intent: CompanionIntent) -> None:
        try:
            await self._sink(intent)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "surface.sink_failed",
                error=str(exc),
                kind=intent.kind.value,
            )

    # ------------------------------------------------------------------
    # Intent builders
    # ------------------------------------------------------------------

    def _build_show_intent(self, approval: Approval) -> CompanionIntent:
        bubble = BubbleSpec(
            id=self.bubble_id_for(approval.approval_id),
            kind=BubbleKind.APPROVAL_HINT,
            text=approval.prompt,
            priority=approval.priority,
            ttl_ms=None,  # approvals require a user answer; no auto-hide
            actions=[
                BubbleAction(
                    label="Allow",
                    interaction_kind=InteractionKind.PERMISSION_RESOLVE.value,
                    payload={
                        "approval_id": approval.approval_id,
                        "allow": True,
                    },
                ),
                BubbleAction(
                    label="Deny",
                    interaction_kind=InteractionKind.PERMISSION_RESOLVE.value,
                    payload={
                        "approval_id": approval.approval_id,
                        "allow": False,
                    },
                ),
            ],
        )
        return CompanionIntent(
            kind=IntentKind.SHOW_PET_BUBBLE,
            payload={
                "bubble": bubble.model_dump(mode="json"),
                "approval_id": approval.approval_id,
            },
        )

    def _build_dismiss_intent(self, approval: Approval) -> CompanionIntent:
        return CompanionIntent(
            kind=IntentKind.DISMISS_PET_BUBBLE,
            payload={
                "bubble_id": self.bubble_id_for(approval.approval_id),
                "approval_id": approval.approval_id,
            },
        )


__all__ = ["ApprovalSurfacePublisher", "IntentSink"]
