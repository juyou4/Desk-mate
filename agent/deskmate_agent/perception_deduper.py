"""Perception tick deduper (V10 Phase 9 · §4 Step 3 / L3-D1).

Swift already diffs at the source and only ships deltas across the
bridge. This deduper is the agent-side second line of defense: when
two consecutive ticks share a fingerprint and arrive within a
configurable gap, the second one is dropped before observers and
the proactive engine see it.

The gap is the product of a fixed *base* gap and a runtime
*widening factor*. The factor is read from a callable on every
tick so :class:`DegradationController.perception_widening_factor`
can drive it: at level ≥ 3 the factor is ``2.0`` and ticks coalesce
twice as aggressively, halving downstream CPU during degradation.

The deduper is pure: it has no side effects, no async, and never
emits intents. Callers ask :meth:`should_accept` and decide what to
do with the answer.
"""

from __future__ import annotations

from collections.abc import Callable

from .context import PerceptionSnapshot
from .logging_setup import get_logger

_LOG = get_logger("deskmate_agent.perception_deduper")


# Default base gap: 200 ms is short enough that real human-scale
# events (window switch, focus loss) always survive, while long
# enough that a noisy 50 Hz timer can't flood the agent with
# identical-fingerprint ticks. Tuned to be ~halfway between the
# Swift side's typical 100 ms cadence and the proactive chain's
# 1 s tick budget.
DEFAULT_BASE_GAP_MS = 200


class PerceptionDeduper:
    """Track the most-recent fingerprint + ts and gate identical ticks.

    A *fingerprint* is the tuple of fields we consider "context":
    user state, focus class, frontmost bundle id, window title.
    ``idle_ms`` / ``ts_ms`` deliberately stay out of the fingerprint
    so two ticks that only differ by elapsed-idle still dedup.

    Different fingerprints always pass — they're real context
    changes the proactive chain must see immediately.
    """

    def __init__(
        self,
        *,
        base_gap_ms: int = DEFAULT_BASE_GAP_MS,
        widening_factor_provider: Callable[[], float] | None = None,
    ) -> None:
        self._base_gap_ms = max(0, int(base_gap_ms))
        self._widening_factor_provider = (
            widening_factor_provider or (lambda: 1.0)
        )
        self._last_fingerprint: tuple[object, ...] | None = None
        self._last_accepted_ms: int | None = None
        self._dropped_count = 0

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    @property
    def base_gap_ms(self) -> int:
        return self._base_gap_ms

    @property
    def dropped_count(self) -> int:
        """Cumulative number of ticks the deduper has rejected."""
        return self._dropped_count

    def effective_gap_ms(self) -> int:
        """Current dedup gap = ``base_gap_ms * widening_factor()``.

        Reads the factor fresh on every call so degradation level
        changes take effect on the next tick without re-wiring.
        """
        factor = float(self._widening_factor_provider())
        return max(0, int(self._base_gap_ms * factor))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def should_accept(
        self, snapshot: PerceptionSnapshot, *, now_ms: int
    ) -> bool:
        """Return ``True`` when the tick should be processed downstream.

        Always accepts when the fingerprint differs from the last
        accepted snapshot, regardless of timing — context changes
        must always be observable. Same-fingerprint ticks are gated
        by :meth:`effective_gap_ms`.
        """
        fingerprint = self._fingerprint(snapshot)
        if self._last_fingerprint is None:
            self._last_fingerprint = fingerprint
            self._last_accepted_ms = now_ms
            return True
        if fingerprint != self._last_fingerprint:
            self._last_fingerprint = fingerprint
            self._last_accepted_ms = now_ms
            return True
        # Same fingerprint — check the elapsed gap.
        gap = self.effective_gap_ms()
        if (
            self._last_accepted_ms is None
            or (now_ms - self._last_accepted_ms) >= gap
        ):
            self._last_accepted_ms = now_ms
            return True
        self._dropped_count += 1
        return False

    def reset(self) -> None:
        """Forget the last accepted state. Useful in tests / hot
        reload — the next tick will accept unconditionally."""
        self._last_fingerprint = None
        self._last_accepted_ms = None
        self._dropped_count = 0

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _fingerprint(snap: PerceptionSnapshot) -> tuple[object, ...]:
        return (
            snap.user_state,
            snap.focus,
            snap.app_bundle_id,
            snap.window_title,
        )


__all__ = ["DEFAULT_BASE_GAP_MS", "PerceptionDeduper"]
