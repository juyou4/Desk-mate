"""Domain vs Surface state shapes (V10 L1-A / L1-B, I1 / I5).

``DomainState`` is the single source of truth owned by the agent core.
Per-surface states (pet / island / menu bar) are *derived* views of the domain
state plus transient UI details. Pet and Island never own authoritative data.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SPEC_VERSION = 1


# ---------------------------------------------------------------------------
# Domain (priority, focus, mood)
# ---------------------------------------------------------------------------


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class UserFocus(StrEnum):
    FOCUSED = "focused"
    CASUAL = "casual"
    IDLE_BACK = "idle_back"


class AgentMood(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    THINKING = "thinking"
    HAPPY = "happy"
    ALERT = "alert"


class DomainState(BaseModel):
    """Authoritative non-UI state. Pet / Island / MenuBar only read from this."""

    model_config = ConfigDict(extra="allow")

    spec_version: int = SPEC_VERSION
    current_priority: Priority = Priority.P3
    user_focus: UserFocus = UserFocus.CASUAL
    agent_mood: AgentMood = AgentMood.IDLE
    pending_approvals: list[str] = Field(default_factory=list)
    active_session_id: str | None = None
    # V10 Phase 15-i: rolling sum of coding-session durations whose
    # end timestamp falls after local midnight. Populated by the
    # :class:`CodingSessionStore` rollup at startup + after every
    # session closes. 0 when unknown / disabled.
    coding_today_ms: int = 0
    # V10 Phase 15-i+: per-IDE breakdown of the same window, sorted
    # descending by duration (the dict preserves insertion order).
    # The menu bar uses this to render the "Today on <IDE>: Xm" rows
    # without needing to query SQLite from Swift.
    coding_today_by_ide: dict[str, int] = Field(default_factory=dict)
    # V10 Phase 9 · §4 degradation: monotonic integer 0..6 driving
    # the graduated performance degradation path (FPS, proactive
    # cooldown, perception widening, island orderOut, …). 0 = normal.
    # Swift mirrors this field to decide FPS tier / SneakPeek HUD /
    # orderOut behaviour.
    degradation_level: int = 0


# ---------------------------------------------------------------------------
# Pet surface state (I1)
# ---------------------------------------------------------------------------


class PetAnchorKind(StrEnum):
    DESKTOP = "desktop"
    NEST = "nest"
    TRANSITION = "transition"


class PetVelocity(BaseModel):
    model_config = ConfigDict(extra="allow")

    dx: float = 0.0
    dy: float = 0.0


class PetAnchor(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: PetAnchorKind = PetAnchorKind.DESKTOP
    target_nest: str | None = None


class NestBehaviorPolicy(BaseModel):
    model_config = ConfigDict(extra="allow")

    can_enter_nest: bool = True
    should_leave_nest: bool = False
    target_nest: str | None = None


class PetPresentationState(BaseModel):
    """Surface-level pet state — animation, emotion, anchor, avatar style."""

    model_config = ConfigDict(extra="allow")

    animation_state: str = "idle"
    emotion: str = "neutral"
    attention_level: float = 0.0  # 0..1, drives proactive behaviour scaling
    anchor_kind: PetAnchorKind = PetAnchorKind.DESKTOP
    velocity: PetVelocity = Field(default_factory=PetVelocity)
    is_interactive: bool = True
    bubble_id: str | None = None
    avatar_style: str = "pixel"


# ---------------------------------------------------------------------------
# Island surface state (L1-E / I5)
# ---------------------------------------------------------------------------


class IslandSurfaceKind(StrEnum):
    """Five-way enumeration. Any future surface must be added additively."""

    COMPACT = "compact"
    NOTIFICATION_CARD = "notification_card"
    SESSION_LIST = "session_list"
    LIVE_ACTIVITY = "live_activity"
    EMPTY = "empty"


class IslandSurfaceState(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: IslandSurfaceKind = IslandSurfaceKind.COMPACT
    session_id: str | None = None
    activity_id: str | None = None
    # V10 Phase 13-ii: free-form secondary label for the island pill.
    # Set by skills (e.g. CodingSessionTracker) to carry the current
    # window title / session duration / live progress text without
    # churning ``activity_id`` on every update. Swift's
    # :class:`IslandSurfaceState` mirrors this field verbatim.
    detail: str | None = None


# ---------------------------------------------------------------------------
# MenuBar surface state
# ---------------------------------------------------------------------------


class MenuBarState(BaseModel):
    model_config = ConfigDict(extra="allow")

    unread_count: int = 0
    summary_text: str | None = None


# ---------------------------------------------------------------------------
# BubbleSpec (I3) lives with state because it rides along PetPresentationState
# ---------------------------------------------------------------------------


class BubbleKind(StrEnum):
    CHAT = "chat"
    STATUS = "status"
    APPROVAL_HINT = "approval_hint"
    REMINDER = "reminder"
    RANDOM_REACTION = "random_reaction"
    SYSTEM = "system"


class BubbleAction(BaseModel):
    model_config = ConfigDict(extra="allow")

    label: str
    interaction_kind: str  # references InteractionKind value; decoupled to avoid circular import
    payload: dict[str, Any] = Field(default_factory=dict)


class BubbleSpec(BaseModel):
    """Declarative bubble description. UI is the renderer, this is the spec."""

    model_config = ConfigDict(extra="allow")

    id: str
    kind: BubbleKind = BubbleKind.CHAT
    icon: str | None = None
    text: str = ""
    markdown: str | None = None
    actions: list[BubbleAction] = Field(default_factory=list)
    start_audio: str | None = None
    end_audio: str | None = None
    ttl_ms: int | None = 8000
    priority: Priority = Priority.P2
    source_event_id: str | None = None


__all__ = [
    "SPEC_VERSION",
    "AgentMood",
    "BubbleAction",
    "BubbleKind",
    "BubbleSpec",
    "DomainState",
    "IslandSurfaceKind",
    "IslandSurfaceState",
    "MenuBarState",
    "NestBehaviorPolicy",
    "PetAnchor",
    "PetAnchorKind",
    "PetPresentationState",
    "PetVelocity",
    "Priority",
    "UserFocus",
]
