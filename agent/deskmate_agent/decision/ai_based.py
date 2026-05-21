"""AI-backed decision engine with transparent fallback on failure.

The engine accepts any awaitable ``ShouldRespondAI`` callable so this module
stays decoupled from the LLM client wiring (which lands in Phase 10). In
tests we pass a plain coroutine; in production Phase 10 supplies a
DeepSeek/Ollama-backed probe.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..context import ProactiveContext
from .base import DecisionEngine, DecisionOutcome, EngineKind
from .threshold import ThresholdDecisionEngine


class ShouldRespondAI(Protocol):
    """Async callable returning whether the AI recommends speaking."""

    async def __call__(self, ctx: ProactiveContext) -> bool: ...


class AIBasedDecisionEngine(DecisionEngine):
    kind = EngineKind.AI

    def __init__(
        self,
        ai_probe: ShouldRespondAI | Callable[[ProactiveContext], Awaitable[bool]],
        fallback: DecisionEngine | None = None,
    ) -> None:
        self._probe = ai_probe
        self._fallback = fallback or ThresholdDecisionEngine()

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        try:
            decision = bool(await self._probe(ctx))
        except Exception as exc:  # noqa: BLE001 — we want *every* failure to fall back
            fallback = await self._fallback.evaluate(ctx)
            return DecisionOutcome(
                should_respond=fallback.should_respond,
                reason=f"ai_error({type(exc).__name__})->{fallback.reason}",
                engine=fallback.engine,
            )
        return DecisionOutcome(
            should_respond=decision,
            reason=f"ai({decision})",
            engine=self.kind,
        )


__all__ = ["AIBasedDecisionEngine", "ShouldRespondAI"]
