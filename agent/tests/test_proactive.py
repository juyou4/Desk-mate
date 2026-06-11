"""Proactive chain tests: cooldown + rule filter + engine composition."""

from __future__ import annotations

import pytest

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.decision.base import DecisionEngine, DecisionOutcome, EngineKind
from deskmate_agent.proactive import (
    CooldownTracker,
    ProactiveEngine,
    RuleFilter,
)
from deskmate_agent.proactive.cooldown import ONE_DAY_MS
from deskmate_agent.protocol.state import Priority, UserFocus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FixedEngine(DecisionEngine):
    """Decision engine that always returns a configured outcome."""

    kind = EngineKind.SIMPLE

    def __init__(self, should: bool, reason: str = "fixed") -> None:
        self._should = should
        self._reason = reason

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        return DecisionOutcome(
            should_respond=self._should,
            reason=self._reason,
            engine=self.kind,
        )


def _ctx(
    *,
    idle_ms: int = 600_000,
    focus: UserFocus = UserFocus.CASUAL,
    pet_in_nest: bool = False,
    nest_duration_ms: int = 0,
    current_priority: Priority = Priority.P2,
) -> ProactiveContext:
    return ProactiveContext(
        perception=PerceptionSnapshot(idle_ms=idle_ms, focus=focus),
        pet_in_nest=pet_in_nest,
        nest_duration_ms=nest_duration_ms,
        current_priority=current_priority,
    )


# ---------------------------------------------------------------------------
# CooldownTracker
# ---------------------------------------------------------------------------


def test_cooldown_initial_state_allows_trigger() -> None:
    cd = CooldownTracker(min_cooldown_s=60, daily_quota=2)
    assert cd.within_cooldown() is False
    assert cd.over_quota() is False


def test_cooldown_record_blocks_until_min_elapsed() -> None:
    now = 1_000_000_000_000
    cd = CooldownTracker(min_cooldown_s=60, daily_quota=10)
    cd.record_trigger(now_ms=now)
    assert cd.within_cooldown(now_ms=now + 30_000) is True
    assert cd.within_cooldown(now_ms=now + 61_000) is False


def test_cooldown_daily_quota_rolls_over_after_24h() -> None:
    now = 1_000_000_000_000
    cd = CooldownTracker(min_cooldown_s=0, daily_quota=2)
    cd.record_trigger(now_ms=now)
    cd.record_trigger(now_ms=now + 1_000)
    assert cd.over_quota(now_ms=now + 2_000) is True

    # 24h later, the window rolls and quota resets.
    later = now + ONE_DAY_MS + 1_000
    assert cd.over_quota(now_ms=later) is False


def test_cooldown_multiplier_provider_extends_cooldown() -> None:
    now = 1_000_000_000_000
    multiplier = 2.0
    cd = CooldownTracker(
        min_cooldown_s=60,
        interval_multiplier_provider=lambda: multiplier,
    )
    cd.record_trigger(now_ms=now)
    # At 2x, 90 s still inside cooldown (base 60 × 2 = 120 s).
    assert cd.within_cooldown(now_ms=now + 90_000) is True
    # Past 2x base.
    assert cd.within_cooldown(now_ms=now + 125_000) is False


def test_cooldown_multiplier_provider_runtime_swap() -> None:
    """A live degradation controller increases the level mid-run;
    the cooldown must widen on the next query without any re-wire."""
    now = 1_000_000_000_000
    multiplier = 1.0
    cd = CooldownTracker(
        min_cooldown_s=60,
        interval_multiplier_provider=lambda: multiplier,
    )
    cd.record_trigger(now_ms=now)
    # Base level: 80s past base (>60s) frees the cooldown.
    assert cd.within_cooldown(now_ms=now + 80_000) is False
    # Escalation: multiplier flips to 2x, cooldown re-widens.
    multiplier = 2.0
    assert cd.within_cooldown(now_ms=now + 80_000) is True


def test_cooldown_multiplier_under_one_is_clamped() -> None:
    """A misconfigured provider (accidental <1.0) must not shorten the
    configured baseline — we clamp to 1.0."""
    cd = CooldownTracker(
        min_cooldown_s=60, interval_multiplier_provider=lambda: 0.25
    )
    assert cd.effective_cooldown_ms() == 60_000


