"""SwitchableDecisionEngine + DegradationController integration (V10 L2-#5).

Pins the contract for the auto-swap behaviour:

- Default routing follows the predicate's *current* return value.
- Each ``evaluate`` reads the predicate fresh — no caching beyond the
  edge-detection latch used for logging.
- Outcomes carry the *actually-evaluated* engine's kind, never
  ``AUTO`` (so observers can tell which engine made the call).
- Transitions log on each edge, never on stable evaluations, even
  when the predicate flaps repeatedly.
- Wired through :class:`DegradationController.force_threshold_engine`
  the engine flips to threshold at level ≥ 2 and back at level 0.
"""

from __future__ import annotations

import json

import pytest

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.decision import (
    DecisionEngine,
    DecisionOutcome,
    EngineKind,
    SwitchableDecisionEngine,
    ThresholdDecisionEngine,
)
from deskmate_agent.degradation import (
    LEVEL_FPS_DOWN,
    LEVEL_PROACTIVE_X2,
    DegradationController,
)


def _ctx() -> ProactiveContext:
    return ProactiveContext(
        perception=PerceptionSnapshot(),
        last_p2_ts_ms=None,
        urgency="normal",
    )


class _StubEngine(DecisionEngine):
    """Records calls + returns a configurable outcome."""

    def __init__(self, kind: EngineKind, *, should_respond: bool) -> None:
        self.kind = kind  # type: ignore[misc]  # instance shadow ok for tests
        self.calls = 0
        self._should_respond = should_respond

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        self.calls += 1
        return DecisionOutcome(
            should_respond=self._should_respond,
            reason=f"stub:{self.kind.value}",
            engine=self.kind,
        )


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_to_primary_when_predicate_false() -> None:
    primary = _StubEngine(EngineKind.AI, should_respond=True)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: False
    )
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.AI
    assert outcome.should_respond is True
    assert primary.calls == 1
    assert fallback.calls == 0


@pytest.mark.asyncio
async def test_routes_to_fallback_when_predicate_true() -> None:
    primary = _StubEngine(EngineKind.AI, should_respond=True)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: True
    )
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.THRESHOLD
    assert outcome.should_respond is False
    assert primary.calls == 0
    assert fallback.calls == 1


@pytest.mark.asyncio
async def test_predicate_re_evaluated_per_call() -> None:
    primary = _StubEngine(EngineKind.AI, should_respond=True)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    flag = {"use_fallback": False}
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: flag["use_fallback"]
    )

    await engine.evaluate(_ctx())
    flag["use_fallback"] = True
    await engine.evaluate(_ctx())
    flag["use_fallback"] = False
    await engine.evaluate(_ctx())

    assert primary.calls == 2
    assert fallback.calls == 1


# ---------------------------------------------------------------------------
# Edge logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latch_tracks_predicate_after_each_evaluate() -> None:
    """``using_fallback`` reflects the most recent predicate result."""

    primary = _StubEngine(EngineKind.AI, should_respond=False)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    flag = {"use_fallback": False}
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: flag["use_fallback"]
    )

    assert engine.using_fallback is False
    await engine.evaluate(_ctx())
    assert engine.using_fallback is False

    flag["use_fallback"] = True
    await engine.evaluate(_ctx())
    assert engine.using_fallback is True

    # Stable second eval at fallback keeps the latch.
    await engine.evaluate(_ctx())
    assert engine.using_fallback is True

    flag["use_fallback"] = False
    await engine.evaluate(_ctx())
    assert engine.using_fallback is False


@pytest.mark.asyncio
async def test_transition_logs_only_on_edge(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Each predicate flip emits exactly one structlog transition record."""

    primary = _StubEngine(EngineKind.AI, should_respond=False)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    flag = {"use_fallback": False}
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: flag["use_fallback"]
    )

    def _drain_transitions() -> list[dict[str, object]]:
        out = capsys.readouterr().out
        events: list[dict[str, object]] = []
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if ev.get("event") == "decision.switchable.transition":
                events.append(ev)
        return events

    # Stable at primary → no edge events.
    await engine.evaluate(_ctx())
    await engine.evaluate(_ctx())
    assert _drain_transitions() == []

    # Edge to fallback.
    flag["use_fallback"] = True
    await engine.evaluate(_ctx())
    rising = _drain_transitions()
    assert len(rising) == 1
    assert rising[0]["from_engine"] == EngineKind.AI.value
    assert rising[0]["to_engine"] == EngineKind.THRESHOLD.value
    assert rising[0]["using_fallback"] is True

    # Stable at fallback → no extra edge events.
    await engine.evaluate(_ctx())
    assert _drain_transitions() == []

    # Edge back to primary.
    flag["use_fallback"] = False
    await engine.evaluate(_ctx())
    falling = _drain_transitions()
    assert len(falling) == 1
    assert falling[0]["from_engine"] == EngineKind.THRESHOLD.value
    assert falling[0]["to_engine"] == EngineKind.AI.value
    assert falling[0]["using_fallback"] is False


@pytest.mark.asyncio
async def test_initial_latch_matches_predicate() -> None:
    """Constructed at fallback → no spurious transition on first call."""

    primary = _StubEngine(EngineKind.AI, should_respond=False)
    fallback = _StubEngine(EngineKind.THRESHOLD, should_respond=False)
    engine = SwitchableDecisionEngine(
        primary, fallback, should_use_fallback=lambda: True
    )
    assert engine.using_fallback is True
    # Predicate stays True; no edge.
    await engine.evaluate(_ctx())
    assert engine.using_fallback is True


# ---------------------------------------------------------------------------
# DegradationController integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degradation_drives_engine_swap() -> None:
    primary = _StubEngine(EngineKind.AI, should_respond=True)
    fallback = ThresholdDecisionEngine({"normal": 0})
    controller = DegradationController()
    engine = SwitchableDecisionEngine(
        primary,
        fallback,
        should_use_fallback=controller.force_threshold_engine,
    )

    # Level 0 → primary owns the verdict.
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.AI
    assert primary.calls == 1

    # Level 1 (FPS down only) does *not* yet flip the engine.
    controller.set_level(LEVEL_FPS_DOWN)
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.AI
    assert primary.calls == 2

    # Level 2 → SwitchableDecisionEngine routes to threshold fallback.
    controller.set_level(LEVEL_PROACTIVE_X2)
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.THRESHOLD
    assert primary.calls == 2  # primary stayed silent on level 2

    # Drop back below the threshold → primary resumes.
    controller.set_level(0)
    outcome = await engine.evaluate(_ctx())
    assert outcome.engine is EngineKind.AI
    assert primary.calls == 3
