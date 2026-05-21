"""Decision engine base type and outcome record."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ..context import ProactiveContext


class EngineKind(StrEnum):
    """Configurable engine selection. ``AUTO`` picks the best available."""

    AI = "ai"
    THRESHOLD = "threshold"
    SIMPLE = "simple"
    AUTO = "auto"


@dataclass(frozen=True)
class DecisionOutcome:
    should_respond: bool
    reason: str
    engine: EngineKind


class DecisionEngine(ABC):
    """Every engine implements a single async ``evaluate`` method."""

    kind: EngineKind

    @abstractmethod
    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        """Return whether the proactive chain should speak *now*."""


__all__ = ["DecisionEngine", "DecisionOutcome", "EngineKind"]
