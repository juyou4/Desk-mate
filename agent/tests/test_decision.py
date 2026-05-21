"""Decision engine tests (V10 L2-#5).

Each test pins a contract:

- The three engines share the same ``evaluate`` signature.
- ``AIBasedDecisionEngine`` *always* falls back on exceptions (no propagation).
- ``make_decision_engine(AUTO, ai_probe=None)`` downgrades to Threshold.
- ``make_decision_engine(AI, ai_probe=None)`` fails fast — explicit kinds are strict.
"""

from __future__ import annotations

import time

import pytest

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.decision import (
    DEFAULT_THRESHOLDS_S,
    AIBasedDecisionEngine,
    EngineKind,
    SimpleDecisionEngine,
    ThresholdDecisionEngine,
    make_decision_engine,
)


def _ctx(
    *,
    last_p2_s_ago: int | None = None,
    urgency: str = "normal",
    focus: str | None = None,
) -> ProactiveContext:
    snap = PerceptionSnapshot()
    now_ms = int(time.time() * 1000)
    return ProactiveContext(
        perception=snap,
        last_p2_ts_ms=None if last_p2_s_ago is None else now_ms - last_p2_s_ago * 1000,
        urgency=urgency,
    )


# ---------------------------------------------------------------------------
# SimpleDecisionEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_blocks_below_min_interval() -> None:
    engine = SimpleDecisionEngine(min_response_interval_s=60)
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=30))
    assert outcome.should_respond is False
    assert outcome.engine is EngineKind.SIMPLE
    assert "below_min_interval" in outcome.reason


@pytest.mark.asyncio
async def test_simple_forces_above_max_interval() -> None:
    engine = SimpleDecisionEngine(min_response_interval_s=60, max_response_interval_s=3600)
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=7200))
    assert outcome.should_respond is True
    assert "max_interval_exceeded" in outcome.reason


@pytest.mark.asyncio
async def test_simple_speaks_after_two_min_intervals() -> None:
    engine = SimpleDecisionEngine(min_response_interval_s=60)
    # Exactly 2x min interval → should speak.
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=130))
    assert outcome.should_respond is True


# ---------------------------------------------------------------------------
# ThresholdDecisionEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_threshold_respects_urgency_table() -> None:
    engine = ThresholdDecisionEngine()
    urgent_default = DEFAULT_THRESHOLDS_S["urgent"]
    # 10s elapsed under urgent (30s threshold) → still waiting.
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=urgent_default - 5, urgency="urgent"))
    assert outcome.should_respond is False

    # 40s elapsed under urgent → speak.
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=urgent_default + 10, urgency="urgent"))
    assert outcome.should_respond is True


@pytest.mark.asyncio
async def test_threshold_unknown_urgency_falls_back_to_normal() -> None:
    engine = ThresholdDecisionEngine()
    normal = DEFAULT_THRESHOLDS_S["normal"]
    outcome = await engine.evaluate(
        _ctx(last_p2_s_ago=normal + 5, urgency="unknown-urgency-level")
    )
    assert outcome.should_respond is True


# ---------------------------------------------------------------------------
# AIBasedDecisionEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ai_based_happy_path() -> None:
    async def probe(_ctx: ProactiveContext) -> bool:
        return True

    engine = AIBasedDecisionEngine(probe)
    outcome = await engine.evaluate(_ctx())
    assert outcome.should_respond is True
    assert outcome.engine is EngineKind.AI


@pytest.mark.asyncio
async def test_ai_based_falls_back_on_exception() -> None:
    async def boom(_ctx: ProactiveContext) -> bool:
        raise RuntimeError("provider down")

    # Fallback forced to speak so we can distinguish the outcome clearly.
    fallback = ThresholdDecisionEngine({"normal": 0})
    engine = AIBasedDecisionEngine(boom, fallback=fallback)
    outcome = await engine.evaluate(_ctx(last_p2_s_ago=10))
    assert outcome.should_respond is True
    assert outcome.engine is EngineKind.THRESHOLD
    assert outcome.reason.startswith("ai_error(RuntimeError)")


# ---------------------------------------------------------------------------
# Factory / auto-downgrade
# ---------------------------------------------------------------------------


def test_factory_auto_without_probe_yields_threshold() -> None:
    engine = make_decision_engine(EngineKind.AUTO)
    assert isinstance(engine, ThresholdDecisionEngine)


def test_factory_auto_with_probe_yields_ai() -> None:
    async def probe(_ctx: ProactiveContext) -> bool:
        return False

    engine = make_decision_engine(EngineKind.AUTO, ai_probe=probe)
    assert isinstance(engine, AIBasedDecisionEngine)


def test_factory_explicit_ai_without_probe_fails_fast() -> None:
    with pytest.raises(ValueError):
        make_decision_engine(EngineKind.AI)


def test_factory_simple_and_threshold_are_strict() -> None:
    assert isinstance(make_decision_engine(EngineKind.SIMPLE), SimpleDecisionEngine)
    assert isinstance(make_decision_engine(EngineKind.THRESHOLD), ThresholdDecisionEngine)
