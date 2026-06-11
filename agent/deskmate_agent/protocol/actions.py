"""Typed user interaction actions (V10 L1-F / I8).

Every UI-originated event MUST serialize as an ``InteractionAction``. Free-form
string actions (``user.island_action { action: "join" }``) are forbidden at the
protocol boundary; router code may translate them internally but the wire
format must be typed.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionSource(StrEnum):
    PET = "pet"
    ISLAND = "island"
    MENU_BAR = "menu_bar"


class ActionTarget(StrEnum):
    SESSION = "session"
    REMINDER = "reminder"
    SKILL = "skill"
    SYSTEM = "system"
    BUBBLE = "bubble"


class InteractionKind(StrEnum):
    """Typed action verbs. Extend additively only."""

    # Session / approval
    PERMISSION_RESOLVE = "permission.resolve"
    QUESTION_ANSWER = "question.answer"
    SESSION_JUMP = "session.jump"
    TASK_OPEN_DETAIL = "task.open_detail"
    TASK_START = "task.start"
    TASK_PAUSE = "task.pause"
    TASK_ADVANCE = "task.advance"
    TASK_COMPLETE = "task.complete"

    # Surface lifecycle
    SURFACE_DISMISS = "surface.dismiss"

    # Developer/demo controls
    DEMO_TRIGGER = "demo.trigger"

    # Pet-native
    PET_INTERACT = "pet.interact"
    PET_DRAG = "pet.drag"
    PET_NEST = "pet.nest"


class InteractionAction(BaseModel):
    """Single typed action produced by any UI surface.

    Payload is intentionally schemaless; consumers are expected to dispatch on
    ``kind`` and validate their own payload shape. Unknown fields survive.
    """

    model_config = ConfigDict(extra="allow")

    source: ActionSource
    target: ActionTarget
    kind: InteractionKind
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "ActionSource",
    "ActionTarget",
    "InteractionAction",
    "InteractionKind",
]
