"""Deterministic rule pre-filter (V10 L3-B9).

Runs *before* the decision engine so 80-90% of ticks short-circuit without any
LLM call. Order rules cheapest → most expensive; the first blocking rule wins.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ProactiveContext
from ..protocol.state import Priority, UserFocus
from .cooldown import CooldownTracker


@dataclass(frozen=True)
class RuleResult:
    passed: bool
    reason: str


class RuleFilter:
    def __init__(
        self,
        cooldown: CooldownTracker,
        min_idle_seconds: int = 300,
        nest_min_stay_s: int = 300,
    ) -> None:
        self.cooldown = cooldown
        self.min_idle_seconds = min_idle_seconds
        self.nest_min_stay_s = nest_min_stay_s

    def check(self, ctx: ProactiveContext) -> RuleResult:
        # Cheapest checks first so we rarely reach the expensive ones.
        if ctx.perception.focus is UserFocus.FOCUSED:
            return RuleResult(False, "user_focused")
        if ctx.current_priority in {Priority.P0, Priority.P1}:
            return RuleResult(False, f"priority:{ctx.current_priority.value}")
        if ctx.idle_seconds < self.min_idle_seconds:
            return RuleResult(False, f"idle<{self.min_idle_seconds}s")
        if self.cooldown.within_cooldown():
            return RuleResult(False, "cooldown")
        if self.cooldown.over_quota():
            return RuleResult(False, "daily_quota")
        if ctx.pet_in_nest and ctx.nest_duration_ms < (self.nest_min_stay_s * 1000):
            return RuleResult(False, f"nesting<{self.nest_min_stay_s}s")
        return RuleResult(True, "rules_passed")


__all__ = ["RuleFilter", "RuleResult"]
