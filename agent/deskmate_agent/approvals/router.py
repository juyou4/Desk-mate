"""Route :class:`InteractionAction` values of kind
``PERMISSION_RESOLVE`` into :class:`ApprovalStore` mutations
(V10 Phase 6 / L1-F).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..logging_setup import get_logger
from ..protocol.actions import InteractionAction, InteractionKind
from .model import ApprovalDecision
from .store import ApprovalStore

_LOG = get_logger("deskmate_agent.approvals.router")


def _default_clock() -> int:
    return int(time.time() * 1000)


@dataclass
class ApprovalResolveResult:
    handled: bool
    effect: str = ""
    approval_id: str | None = None
    decision: ApprovalDecision | None = None


class ApprovalRouter:
    """Dispatch a ``PERMISSION_RESOLVE`` into the approval store."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        self._store = store
        self._clock = clock

    def handle(self, action: InteractionAction) -> ApprovalResolveResult:
        if action.kind is not InteractionKind.PERMISSION_RESOLVE:
            return ApprovalResolveResult(handled=False, effect="unknown_kind")

        approval_id = action.payload.get("approval_id")
        if not isinstance(approval_id, str) or not approval_id:
            return ApprovalResolveResult(
                handled=False, effect="missing_approval_id"
            )

        decision = self._decision_from_payload(action.payload)
        if decision is None:
            return ApprovalResolveResult(
                handled=False,
                effect="missing_decision",
                approval_id=approval_id,
            )

        updated = self._store.resolve(approval_id, decision, self._clock())
        if updated is None:
            # Either unknown id or already terminal (double-click / stale).
            effect = (
                "approval.resolve.unknown_id"
                if self._store.get(approval_id) is None
                else "approval.resolve.already_terminal"
            )
            result = ApprovalResolveResult(
                handled=True,
                effect=effect,
                approval_id=approval_id,
                decision=decision,
            )
        else:
            result = ApprovalResolveResult(
                handled=True,
                effect="approval.resolve.accepted",
                approval_id=approval_id,
                decision=decision,
            )
        _LOG.info(
            "approvals.router_handled",
            kind=action.kind.value,
            approval_id=approval_id,
            decision=decision.value,
            effect=result.effect,
        )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _decision_from_payload(
        payload: dict[str, object],
    ) -> ApprovalDecision | None:
        """Accept either ``{"allow": true}`` **or**
        ``{"decision": "allow"|"deny"}``; the former matches V10
        L1-F's example, the latter is more explicit for ternary cases."""
        if "decision" in payload:
            raw = payload.get("decision")
            if isinstance(raw, str):
                try:
                    return ApprovalDecision(raw)
                except ValueError:
                    return None
            return None
        if "allow" in payload:
            allow = payload.get("allow")
            if isinstance(allow, bool):
                return ApprovalDecision.ALLOW if allow else ApprovalDecision.DENY
        return None


__all__ = ["ApprovalResolveResult", "ApprovalRouter"]
