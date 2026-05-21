"""Engine factory + ``auto`` downgrade policy."""

from __future__ import annotations

from .ai_based import AIBasedDecisionEngine, ShouldRespondAI
from .base import DecisionEngine, EngineKind
from .simple import SimpleDecisionEngine
from .threshold import ThresholdDecisionEngine


def make_decision_engine(
    kind: EngineKind = EngineKind.AUTO,
    *,
    ai_probe: ShouldRespondAI | None = None,
    thresholds_s: dict[str, int] | None = None,
) -> DecisionEngine:
    """Construct an engine by requested ``kind``.

    ``AUTO`` prefers AI → threshold → simple, downgrading whenever the
    dependency (AI probe) isn't supplied. Explicit kinds are honoured strictly;
    requesting :attr:`EngineKind.AI` without a probe raises ``ValueError`` so
    callers fail fast rather than run with silent degradation.
    """
    if kind is EngineKind.SIMPLE:
        return SimpleDecisionEngine()
    if kind is EngineKind.THRESHOLD:
        return ThresholdDecisionEngine(thresholds_s)
    if kind is EngineKind.AI:
        if ai_probe is None:
            raise ValueError("EngineKind.AI requires an ai_probe callable")
        return AIBasedDecisionEngine(
            ai_probe,
            fallback=ThresholdDecisionEngine(thresholds_s),
        )
    # AUTO
    if ai_probe is not None:
        return AIBasedDecisionEngine(
            ai_probe,
            fallback=ThresholdDecisionEngine(thresholds_s),
        )
    return ThresholdDecisionEngine(thresholds_s)


__all__ = ["make_decision_engine"]
