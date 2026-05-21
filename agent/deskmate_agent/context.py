"""Shared dataclasses for the reactive and proactive chains.

Kept intentionally small so both the orchestrator and the proactive engine can
import it without creating import cycles. Enumerations live in
``deskmate_agent.protocol.state`` to stay wire-compatible with Swift.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .protocol.state import AgentMood, Priority, UserFocus


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class PerceptionSnapshot:
    """Latest perception snapshot forwarded by the Swift shell.

    V10 L3-D1: Swift diffs against the previous snapshot and only ships deltas,
    so this value is expected to change infrequently.
    """

    user_state: str = "idle"
    focus: UserFocus = UserFocus.CASUAL
    app_bundle_id: str | None = None
    window_title: str | None = None
    idle_ms: int = 0
    ts_ms: int = field(default_factory=_now_ms)

    @property
    def idle_seconds(self) -> int:
        return self.idle_ms // 1000


@dataclass
class ProactiveContext:
    """Bundle of signals read by rule pre-filter + decision engine.

    ``urgency`` maps to the threshold table in
    :class:`~deskmate_agent.decision.threshold.ThresholdDecisionEngine`.
    """

    perception: PerceptionSnapshot
    last_user_message_ts_ms: int | None = None
    last_p2_ts_ms: int | None = None
    daily_p2_count: int = 0
    pet_in_nest: bool = False
    nest_duration_ms: int = 0
    urgency: str = "normal"  # urgent | high | medium | normal | low
    current_priority: Priority = Priority.P2
    current_mood: AgentMood = AgentMood.IDLE
    # V10 Phase 16-ii: today's cumulative coding time so the nudge
    # selector can escalate from generic pings to "long day, rest
    # your eyes" messages after a long stretch. Filled in by the
    # dispatcher from the latest :class:`DomainState` projection.
    coding_today_ms: int = 0

    @property
    def idle_seconds(self) -> int:
        return self.perception.idle_seconds


__all__ = ["PerceptionSnapshot", "ProactiveContext"]
