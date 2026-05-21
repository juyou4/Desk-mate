"""Approval flow (V10 Phase 6 / L1-F).

Approvals are questions the agent needs the user to answer before it
proceeds (e.g. "Allow clipboard read?"). While at least one ``PENDING``
approval exists, :class:`DomainState.pending_approvals` carries its id
and the Pet state machine shifts to :attr:`AgentMood.ALERT`
(V10 L1-B + Phase 2a ``PetStateMachine``).

Resolution travels back through a typed :class:`InteractionAction` of
kind ``PERMISSION_RESOLVE`` routed into :class:`ApprovalRouter`.
"""

from __future__ import annotations

from .model import Approval, ApprovalDecision, ApprovalStatus
from .router import ApprovalResolveResult, ApprovalRouter
from .store import ApprovalStore, ApprovalStoreEvent
from .surface import ApprovalSurfacePublisher

__all__ = [
    "Approval",
    "ApprovalDecision",
    "ApprovalResolveResult",
    "ApprovalRouter",
    "ApprovalStatus",
    "ApprovalStore",
    "ApprovalStoreEvent",
    "ApprovalSurfacePublisher",
]
