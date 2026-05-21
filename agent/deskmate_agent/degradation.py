"""Run-time degradation controller (V10 Phase 9 · Section 4).

The plan calls for a graduated six-step degradation path when
Instruments / battery / CPU alerts fire:

1. FPS down a tier (L3-A3)
2. Proactive interval ×2 + switch to ``ThresholdDecisionEngine``
   (L2-#5)
3. Perception-diff threshold widened (L3-D1)
4. Disable SneakPeek HUD / ``matchedGeometryEffect``
5. ``IslandSurface = .empty`` + ``orderOut`` (L3-A4)
6. Last resort — shut down the camera observer (V0.2)

All six steps are monotonic: entering level ``K`` implies levels
``1..K`` are also in effect. This module owns the one authoritative
int-typed level + a pub/sub so every participant (proactive engine,
Swift shell via DomainState mirror, menu-bar badge, …) can read a
consistent value.

The controller is side-effect-free: every policy decision is a pure
function of the current level. Triggers (battery drop, CPU spike)
live elsewhere and call :meth:`set_level`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field

from .logging_setup import get_logger

_LOG = get_logger("deskmate_agent.degradation")


# Level constants — integer so ``>=`` comparisons read naturally.
# Named for traceability against the V10 plan's numbered list.
LEVEL_NORMAL = 0       # no degradation
LEVEL_FPS_DOWN = 1     # step 1
LEVEL_PROACTIVE_X2 = 2  # step 2 — also switches to ThresholdDecisionEngine
LEVEL_PERCEPTION_WIDE = 3  # step 3
LEVEL_HIDE_HUD = 4     # step 4 — SneakPeek off, matchedGeometry off
LEVEL_ISLAND_OFF = 5   # step 5 — IslandSurface = .empty + orderOut
LEVEL_CAMERA_OFF = 6   # step 6 — camera observer disabled
MAX_LEVEL = LEVEL_CAMERA_OFF


@dataclass
class DegradationController:
    """Current degradation level + change notifications.

    The callback is invoked with the new level whenever it changes;
    identical-value sets don't fan out. Subscribers never see the
    initial value — read :attr:`level` for that.
    """

    initial_level: int = 0
    _level: int = field(init=False, default=0)
    _subscribers: list[Callable[[int], None]] = field(
        init=False, default_factory=list
    )

    def __post_init__(self) -> None:
        self._level = self._clamp(self.initial_level)

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------

    @property
    def level(self) -> int:
        return self._level

    def set_level(self, new_level: int, *, reason: str | None = None) -> bool:
        """Set the level (clamped 0..MAX_LEVEL). Returns ``True`` when
        the value actually changed."""
        clamped = self._clamp(new_level)
        if clamped == self._level:
            return False
        previous = self._level
        self._level = clamped
        _LOG.info(
            "degradation.level_change",
            previous=previous,
            current=clamped,
            reason=reason or "",
        )
        for cb in list(self._subscribers):
            try:
                cb(clamped)
            except Exception as exc:  # noqa: BLE001 — fail-soft
                _LOG.warning(
                    "degradation.subscriber_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        return True

    def subscribe(
        self, cb: Callable[[int], None]
    ) -> Callable[[], None]:
        self._subscribers.append(cb)

        def _unsub() -> None:
            with contextlib.suppress(ValueError):
                self._subscribers.remove(cb)

        return _unsub

    # ------------------------------------------------------------------
    # Derived policies — pure functions of the current level
    # ------------------------------------------------------------------

    def proactive_interval_multiplier(self) -> float:
        """Multiplier for proactive cooldowns / quotas.

        Step 2 doubles the base interval; no further change after.
        """
        return 2.0 if self._level >= LEVEL_PROACTIVE_X2 else 1.0

    def force_threshold_engine(self) -> bool:
        """At step 2 we switch away from the (more expensive) AI
        decision engine to the cheap threshold one."""
        return self._level >= LEVEL_PROACTIVE_X2

    def perception_widening_factor(self) -> float:
        """Step 3: perception diff thresholds widen (coarser dedup)."""
        return 2.0 if self._level >= LEVEL_PERCEPTION_WIDE else 1.0

    def hide_hud(self) -> bool:
        """Step 4: SneakPeek HUD + matchedGeometryEffect off."""
        return self._level >= LEVEL_HIDE_HUD

    def island_orderout(self) -> bool:
        """Step 5: the island window goes ``.empty`` and ``orderOut``."""
        return self._level >= LEVEL_ISLAND_OFF

    def camera_off(self) -> bool:
        """Step 6: the camera observer (V0.2 future) is disabled."""
        return self._level >= LEVEL_CAMERA_OFF

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _clamp(value: int) -> int:
        return max(0, min(MAX_LEVEL, int(value)))


__all__ = [
    "DegradationController",
    "LEVEL_NORMAL",
    "LEVEL_FPS_DOWN",
    "LEVEL_PROACTIVE_X2",
    "LEVEL_PERCEPTION_WIDE",
    "LEVEL_HIDE_HUD",
    "LEVEL_ISLAND_OFF",
    "LEVEL_CAMERA_OFF",
    "MAX_LEVEL",
]
