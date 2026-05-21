"""Reminder model (V10 L2-#4).

A :class:`Reminder` is addressable (``reminder_id``), has a firing time
(``due_at_ms``), and progresses through a strictly-forward status lattice:

    PENDING → FIRED → DISMISSED
            ↘ CANCELLED

Unknown fields on the wire survive — older agents + newer Swift clients
won't drop data they don't understand.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..protocol.state import Priority


class ReminderStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    DISMISSED = "dismissed"
    CANCELLED = "cancelled"


class Reminder(BaseModel):
    model_config = ConfigDict(extra="allow")

    reminder_id: str
    text: str
    due_at_ms: int
    created_at_ms: int

    status: ReminderStatus = ReminderStatus.PENDING
    priority: Priority = Priority.P1

    session_id: str | None = None
    bubble_id: str | None = None  # filled when fired; ties the bubble back

    fired_at_ms: int | None = None
    resolved_at_ms: int | None = None

    extras: dict[str, Any] = Field(default_factory=dict)


__all__ = ["Reminder", "ReminderStatus"]
