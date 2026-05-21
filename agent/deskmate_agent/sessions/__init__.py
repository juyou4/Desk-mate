"""Runtime session model (V10 L1-D).

Live sessions drive the Island ``session_list`` surface and the Pet
``active_session_id`` hint. Completed summaries are persisted separately
via :class:`deskmate_agent.memory.SessionMemory` — this package is the
*in-memory runtime* half of the pair.
"""

from __future__ import annotations

from .info import SessionInfo, SessionPhase, SessionState
from .router import RouterResult, SessionInteractionRouter
from .store import (
    SessionListItem,
    SessionStore,
    SessionStoreEvent,
)

__all__ = [
    "RouterResult",
    "SessionInfo",
    "SessionInteractionRouter",
    "SessionListItem",
    "SessionPhase",
    "SessionState",
    "SessionStore",
    "SessionStoreEvent",
]
