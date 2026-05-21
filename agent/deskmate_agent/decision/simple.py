"""Time-threshold engine — deterministic, no LLM, no external I/O.

This is the *last resort* fallback when both the AI probe and the threshold
engine are unavailable. It is also cheap enough to run unconditionally during
tests and low-power scenarios (V10 L3-A8 low-power mode).
"""

from __future__ import annotations

import time

from ..context import ProactiveContext
from .base import DecisionEngine, DecisionOutcome, EngineKind


class SimpleDecisionEngine(DecisionEngine):
    kind = EngineKind.SIMPLE

    def __init__(
        self,
        min_response_interval_s: int = 60,
        max_response_interval_s: int = 3600,
    ) -> None:
        self.min_response_interval_s = min_response_interval_s
        self.max_response_interval_s = max_response_interval_s

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        now_ms = int(time.time() * 1000)
        last = ctx.last_p2_ts_ms
        # Treat "never spoke" as a long elapsed so we still respect the min
        # interval but never block forever.
        elapsed_s = ((now_ms - last) // 1000) if last is not None else 10_000

        if elapsed_s > self.max_response_interval_s:
            return DecisionOutcome(
                should_respond=True,
                reason=f"max_interval_exceeded({elapsed_s}s>{self.max_response_interval_s}s)",
                engine=self.kind,
            )
        if elapsed_s < self.min_response_interval_s:
            return DecisionOutcome(
                should_respond=False,
                reason=f"below_min_interval({elapsed_s}s<{self.min_response_interval_s}s)",
                engine=self.kind,
            )

        threshold = self.min_response_interval_s * 2
        should = elapsed_s >= threshold
        return DecisionOutcome(
            should_respond=should,
            reason=(
                f"simple({elapsed_s}s>={threshold}s)"
                if should
                else f"waiting({elapsed_s}s<{threshold}s)"
            ),
            engine=self.kind,
        )


__all__ = ["SimpleDecisionEngine"]
