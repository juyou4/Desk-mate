"""In-memory runtime :class:`SessionStore` (V10 L1-D / L2-#4 / L2-#5).

Single source of truth for "what sessions are currently alive". The
orchestrator upserts as it receives skill results; the App snapshot
broadcasts them to Swift; the Island's ``session_list`` surface renders
them. On shutdown / commit, entries migrate to
:class:`deskmate_agent.memory.SessionMemory` (disk).

Thread model: all mutations happen on the asyncio event loop, so no
locking is required. Subscriber callbacks MUST be fast and non-async.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ..logging_setup import get_logger
from ..protocol.state import Priority
from .info import SessionInfo, SessionPhase, SessionState

_LOG = get_logger("deskmate_agent.sessions.store")


SessionStoreEventKind = Literal["upsert", "touch", "remove"]


@dataclass
class SessionStoreEvent:
    kind: SessionStoreEventKind
    session_id: str
    info: SessionInfo | None
    ts_ms: int = 0


Subscription = Callable[[SessionStoreEvent], None]
Unsubscribe = Callable[[], None]


_PRIORITY_RANK: dict[Priority, int] = {
    Priority.P0: 0,
    Priority.P1: 1,
    Priority.P2: 2,
    Priority.P3: 3,
}


# V10 L2-#4: actionable-first ordering. Lower rank → sorted earlier →
# rendered higher on the island's ``session_list``. Anything not in
# the table sorts last so an unknown phase doesn't crash the UI.
_PHASE_RANK: dict[SessionPhase, int] = {
    SessionPhase.WAITING_FOR_APPROVAL: 0,
    SessionPhase.WAITING_FOR_ANSWER: 1,
    SessionPhase.FAILED: 2,
    SessionPhase.RUNNING_TOOL: 3,
    SessionPhase.EDITING: 4,
    SessionPhase.TESTING: 5,
    SessionPhase.THINKING: 6,
    SessionPhase.RUNNING: 7,
    SessionPhase.COMPLETED: 8,
}


@dataclass(frozen=True)
class SessionListItem:
    """A top-level session paired with a fold of its subagents.

    Returned by :meth:`SessionStore.list_top_level_with_fold`. The
    fold is intentionally cheap — just a count + short summary list —
    so the island can decide how to render it (badge / disclosure
    arrow / inline tool icons) without re-querying the store.
    """

    info: SessionInfo
    subagent_count: int = 0
    subagent_summaries: tuple[str, ...] = field(default_factory=tuple)


class SessionStore:
    """Observable, ordered index of live sessions."""

    def __init__(self) -> None:
        self._by_id: dict[str, SessionInfo] = {}
        self._subs: list[Subscription] = []

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def upsert(self, info: SessionInfo) -> SessionInfo:
        """Insert or update a session. If ``created_at_ms`` is 0 and an
        existing record exists, the previous ``created_at_ms`` is kept so
        callers can ``upsert`` partial diffs without losing history."""
        existing = self._by_id.get(info.session_id)
        if existing is not None and info.created_at_ms == 0:
            info = info.model_copy(
                update={"created_at_ms": existing.created_at_ms}
            )
        self._by_id[info.session_id] = info
        self._emit(SessionStoreEvent(
            kind="upsert",
            session_id=info.session_id,
            info=info,
            ts_ms=info.updated_at_ms,
        ))
        return info

    def remove(self, sid: str) -> SessionInfo | None:
        info = self._by_id.pop(sid, None)
        if info is not None:
            self._emit(SessionStoreEvent(
                kind="remove", session_id=sid, info=info, ts_ms=info.updated_at_ms
            ))
        return info

    def touch(
        self,
        sid: str,
        ts_ms: int,
        *,
        new_state: SessionState | None = None,
    ) -> SessionInfo | None:
        """Update ``updated_at_ms`` (and optionally ``state``). Returns the
        refreshed :class:`SessionInfo` or ``None`` if ``sid`` is unknown."""
        existing = self._by_id.get(sid)
        if existing is None:
            return None
        updates: dict[str, object] = {"updated_at_ms": ts_ms}
        if new_state is not None:
            updates["state"] = new_state
            if new_state is SessionState.CLOSED and existing.closed_at_ms is None:
                updates["closed_at_ms"] = ts_ms
        updated = existing.model_copy(update=updates)
        self._by_id[sid] = updated
        self._emit(SessionStoreEvent(
            kind="touch", session_id=sid, info=updated, ts_ms=ts_ms
        ))
        return updated

    def clear(self) -> None:
        self._by_id.clear()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, sid: str) -> SessionInfo | None:
        return self._by_id.get(sid)

    def list(
        self,
        *,
        state: SessionState | None = None,
        limit: int | None = None,
        include_subagents: bool = False,
    ) -> list[SessionInfo]:
        """Return sessions sorted actionable-first.

        Sort key (lowest sorts first):

        1. Phase rank — :attr:`SessionPhase.WAITING_FOR_APPROVAL`
           sits above ``WAITING_FOR_ANSWER`` above ``RUNNING`` above
           ``COMPLETED`` (V10 L2-#4).
        2. ``-updated_at_ms`` — most recently touched first.
        3. Priority rank — P0 ahead of P3.

        ``state`` filters by :class:`SessionState`; ``limit`` caps
        the result length; ``include_subagents`` defaults to ``False``
        so callers that just want top-level sessions don't have to
        re-filter (the island session list is the canonical example).
        """
        items = list(self._by_id.values())
        if state is not None:
            items = [i for i in items if i.state is state]
        if not include_subagents:
            items = [i for i in items if i.parent_session_id is None]
        items.sort(
            key=lambda i: (
                _PHASE_RANK.get(i.phase, len(_PHASE_RANK)),
                -i.updated_at_ms,
                _PRIORITY_RANK.get(i.priority, 3),
            )
        )
        if limit is not None:
            items = items[:limit]
        return items

    def list_active(
        self, *, limit: int | None = None, include_subagents: bool = False
    ) -> list[SessionInfo]:
        return self.list(
            state=SessionState.ACTIVE,
            limit=limit,
            include_subagents=include_subagents,
        )

    def list_subagents_of(self, parent_id: str) -> list[SessionInfo]:
        """Return every subagent (any state) whose ``parent_session_id``
        equals ``parent_id``, sorted actionable-first.

        Empty list when nothing is folded under the parent — the
        caller can use this to skip rendering the fold affordance.
        """
        children = [
            i
            for i in self._by_id.values()
            if i.parent_session_id == parent_id
        ]
        children.sort(
            key=lambda i: (
                _PHASE_RANK.get(i.phase, len(_PHASE_RANK)),
                -i.updated_at_ms,
                _PRIORITY_RANK.get(i.priority, 3),
            )
        )
        return children

    def list_top_level_with_fold(
        self,
        *,
        state: SessionState | None = None,
        limit: int | None = None,
        max_summaries_per_parent: int = 3,
    ) -> list[SessionListItem]:
        """Top-level listing with a per-parent subagent fold.

        Each top-level session is paired with:

        - ``subagent_count`` — total live subagents under that parent,
          regardless of state. The badge in the island's session list
          shows this number.
        - ``subagent_summaries`` — at most ``max_summaries_per_parent``
          short labels (subagent ``title`` if non-empty, else
          ``subagent_kind``, else ``session_id``). These render as a
          tooltip / disclosure preview when the user hovers / taps.
        """
        tops = self.list(state=state, limit=limit, include_subagents=False)
        out: list[SessionListItem] = []
        for top in tops:
            subs = self.list_subagents_of(top.session_id)
            summaries: list[str] = []
            for sub in subs[: max(0, max_summaries_per_parent)]:
                label = (
                    sub.title.strip()
                    or sub.subagent_kind
                    or sub.session_id
                )
                summaries.append(label)
            out.append(
                SessionListItem(
                    info=top,
                    subagent_count=len(subs),
                    subagent_summaries=tuple(summaries),
                )
            )
        return out

    def __contains__(self, sid: object) -> bool:
        return isinstance(sid, str) and sid in self._by_id

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

    def _emit(self, event: SessionStoreEvent) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                # Subscriber errors must never break the store — we log and
                # keep delivering to remaining subscribers.
                _LOG.warning(
                    "sessions.subscriber_error",
                    session_id=event.session_id,
                    kind=event.kind,
                    error=str(exc),
                )


__all__ = [
    "SessionListItem",
    "SessionStore",
    "SessionStoreEvent",
    "SessionStoreEventKind",
    "Subscription",
    "Unsubscribe",
]
