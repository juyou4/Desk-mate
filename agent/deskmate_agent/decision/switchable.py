"""Switchable decision engine (V10 L2-#5).

Wraps a primary engine + a fallback engine; on every ``evaluate`` it
asks a predicate which one to route to. Used to honour
:meth:`DegradationController.force_threshold_engine` so the proactive
chain drops the (potentially expensive) AI probe once the system
enters ``LEVEL_PROACTIVE_X2`` or higher, and resumes it when the
level falls back below the threshold.

The wrapper is *lazy*: the predicate is read at the start of each
``evaluate`` call. There is no subscription to the controller, so
construction order doesn't matter and tests can drive the predicate
directly with a plain bool flag.

Transitions are logged exactly once per edge so an operator can
correlate degradation entry/exit with engine swaps.
"""

from __future__ import annotations

from collections.abc import Callable

from ..context import ProactiveContext
from ..logging_setup import get_logger
from .base import DecisionEngine, DecisionOutcome, EngineKind

_LOG = get_logger("deskmate_agent.decision.switchable")


class SwitchableDecisionEngine(DecisionEngine):
    """Route to ``primary`` unless ``should_use_fallback()`` returns True.

    The composite engine surfaces ``EngineKind.AUTO`` only as a static
    type marker; each :class:`DecisionOutcome` carries the *actual*
    engine kind that produced the verdict (so logs / metrics still see
    ``threshold`` vs ``ai`` cleanly).
    """

    kind = EngineKind.AUTO

    def __init__(
        self,
        primary: DecisionEngine,
        fallback: DecisionEngine,
        *,
        should_use_fallback: Callable[[], bool],
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._should_use_fallback = should_use_fallback
        # Latch the current edge so the transition log fires only on
        # change, not on every evaluation.
        self._using_fallback: bool = bool(should_use_fallback())

    @property
    def primary(self) -> DecisionEngine:
        return self._primary

    @property
    def fallback(self) -> DecisionEngine:
        return self._fallback

    @property
    def using_fallback(self) -> bool:
        """Latched edge state — refreshed on each ``evaluate``."""
        return self._using_fallback

    @property
    def active(self) -> DecisionEngine:
        """Engine that *would* serve the next ``evaluate`` if called now.

        Reads the predicate fresh; useful for diagnostics / tests.
        """
        return self._fallback if self._should_use_fallback() else self._primary

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        target_fallback = bool(self._should_use_fallback())
        if target_fallback != self._using_fallback:
            from_engine = (
                self._fallback if self._using_fallback else self._primary
            )
            to_engine = (
                self._fallback if target_fallback else self._primary
            )
            _LOG.info(
                "decision.switchable.transition",
                from_engine=from_engine.kind.value,
                to_engine=to_engine.kind.value,
                using_fallback=target_fallback,
            )
            self._using_fallback = target_fallback
        engine = self._fallback if target_fallback else self._primary
        return await engine.evaluate(ctx)


__all__ = ["SwitchableDecisionEngine"]
