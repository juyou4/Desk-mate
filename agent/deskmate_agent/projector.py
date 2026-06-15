"""DomainState projector (V10 Phase 7 / L1-B).

Subscribes to the live in-memory stores (approval, session) and emits
:class:`CompanionIntent` values of kind
:attr:`IntentKind.UPDATE_DOMAIN_STATE` whenever the projected
:class:`DomainState` changes. The Pet state machine + Island modules on
the Swift side re-derive their presentation from that delta instead of
waiting for a full snapshot round-trip.

Design notes:

* **Single projector, many stores.** Today it watches approvals +
  sessions; future stores (focus, idle tracker) hook in the same way.
* **Coalescing "drain" pattern.** Multiple mutations within the same
  sync burst collapse into at most one intent reflecting the *final*
  state, so a flurry of events (e.g. store.cancel following store.add)
  never turns into two spurious updates.
* **Dedupe against last emission.** If the projection is identical to
  what we last sent, nothing goes out. Swift never sees redundant
  deltas.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from .agent_phase import presentation_for_phase
from .approvals import ApprovalStore, ApprovalStoreEvent
from .logging_setup import get_logger
from .protocol.intents import CompanionIntent, IntentKind
from .protocol.state import AgentMood, DomainState, Priority
from .sessions import SessionStore, SessionStoreEvent

_LOG = get_logger("deskmate_agent.projector")


IntentSink = Callable[[CompanionIntent], Awaitable[None]]


class DomainStateProjector:
    """Fan state-store mutations into typed ``UPDATE_DOMAIN_STATE`` intents."""

    def __init__(
        self,
        *,
        approval_store: ApprovalStore,
        session_store: SessionStore,
        intent_sink: IntentSink,
    ) -> None:
        self._approval_store = approval_store
        self._session_store = session_store
        self._sink = intent_sink

        self._unsubscribe_callbacks: list[Callable[[], None]] = []
        self._started = False
        self._dirty = False
        self._drain_task: asyncio.Task[None] | None = None
        self._last_emitted: DomainState | None = None
        # Phase 15-i: latest "today's coding time" rollup. Owned by
        # the app (async store lookup happens out-of-band), fed here
        # via :meth:`set_coding_today`.
        self._coding_today_ms: int = 0
        self._coding_today_by_ide: dict[str, int] = {}
        # Phase 9 · §4: degradation level, injected by whichever
        # component (battery watchdog, CPU heuristic, manual toggle)
        # owns the authoritative ``DegradationController``.
        self._degradation_level: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Subscribe to every upstream store. Idempotent."""
        if self._started:
            return
        self._started = True
        self._unsubscribe_callbacks.append(
            self._approval_store.subscribe(self._on_approval_event)
        )
        self._unsubscribe_callbacks.append(
            self._session_store.subscribe(self._on_session_event)
        )

    async def stop(self) -> None:
        """Unsubscribe and wait for any in-flight drain to finish."""
        self._started = False
        for unsub in self._unsubscribe_callbacks:
            try:
                unsub()
            except Exception as exc:  # noqa: BLE001
                _LOG.debug("projector.unsubscribe_failed", error=str(exc))
        self._unsubscribe_callbacks.clear()
        await self.flush()

    async def flush(self) -> None:
        """Await the current drain, if any. Deterministic for tests."""
        task = self._drain_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("projector.drain_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Event handlers — sync, cheap, schedule a drain task
    # ------------------------------------------------------------------

    def _on_approval_event(self, _event: ApprovalStoreEvent) -> None:
        self._kick()

    def _on_session_event(self, _event: SessionStoreEvent) -> None:
        self._kick()

    def _kick(self) -> None:
        self._dirty = True
        if self._drain_task is not None and not self._drain_task.done():
            return  # existing drain will pick up the latest state
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop → silently drop (e.g. test teardown)
        self._drain_task = loop.create_task(
            self._drain(), name="projector-drain"
        )

    # ------------------------------------------------------------------
    # Projection + emission
    # ------------------------------------------------------------------

    async def _drain(self) -> None:
        """Loop emitting the latest projection until the dirty flag
        stays clear across an iteration. This absorbs mutations that
        happen mid-emit without dropping the newest state."""
        while self._dirty:
            self._dirty = False
            current = self._project()
            if current == self._last_emitted:
                continue
            self._last_emitted = current
            try:
                await self._sink(self._build_intent(current))
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "projector.sink_failed",
                    error=str(exc),
                    pending_approvals=current.pending_approvals,
                )

    def _project(self) -> DomainState:
        active_sessions = self._session_store.list_active(limit=1)
        active_session_id = (
            active_sessions[0].session_id if active_sessions else None
        )
        pending_approvals = list(self._approval_store.pending_ids())
        current_priority, agent_mood = self._project_attention(
            pending_approvals=pending_approvals,
        )
        return DomainState(
            current_priority=current_priority,
            agent_mood=agent_mood,
            pending_approvals=pending_approvals,
            active_session_id=active_session_id,
            coding_today_ms=self._coding_today_ms,
            coding_today_by_ide=dict(self._coding_today_by_ide),
            degradation_level=self._degradation_level,
        )

    def _project_attention(
        self,
        *,
        pending_approvals: list[str],
    ) -> tuple[Priority, AgentMood]:
        """Derive the global attention level from live approvals/sessions.

        The bridge carries one compact ``DomainState`` to every Swift
        surface. Pending approvals win outright; otherwise the most
        actionable active session determines the priority and pet mood.
        """
        if pending_approvals:
            return Priority.P0, AgentMood.ALERT

        sessions = self._session_store.list_active(include_subagents=True)
        if not sessions:
            return Priority.P3, AgentMood.IDLE

        best_priority = Priority.P3
        best_mood = AgentMood.IDLE
        best_rank = _priority_rank(best_priority)
        for session in sessions:
            phase_ui = presentation_for_phase(
                session.phase,
                source=session.source or session.kind or "agent",
                summary=session.summary,
                title=session.title,
            )
            effective_priority = _more_urgent(
                session.priority,
                phase_ui.priority,
            )
            rank = _priority_rank(effective_priority)
            if rank < best_rank:
                best_priority = effective_priority
                best_mood = _mood_for_pet_state(phase_ui.pet_state)
                best_rank = rank

        if best_mood is AgentMood.IDLE and sessions:
            top = sessions[0]
            phase_ui = presentation_for_phase(
                top.phase,
                source=top.source or top.kind or "agent",
                summary=top.summary,
                title=top.title,
            )
            best_mood = _mood_for_pet_state(phase_ui.pet_state)
        return best_priority, best_mood

    def set_coding_today_ms(self, value: int) -> None:
        """Phase 15-i: update the cached daily coding total.

        Kicks the drain so the new value ships in the next intent
        (deduped against the previous emission).
        """
        clamped = max(0, int(value))
        if clamped == self._coding_today_ms:
            return
        self._coding_today_ms = clamped
        self._kick()

    def current_state(self) -> DomainState:
        """Phase 16-ii: a snapshot of the latest projected state so
        other chain participants (dispatcher, nudge selector) can
        consult it without subscribing directly.

        Always returns a fresh projection rather than the last
        emitted intent. Snapshot builders and nudge selectors call
        this on demand, so they must see in-memory mutations even if
        the coalescing drain has not emitted yet.
        """
        return self._project()

    def set_degradation_level(self, level: int) -> None:
        """Phase 9 · §4: accept the latest level from the controller.

        Clamps to ``0..6`` (the plan's graduated steps) and only kicks
        the drain on change.
        """
        clamped = max(0, min(6, int(level)))
        if clamped == self._degradation_level:
            return
        self._degradation_level = clamped
        self._kick()

    def set_coding_today_by_ide(self, breakdown: dict[str, int]) -> None:
        """Phase 15-i+: update the per-IDE breakdown.

        Values less than zero clamp to zero and zero-valued entries
        are dropped. Dedups against the previous map so the projector
        stays quiet when nothing changed.
        """
        cleaned: dict[str, int] = {}
        for ide, ms in breakdown.items():
            clamped = max(0, int(ms))
            if clamped > 0:
                cleaned[str(ide)] = clamped
        if cleaned == self._coding_today_by_ide:
            return
        self._coding_today_by_ide = cleaned
        self._kick()

    @staticmethod
    def _build_intent(state: DomainState) -> CompanionIntent:
        return CompanionIntent(
            kind=IntentKind.UPDATE_DOMAIN_STATE,
            payload={"domain_state": state.model_dump(mode="json")},
        )


def _priority_rank(priority: Priority) -> int:
    return {
        Priority.P0: 0,
        Priority.P1: 1,
        Priority.P2: 2,
        Priority.P3: 3,
    }[priority]


def _more_urgent(left: Priority, right: Priority) -> Priority:
    return left if _priority_rank(left) <= _priority_rank(right) else right


def _mood_for_pet_state(pet_state: str) -> AgentMood:
    if pet_state == "alert":
        return AgentMood.ALERT
    if pet_state == "thinking":
        return AgentMood.THINKING
    if pet_state == "working":
        return AgentMood.WORKING
    if pet_state == "happy":
        return AgentMood.HAPPY
    return AgentMood.IDLE


__all__ = ["DomainStateProjector", "IntentSink"]
