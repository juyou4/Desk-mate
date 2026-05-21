"""PerceptionDeduper tests (V10 Phase 9 · §4 Step 3 / L3-D1).

Pin the contract:

- First tick is always accepted.
- Different fingerprint always passes regardless of timing.
- Same fingerprint within ``effective_gap_ms`` is dropped + counted.
- ``widening_factor_provider`` is read fresh on every call so
  degradation level changes take effect on the next tick.
- ``reset`` clears the latch + counter.
"""

from __future__ import annotations

from deskmate_agent.context import PerceptionSnapshot
from deskmate_agent.perception_deduper import (
    DEFAULT_BASE_GAP_MS,
    PerceptionDeduper,
)
from deskmate_agent.protocol.state import UserFocus


def _snap(
    *,
    user_state: str = "idle",
    focus: UserFocus = UserFocus.CASUAL,
    bundle: str | None = "com.apple.Terminal",
    title: str | None = "bash",
    idle_ms: int = 0,
) -> PerceptionSnapshot:
    return PerceptionSnapshot(
        user_state=user_state,
        focus=focus,
        app_bundle_id=bundle,
        window_title=title,
        idle_ms=idle_ms,
    )


# ---------------------------------------------------------------------------
# Fingerprint matching
# ---------------------------------------------------------------------------


def test_first_tick_always_accepts() -> None:
    d = PerceptionDeduper()
    assert d.should_accept(_snap(), now_ms=0) is True
    assert d.dropped_count == 0


def test_idle_ms_change_alone_does_not_pass_after_first_accept() -> None:
    """Two ticks differing only in elapsed-idle share a fingerprint —
    the second one inside the gap must be dropped."""
    d = PerceptionDeduper(base_gap_ms=200)
    assert d.should_accept(_snap(idle_ms=0), now_ms=0) is True
    # 50 ms later, idle_ms bumped — fingerprint identical.
    assert d.should_accept(_snap(idle_ms=50), now_ms=50) is False
    assert d.dropped_count == 1


def test_window_change_passes_immediately() -> None:
    d = PerceptionDeduper(base_gap_ms=10_000)
    assert d.should_accept(_snap(title="bash"), now_ms=0) is True
    # Same bundle, different window title → fingerprint differs.
    assert d.should_accept(_snap(title="vim"), now_ms=10) is True


def test_bundle_change_passes_immediately() -> None:
    d = PerceptionDeduper(base_gap_ms=10_000)
    assert d.should_accept(_snap(bundle="com.apple.Terminal"), now_ms=0)
    assert d.should_accept(_snap(bundle="com.microsoft.VSCode"), now_ms=5) is True


def test_user_state_and_focus_are_part_of_fingerprint() -> None:
    d = PerceptionDeduper(base_gap_ms=10_000)
    d.should_accept(_snap(user_state="idle"), now_ms=0)
    # user_state flip → fresh fingerprint, immediate accept.
    assert d.should_accept(_snap(user_state="coding"), now_ms=10) is True
    # focus class flip → fresh fingerprint, immediate accept.
    assert (
        d.should_accept(
            _snap(user_state="coding", focus=UserFocus.FOCUSED), now_ms=20
        )
        is True
    )


# ---------------------------------------------------------------------------
# Gap timing + widening
# ---------------------------------------------------------------------------


def test_same_fingerprint_passes_after_gap() -> None:
    d = PerceptionDeduper(base_gap_ms=200)
    d.should_accept(_snap(), now_ms=0)
    # 199 ms later — still inside the gap.
    assert d.should_accept(_snap(idle_ms=199), now_ms=199) is False
    # 200 ms later — at the boundary, should pass.
    assert d.should_accept(_snap(idle_ms=200), now_ms=200) is True


def test_widening_factor_doubles_the_gap() -> None:
    factor = {"value": 1.0}
    d = PerceptionDeduper(
        base_gap_ms=200,
        widening_factor_provider=lambda: factor["value"],
    )
    d.should_accept(_snap(), now_ms=0)
    # At factor 1.0, 250 ms passes through.
    assert d.should_accept(_snap(idle_ms=250), now_ms=250) is True

    # Bump the factor to 2.0 — gap is now 400 ms.
    factor["value"] = 2.0
    # 250 ms after the previous accept — still inside the widened gap.
    assert d.should_accept(_snap(idle_ms=500), now_ms=500) is False
    assert d.dropped_count == 1
    # 400 ms after the previous accept — passes.
    assert d.should_accept(_snap(idle_ms=650), now_ms=650) is True


def test_widening_factor_read_per_call() -> None:
    """Lowering the factor mid-stream restores tighter gating
    immediately, no rewiring needed."""
    factor = {"value": 2.0}
    d = PerceptionDeduper(
        base_gap_ms=100,
        widening_factor_provider=lambda: factor["value"],
    )
    d.should_accept(_snap(), now_ms=0)
    # At factor 2.0, gap = 200 — 150 ms is dropped.
    assert d.should_accept(_snap(idle_ms=150), now_ms=150) is False

    # Drop factor back to 1.0 — gap = 100. The same 150 ms is now
    # ≥ gap relative to the most-recent accept (at 0 ms), so it
    # should pass.
    factor["value"] = 1.0
    assert d.should_accept(_snap(idle_ms=150), now_ms=150) is True


def test_effective_gap_clamps_at_zero() -> None:
    """A widening factor < 1 must never make the gap negative."""
    d = PerceptionDeduper(
        base_gap_ms=100,
        widening_factor_provider=lambda: -1.0,
    )
    assert d.effective_gap_ms() == 0
    # With gap=0, every same-fingerprint tick passes (the strict
    # less-than gate is the only thing rejecting).
    d.should_accept(_snap(), now_ms=0)
    assert d.should_accept(_snap(idle_ms=10), now_ms=0) is True


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


def test_default_base_gap_constant_is_exposed() -> None:
    # Other modules import this for tuning / docs; pin it so
    # reflowing the default doesn't silently shift behaviour.
    assert DEFAULT_BASE_GAP_MS == 200


def test_dropped_count_is_cumulative() -> None:
    d = PerceptionDeduper(base_gap_ms=1_000_000)
    d.should_accept(_snap(), now_ms=0)
    for tick in range(5):
        d.should_accept(_snap(idle_ms=tick), now_ms=tick)
    assert d.dropped_count == 5


def test_reset_clears_latch_and_counter() -> None:
    d = PerceptionDeduper(base_gap_ms=10_000)
    d.should_accept(_snap(), now_ms=0)
    d.should_accept(_snap(idle_ms=10), now_ms=10)  # dropped
    assert d.dropped_count == 1

    d.reset()
    assert d.dropped_count == 0
    # After reset, the next tick is accepted unconditionally.
    assert d.should_accept(_snap(idle_ms=20), now_ms=20) is True