def test_cooldown_multiplier_provider_failure_falls_back_to_one() -> None:
    def bad_provider() -> float:
        raise RuntimeError("oops")

    cd = CooldownTracker(
        min_cooldown_s=60, interval_multiplier_provider=bad_provider
    )
    assert cd.effective_cooldown_ms() == 60_000


# ---------------------------------------------------------------------------
# RuleFilter
# ---------------------------------------------------------------------------


def test_rule_filter_blocks_focused_user() -> None:
    rules = RuleFilter(CooldownTracker(), min_idle_seconds=60)
    result = rules.check(_ctx(focus=UserFocus.FOCUSED))
    assert result.passed is False
    assert result.reason == "user_focused"


def test_rule_filter_blocks_short_idle() -> None:
    rules = RuleFilter(CooldownTracker(), min_idle_seconds=60)
    result = rules.check(_ctx(idle_ms=10_000))
    assert result.passed is False
    assert result.reason == "idle<60s"


def test_rule_filter_blocks_when_high_priority_surface_active() -> None:
    rules = RuleFilter(CooldownTracker(), min_idle_seconds=60)
    result = rules.check(
        _ctx(idle_ms=600_000, current_priority=Priority.P1)
    )
    assert result.passed is False
    assert result.reason == "priority:P1"


def test_rule_filter_blocks_cooldown() -> None:
    cd = CooldownTracker(min_cooldown_s=60)
    cd.record_trigger()
    rules = RuleFilter(cd, min_idle_seconds=60)
    result = rules.check(_ctx(idle_ms=120_000))
    assert result.passed is False
    assert result.reason == "cooldown"


def test_rule_filter_blocks_quota() -> None:
    cd = CooldownTracker(min_cooldown_s=0, daily_quota=1)
    cd.record_trigger()
    rules = RuleFilter(cd, min_idle_seconds=60)
    result = rules.check(_ctx(idle_ms=120_000))
    assert result.passed is False
    assert result.reason == "daily_quota"


def test_rule_filter_blocks_short_nest_stay() -> None:
    rules = RuleFilter(CooldownTracker(), min_idle_seconds=60, nest_min_stay_s=300)
    result = rules.check(
        _ctx(idle_ms=600_000, pet_in_nest=True, nest_duration_ms=60_000)
    )
    assert result.passed is False
    assert result.reason.startswith("nesting<")


def test_rule_filter_passes_when_all_good() -> None:
    rules = RuleFilter(CooldownTracker(), min_idle_seconds=60)
    result = rules.check(_ctx(idle_ms=600_000))
    assert result.passed is True
    assert result.reason == "rules_passed"


# ---------------------------------------------------------------------------
# ProactiveEngine composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_short_circuits_on_rule_block() -> None:
    engine = ProactiveEngine(
        decision_engine=_FixedEngine(should=True, reason="would-speak"),
        cooldown=CooldownTracker(min_cooldown_s=60),
    )
    # Focused user → rule block before decision engine ever runs.
    result = await engine.maybe_trigger(_ctx(focus=UserFocus.FOCUSED))
    assert result.should_trigger is False
    assert result.reason.startswith("rule:user_focused")
    assert result.decision_engine is None


@pytest.mark.asyncio
async def test_engine_defers_to_decision_when_rules_pass() -> None:
    cd = CooldownTracker(min_cooldown_s=0, daily_quota=100)
    engine = ProactiveEngine(
        decision_engine=_FixedEngine(should=True, reason="ok"),
        cooldown=cd,
    )
    result = await engine.maybe_trigger(_ctx(idle_ms=600_000))
    assert result.should_trigger is True
    assert result.reason.startswith("engine:ok")
    assert result.decision_engine is EngineKind.SIMPLE


@pytest.mark.asyncio
async def test_engine_record_trigger_charges_cooldown() -> None:
    cd = CooldownTracker(min_cooldown_s=60, daily_quota=10)
    engine = ProactiveEngine(
        decision_engine=_FixedEngine(should=True),
        cooldown=cd,
    )
    assert cd.within_cooldown() is False
    engine.record_trigger()
    assert cd.within_cooldown() is True
