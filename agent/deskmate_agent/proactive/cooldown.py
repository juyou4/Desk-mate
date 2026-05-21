"""Cooldown + daily-quota tracker for the proactive chain."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

ONE_DAY_MS: int = 24 * 60 * 60 * 1000


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class CooldownTracker:
    """Tracks time-since-last-trigger and a rolling 24h quota.

    Defaults mirror the V10 plan:

    - ``min_cooldown_s = 1200`` (P2 at least 20 minutes apart)
    - ``daily_quota = 8`` proactive triggers per 24h window

    The quota window is *lazy*: it only starts counting at the first recorded
    trigger. This keeps test timestamps (which use synthetic ``now_ms``) and
    wall-clock timestamps from drifting against each other.

    V10 Phase 9: ``interval_multiplier_provider`` lets the controller
    plug in an arbitrary >= 1.0 float (typically driven by
    :class:`DegradationController`). At level 2+ the plan doubles the
    cooldown without touching the configured constant, so the tracker
    stays comparable across levels.
    """

    min_cooldown_s: int = 1200
    daily_quota: int = 8
    last_ts_ms: int | None = None
    daily_count: int = 0
    window_start_ts_ms: int | None = None
    # Provider rather than a stored value so the degradation level can
    # change between ticks without anyone rewiring the cooldown.
    interval_multiplier_provider: Callable[[], float] = field(
        default=lambda: 1.0
    )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def effective_cooldown_ms(self) -> int:
        try:
            multiplier = float(self.interval_multiplier_provider())
        except Exception:  # noqa: BLE001 — never let a bad provider crash
            multiplier = 1.0
        if multiplier < 1.0:
            multiplier = 1.0
        return int(self.min_cooldown_s * 1000 * multiplier)

    def within_cooldown(self, now_ms: int | None = None) -> bool:
        if self.last_ts_ms is None:
            return False
        now_ms = now_ms if now_ms is not None else _now_ms()
        return (now_ms - self.last_ts_ms) < self.effective_cooldown_ms()

    def over_quota(self, now_ms: int | None = None) -> bool:
        if self.window_start_ts_ms is None:
            return False
        now_ms = now_ms if now_ms is not None else _now_ms()
        self._rollover_window_if_needed(now_ms)
        return self.daily_count >= self.daily_quota

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def record_trigger(self, now_ms: int | None = None) -> None:
        now_ms = now_ms if now_ms is not None else _now_ms()
        if self.window_start_ts_ms is None:
            self.window_start_ts_ms = now_ms
        else:
            self._rollover_window_if_needed(now_ms)
        self.last_ts_ms = now_ms
        self.daily_count += 1

    def reset_daily(self, now_ms: int | None = None) -> None:
        now_ms = now_ms if now_ms is not None else _now_ms()
        self.window_start_ts_ms = now_ms
        self.daily_count = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _rollover_window_if_needed(self, now_ms: int) -> None:
        assert self.window_start_ts_ms is not None
        if (now_ms - self.window_start_ts_ms) >= ONE_DAY_MS:
            self.window_start_ts_ms = now_ms
            self.daily_count = 0


__all__ = ["CooldownTracker", "ONE_DAY_MS"]
