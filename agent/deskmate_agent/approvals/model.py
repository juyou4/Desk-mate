"""Approval request model (V10 Phase 6 / L1-F).

Status machine::

    PENDING ── user answers ──► RESOLVED (decision=ALLOW/DENY)
            ── ttl elapses ──► EXPIRED
            ── skill revokes─► CANCELLED

RESOLVED / EXPIRED / CANCELLED are terminal. Once terminal, callers must
create a new approval with a fresh ``approval_id``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..protocol.state import Priority


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ApprovalDecision(StrEnum):
    """User's answer. ``NONE`` until ``status == RESOLVED``."""

    NONE = "none"
    ALLOW = "allow"
    DENY = "deny"


class Approval(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_id: str
    prompt: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision = ApprovalDecision.NONE
    priority: Priority = Priority.P1

    session_id: str | None = None
    bubble_id: str | None = None

    created_at_ms: int
    expires_at_ms: int | None = None  # absolute ms; None disables the TTL
    resolved_at_ms: int | None = None

    extras: dict[str, Any] = Field(default_factory=dict)

    def is_terminal(self) -> bool:
        return self.status is not ApprovalStatus.PENDING


__all__ = ["Approval", "ApprovalDecision", "ApprovalStatus"]
