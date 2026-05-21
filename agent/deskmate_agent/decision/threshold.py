"""Per-urgency threshold engine (no LLM).

Thresholds are configurable; the defaults come from the V10 plan. The engine
reads ``ctx.urgency`` which is derived by the perception → router layer.
"""

from __future__ import annotations

import time

from ..context import ProactiveContext
from .base import DecisionEngine, DecisionOutcome, EngineKind

DEFAULT_THRESHOLDS_S: dict[str, int] = {
    "urgent": 30,
    "high": 120,
    "medium": 300,
    "normal": 600,
    "low": 1200,
}


class ThresholdDecisionEngine(DecisionEngine):
    kind = EngineKind.THRESHOLD

    def __init__(self, thresholds_s: dict[str, int] | None = None) -> None:
        self.thresholds_s = dict(DEFAULT_THRESHOLDS_S)
        if thresholds_s:
            self.thresholds_s.update(thresholds_s)

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        now_ms = int(time.time() * 1000)
        last = ctx.last_p2_ts_ms
        elapsed_s = ((now_ms - last) // 1000) if last is not None else 10_000

        threshold = self.thresholds_s.get(
            ctx.urgency, self.thresholds_s["normal"]
        )
        should = elapsed_s >= threshold
        op = ">=" if should else "<"
        return DecisionOutcome(
            should_respond=should,
            reason=f"threshold[{ctx.urgency}]({elapsed_s}s{op}{threshold}s)",
            engine=self.kind,
        )


__all__ = ["DEFAULT_THRESHOLDS_S", "ThresholdDecisionEngine"]
