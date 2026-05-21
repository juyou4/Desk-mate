"""ProactiveEngine: composes the rule pre-filter with a decision engine."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ProactiveContext
from ..decision.base import DecisionEngine, EngineKind
from .cooldown import CooldownTracker
from .rule_filter import RuleFilter


@dataclass(frozen=True)
class ProactiveResult:
    should_trigger: bool
    reason: str
    decision_engine: EngineKind | None = None


class ProactiveEngine:
    """V10 L2-#5 + L3-B9.

    The engine:
      1. Runs the rule pre-filter (cheap + deterministic).
      2. If rules pass, consults the decision engine.
      3. Never charges the cooldown itself — callers call
         :meth:`record_trigger` after the intent has actually been emitted.
         This keeps the engine pure.
    """

    def __init__(
        self,
        decision_engine: DecisionEngine,
        *,
        cooldown: CooldownTracker | None = None,
        rule_filter: RuleFilter | None = None,
    ) -> None:
        self.cooldown = cooldown or CooldownTracker()
        self.rule_filter = rule_filter or RuleFilter(self.cooldown)
        self.decision_engine = decision_engine

    async def maybe_trigger(self, ctx: ProactiveContext) -> ProactiveResult:
        rule_check = self.rule_filter.check(ctx)
        if not rule_check.passed:
            return ProactiveResult(
                should_trigger=False,
                reason=f"rule:{rule_check.reason}",
            )
        outcome = await self.decision_engine.evaluate(ctx)
        return ProactiveResult(
            should_trigger=outcome.should_respond,
            reason=f"engine:{outcome.reason}",
            decision_engine=outcome.engine,
        )

    def record_trigger(self) -> None:
        """Charge the cooldown + quota after an accepted trigger has spoken."""
        self.cooldown.record_trigger()


__all__ = ["ProactiveEngine", "ProactiveResult"]
