"""DegradationController tests (V10 Phase 9)."""

from __future__ import annotations

import pytest

from deskmate_agent.degradation import (
    LEVEL_CAMERA_OFF,
    LEVEL_FPS_DOWN,
    LEVEL_HIDE_HUD,
    LEVEL_ISLAND_OFF,
    LEVEL_PERCEPTION_WIDE,
    LEVEL_PROACTIVE_X2,
    MAX_LEVEL,
    DegradationController,
)


def test_default_level_is_normal() -> None:
    c = DegradationController()
    assert c.level == 0
    assert c.proactive_interval_multiplier() == 1.0
    assert c.force_threshold_engine() is False
    assert c.perception_widening_factor() == 1.0
    assert c.hide_hud() is False
    assert c.island_orderout() is False
    assert c.camera_off() is False


def test_initial_level_is_clamped() -> None:
    assert DegradationController(initial_level=-5).level == 0
    assert DegradationController(initial_level=999).level == MAX_LEVEL


def test_set_level_returns_true_only_on_change() -> None:
    c = DegradationController()
    assert c.set_level(2) is True
    assert c.set_level(2) is False  # same
    assert c.set_level(3) is True


def test_subscribers_receive_new_level() -> None:
    c = DegradationController()
    received: list[int] = []
    c.subscribe(received.append)
    c.set_level(LEVEL_HIDE_HUD)
    c.set_level(LEVEL_HIDE_HUD)  # dedup: no fire
    c.set_level(LEVEL_PROACTIVE_X2)
    assert received == [LEVEL_HIDE_HUD, LEVEL_PROACTIVE_X2]


def test_subscriber_unsub_stops_callbacks() -> None:
    c = DegradationController()
    received: list[int] = []
    unsub = c.subscribe(received.append)
    c.set_level(1)
    unsub()
    c.set_level(2)
    assert received == [1]


def test_subscriber_error_is_isolated() -> None:
    c = DegradationController()

    def bad(level: int) -> None:
        raise RuntimeError("boom")

    received: list[int] = []
    c.subscribe(bad)
    c.subscribe(received.append)
    c.set_level(1)
    # Second subscriber still fires despite the first raising.
    assert received == [1]


@pytest.mark.parametrize(
    ("level", "multiplier", "force_threshold"),
    [
        (0, 1.0, False),
        (LEVEL_FPS_DOWN, 1.0, False),
        (LEVEL_PROACTIVE_X2, 2.0, True),
        (LEVEL_PERCEPTION_WIDE, 2.0, True),
        (MAX_LEVEL, 2.0, True),
    ],
)
def test_proactive_policy_is_monotonic(
    level: int, multiplier: float, force_threshold: bool
) -> None:
    c = DegradationController(initial_level=level)
    assert c.proactive_interval_multiplier() == multiplier
    assert c.force_threshold_engine() is force_threshold


@pytest.mark.parametrize(
    ("level", "widen"),
    [(0, 1.0), (LEVEL_PERCEPTION_WIDE, 2.0), (MAX_LEVEL, 2.0)],
)
def test_perception_widening_is_monotonic(
    level: int, widen: float
) -> None:
    c = DegradationController(initial_level=level)
    assert c.perception_widening_factor() == widen


@pytest.mark.parametrize(
    ("level", "hide", "island_off", "camera_off"),
    [
        (0, False, False, False),
        (LEVEL_HIDE_HUD, True, False, False),
        (LEVEL_ISLAND_OFF, True, True, False),
        (LEVEL_CAMERA_OFF, True, True, True),
    ],
)
def test_ui_policies_are_monotonic(
    level: int, hide: bool, island_off: bool, camera_off: bool
) -> None:
    c = DegradationController(initial_level=level)
    assert c.hide_hud() is hide
    assert c.island_orderout() is island_off
    assert c.camera_off() is camera_off
