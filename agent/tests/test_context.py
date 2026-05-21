"""Smoke tests for shared context dataclasses (V10 Phase 1a)."""

from __future__ import annotations

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.protocol.state import AgentMood, Priority, UserFocus


def test_perception_defaults() -> None:
    snap = PerceptionSnapshot()
    assert snap.focus is UserFocus.CASUAL
    assert snap.idle_ms == 0
    assert snap.idle_seconds == 0
    assert snap.ts_ms > 0


def test_perception_idle_seconds_rounds_down() -> None:
    snap = PerceptionSnapshot(idle_ms=4999)
    assert snap.idle_seconds == 4


def test_proactive_context_defaults() -> None:
    ctx = ProactiveContext(perception=PerceptionSnapshot(idle_ms=60_000))
    assert ctx.idle_seconds == 60
    assert ctx.urgency == "normal"
    assert ctx.current_priority is Priority.P2
    assert ctx.current_mood is AgentMood.IDLE
    assert ctx.last_p2_ts_ms is None
