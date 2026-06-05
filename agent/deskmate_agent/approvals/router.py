"""Route :class:`InteractionAction` values of kind
``PERMISSION_RESOLVE`` into :class:`ApprovalStore` mutations
(V10 Phase 6 / L1-F).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..logging_setup import get_logger
from ..protocol.actions import InteractionAction, InteractionKind
from ..protocol.intents import CompanionIntent, IntentKind
from ..sessions import SessionPhase, SessionStore
from .model import ApprovalDecision
from .store import ApprovalStore

if TYPE_CHECKING:  # pragma: no cover — break import cycle (projector → approvals)
    from ..projector import DomainStateProjector

_LOG = get_logger("deskmate_agent.approvals.router")


def _default_clock() -> int:
    return int(time.time() * 1000)


#: Same shape as :data:`deskmate_agent.dispatcher.IntentSink` — an
#: async callable that ferries a single :class:`CompanionIntent` to
#: the bridge. Re-declared here so this module does not pull in the
#: dispatcher just for the alias.
IntentSink = Callable[[CompanionIntent], Awaitable[None]]


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
        intent_sink: IntentSink,
        session_store: SessionStore,
        domain_projector: DomainStateProjector,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        self._store = store
        self._intent_sink = intent_sink
        self._session_store = session_store
        self._domain_projector = domain_projector
        self._clock = clock

    async def handle(self, action: InteractionAction) -> ApprovalResolveResult:
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
            # --- R1 Close-Loop ---
            # 1. Emit dismiss_island targeting the approval's surface_id
            surface_id = updated.surface_id
            if surface_id and self._intent_sink is not None:
                await self._intent_sink(CompanionIntent(
                    kind=IntentKind.DISMISS_ISLAND,
                    payload={"id": surface_id},
                ))

            # 2. Transition session phase to RUNNING if not terminal
            if self._session_store is not None and updated.session_id:
                session = self._session_store.get(updated.session_id)
                if session is not None and session.phase not in {
                    SessionPhase.COMPLETED, SessionPhase.FAILED
                }:
                    self._session_store.upsert(session.model_copy(
                        update={
                            "phase": SessionPhase.RUNNING,
                            "updated_at_ms": self._clock(),
                        }
                    ))

            # 3. Kick the domain projector to emit update_domain_state
            if self._domain_projector is not None:
                self._domain_projector._kick()

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


__all__ = ["ApprovalResolveResult", "ApprovalRouter", "IntentSink"]
