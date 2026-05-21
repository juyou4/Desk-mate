"""Observable in-memory :class:`ApprovalStore` (V10 Phase 6 / L1-F).

Sole source of truth for "what the user currently owes an answer on".
The App reads ``list_pending`` each snapshot to populate
:class:`DomainState.pending_approvals`; the router reads ``get`` to
resolve, expire, and cancel.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from ..logging_setup import get_logger
from .model import Approval, ApprovalDecision, ApprovalStatus

_LOG = get_logger("deskmate_agent.approvals.store")


ApprovalStoreEventKind = Literal["add", "resolve", "expire", "cancel"]


@dataclass
class ApprovalStoreEvent:
    kind: ApprovalStoreEventKind
    approval_id: str
    approval: Approval | None
    ts_ms: int = 0


Subscription = Callable[[ApprovalStoreEvent], None]
Unsubscribe = Callable[[], None]


class ApprovalStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Approval] = {}
        self._subs: list[Subscription] = []

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add(self, approval: Approval) -> Approval:
        """Insert a new approval. Duplicate ids **replace** the previous
        record so skills can re-ask with updated prompt text."""
        self._by_id[approval.approval_id] = approval
        self._emit(ApprovalStoreEvent(
            kind="add",
            approval_id=approval.approval_id,
            approval=approval,
            ts_ms=approval.created_at_ms,
        ))
        return approval

    def resolve(
        self,
        approval_id: str,
        decision: ApprovalDecision,
        ts_ms: int,
    ) -> Approval | None:
        existing = self._by_id.get(approval_id)
        if existing is None or existing.status is not ApprovalStatus.PENDING:
            return None
        if decision is ApprovalDecision.NONE:
            return None  # must pick ALLOW or DENY
        updated = existing.model_copy(update={
            "status": ApprovalStatus.RESOLVED,
            "decision": decision,
            "resolved_at_ms": ts_ms,
        })
        self._by_id[approval_id] = updated
        self._emit(ApprovalStoreEvent(
            kind="resolve",
            approval_id=approval_id,
            approval=updated,
            ts_ms=ts_ms,
        ))
        return updated

    def expire(self, approval_id: str, ts_ms: int) -> Approval | None:
        existing = self._by_id.get(approval_id)
        if existing is None or existing.status is not ApprovalStatus.PENDING:
            return None
        updated = existing.model_copy(update={
            "status": ApprovalStatus.EXPIRED,
            "resolved_at_ms": ts_ms,
        })
        self._by_id[approval_id] = updated
        self._emit(ApprovalStoreEvent(
            kind="expire",
            approval_id=approval_id,
            approval=updated,
            ts_ms=ts_ms,
        ))
        return updated

    def cancel(self, approval_id: str, ts_ms: int) -> Approval | None:
        existing = self._by_id.get(approval_id)
        if existing is None or existing.status is not ApprovalStatus.PENDING:
            return None
        updated = existing.model_copy(update={
            "status": ApprovalStatus.CANCELLED,
            "resolved_at_ms": ts_ms,
        })
        self._by_id[approval_id] = updated
        self._emit(ApprovalStoreEvent(
            kind="cancel",
            approval_id=approval_id,
            approval=updated,
            ts_ms=ts_ms,
        ))
        return updated

    def clear(self) -> None:
        self._by_id.clear()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get(self, approval_id: str) -> Approval | None:
        return self._by_id.get(approval_id)

    def list(
        self, *, status: ApprovalStatus | None = None
    ) -> list[Approval]:
        items = list(self._by_id.values())
        if status is not None:
            items = [a for a in items if a.status is status]
        items.sort(key=lambda a: a.created_at_ms)
        return items

    def list_pending(self) -> list[Approval]:
        return self.list(status=ApprovalStatus.PENDING)

    def pending_ids(self) -> list[str]:
        return [a.approval_id for a in self.list_pending()]

    def expire_due(self, now_ms: int) -> list[Approval]:
        """Flip every PENDING approval whose TTL has elapsed. Returns the
        list of approvals that just transitioned to EXPIRED."""
        flipped: list[Approval] = []
        for approval in list(self._by_id.values()):
            if approval.status is not ApprovalStatus.PENDING:
                continue
            if approval.expires_at_ms is None:
                continue
            if approval.expires_at_ms > now_ms:
                continue
            updated = self.expire(approval.approval_id, now_ms)
            if updated is not None:
                flipped.append(updated)
        return flipped

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

    def _emit(self, event: ApprovalStoreEvent) -> None:
        for cb in list(self._subs):
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "approvals.subscriber_error",
                    approval_id=event.approval_id,
                    kind=event.kind,
                    error=str(exc),
                )


__all__ = [
    "ApprovalStore",
    "ApprovalStoreEvent",
    "ApprovalStoreEventKind",
    "Subscription",
    "Unsubscribe",
]
