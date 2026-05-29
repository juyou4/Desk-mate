"""Runtime :class:`SessionInfo` model (V10 L1-D / L2-#4).

Designed to be round-tripped across the Swift bridge via Pydantic JSON
(snake_case). The enum values match the strings produced by
``IslandSurfaceState`` / ``DomainState`` so Swift can use them as-is.

V10 L2-#4 ("actionable-first + subagent fold") adds three optional
fields:

- :attr:`phase` — orthogonal to :attr:`state` (which is ``active /
  paused / closed``). Phase is *what kind of attention* the session
  needs from the user right now: an approval, an answer, neither
  (just running), or done. The island session list ranks by phase
  first so the user always sees the actionable ones up top.
- :attr:`parent_session_id` — when a long-running session spawns
  a tool-call / subagent / worktree-bound sub-session, that child
  declares its parent here. The store's top-level listing folds
  subagents under the parent instead of cluttering the surface.
- :attr:`subagent_kind` — free-form short tag (``"tool_call"``,
  ``"worktree"``, …) shipped to the UI for the fold summary. ``None``
  on top-level sessions.

All three default such that existing payloads (Swift snapshots, on-disk
``SessionMemory`` rows from older agents) keep deserialising — the
session looks like a normal, non-subagent, ``running`` top-level
session, which matches V9 behaviour.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..protocol.state import Priority


class SessionState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CLOSED = "closed"


class SessionPhase(StrEnum):
    """Actionable-first ordering axis (V10 L2-#4).

    Ranks (lower = more actionable, sorts earlier):

    - :attr:`WAITING_FOR_APPROVAL` — agent paused on a permission
      / risky-action gate. P0 in plan terms.
    - :attr:`WAITING_FOR_ANSWER` — agent paused asking the user a
      clarifying question.
    - :attr:`RUNNING` — making progress, no input needed.
    - :attr:`COMPLETED` — done; sticks around for a moment so the
      user can read the result before it scrolls off.
    """

    WAITING_FOR_APPROVAL = "waiting_for_approval"
    WAITING_FOR_ANSWER = "waiting_for_answer"
    THINKING = "thinking"
    EDITING = "editing"
    RUNNING_TOOL = "running_tool"
    TESTING = "testing"
    RUNNING = "running"
    FAILED = "failed"
    COMPLETED = "completed"


class SessionInfo(BaseModel):
    """Single runtime session record.

    Unknown fields survive — an older agent shipping a newer Swift payload
    should never drop data.
    """

    model_config = ConfigDict(extra="allow")

    session_id: str
    title: str = ""
    summary: str = ""
    state: SessionState = SessionState.ACTIVE
    priority: Priority = Priority.P2

    created_at_ms: int = 0
    updated_at_ms: int = 0
    closed_at_ms: int | None = None

    # V10 L2-#4: actionable-first sort key. ``RUNNING`` is the safe
    # default — older payloads without ``phase`` end up at the same
    # rank, so their relative order falls back to the legacy
    # ``(updated_at_ms desc, priority asc)`` tiebreaker.
    phase: SessionPhase = SessionPhase.RUNNING

    # V10 L2-#4: subagent / worktree linkage. Top-level sessions
    # leave this ``None``; the store hides children from the default
    # listing and folds them under the parent.
    parent_session_id: str | None = None
    subagent_kind: str | None = None

    # Island-polish-enhancements: phase observability & question tracking.
    # - phase_source: how the most recent phase update was sourced
    #   ("unobserved" / "hooked" / "app_server"). None = not yet classified.
    # - question_seq: monotonically increasing counter for questions in
    #   this session, used to build surface_id = "question:<sid>:<seq>".
    # - last_question_surface_id: the surface_id of the most recent
    #   question notification_card, used by SessionInteractionRouter to
    #   emit the correct dismiss_island target.
    phase_source: str | None = None
    question_seq: int = 0
    last_question_surface_id: str | None = None

    # Agent/runtime discovery metadata. Hook events and passive process
    # scans both populate these so Swift can label session rows.
    source: str | None = None
    kind: str | None = None
    process_id: int | None = None
    cwd: str | None = None
    jump_url: str | None = None

    extras: dict[str, Any] = Field(default_factory=dict)


__all__ = ["SessionInfo", "SessionPhase", "SessionState"]
