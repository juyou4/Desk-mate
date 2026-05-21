"""Proactive decision engines (V10 L2-#5).

Three engines share a single protocol; :func:`make_decision_engine` downgrades
``auto`` to the best available option based on whether an AI probe is wired.
"""

from __future__ import annotations

from .ai_based import AIBasedDecisionEngine, ShouldRespondAI
from .base import DecisionEngine, DecisionOutcome, EngineKind
from .factory import make_decision_engine
from .simple import SimpleDecisionEngine
from .switchable import SwitchableDecisionEngine
from .threshold import DEFAULT_THRESHOLDS_S, ThresholdDecisionEngine

__all__ = [
    "AIBasedDecisionEngine",
    "DEFAULT_THRESHOLDS_S",
    "DecisionEngine",
    "DecisionOutcome",
    "EngineKind",
    "ShouldRespondAI",
    "SimpleDecisionEngine",
    "SwitchableDecisionEngine",
    "ThresholdDecisionEngine",
    "make_decision_engine",
]
