"""NudgeSelector tests (V10 Phase 16-i / 16-ii)."""

from __future__ import annotations

import pytest

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.proactive import NudgeSelector
from deskmate_agent.proactive.nudges import (
    LONG_DAY_CODING_MS,
    LONG_IDLE_SECONDS,
)


def _ctx(
    *, idle_ms: int = 600_000, coding_today_ms: int = 0
) -> ProactiveContext:
    return ProactiveContext(
        perception=PerceptionSnapshot(idle_ms=idle_ms),
        coding_today_ms=coding_today_ms,
    )


@pytest.mark.asyncio
async def test_selects_messages_in_rotation() -> None:
    selector = NudgeSelector(messages=("a", "b", "c"))
    assert await selector.select(_ctx()) == "a"
    assert await selector.select(_ctx()) == "b"
    assert await selector.select(_ctx()) == "c"
    # Wraps back to the first message.
    assert await selector.select(_ctx()) == "a"


@pytest.mark.asyncio
async def test_empty_pool_returns_none() -> None:
    selector = NudgeSelector(messages=())
    assert await selector.select(_ctx()) is None


@pytest.mark.asyncio
async def test_whitespace_messages_are_dropped() -> None:
    selector = NudgeSelector(messages=("  ", "real"))
    assert await selector.select(_ctx()) == "real"
    # Second call wraps through the single surviving entry.
    assert await selector.select(_ctx()) == "real"


@pytest.mark.asyncio
async def test_composer_output_preferred_over_rotation() -> None:
    async def composer(ctx: ProactiveContext) -> str | None:
        return f"composed for idle={ctx.idle_seconds}"

    selector = NudgeSelector(messages=("fallback",), composer=composer)
    assert (
        await selector.select(_ctx())
        == "composed for idle=600"
    )


@pytest.mark.asyncio
async def test_composer_failure_falls_back_to_rotation() -> None:
    async def composer(ctx: ProactiveContext) -> str | None:
        raise RuntimeError("boom")

    selector = NudgeSelector(messages=("fallback",), composer=composer)
    assert await selector.select(_ctx()) == "fallback"


@pytest.mark.asyncio
async def test_composer_returning_empty_falls_back() -> None:
    async def composer(ctx: ProactiveContext) -> str | None:
        return "   \n"

    selector = NudgeSelector(messages=("fallback",), composer=composer)
    assert await selector.select(_ctx()) == "fallback"


@pytest.mark.asyncio
async def test_rewind_resets_rotation_index() -> None:
    selector = NudgeSelector(messages=("a", "b"))
    await selector.select(_ctx())
    await selector.select(_ctx())
    selector.rewind()
    assert await selector.select(_ctx()) == "a"


# ---------------------------------------------------------------------------
# Phase 16-ii: context-aware buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucketed_pool_picks_long_day_after_threshold() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("default-1",),
            "long_day": ("rest-1", "rest-2"),
            "long_idle": ("idle-1",),
        }
    )
    ctx = _ctx(idle_ms=60_000, coding_today_ms=LONG_DAY_CODING_MS)
    assert selector.tag_for(ctx) == "long_day"
    assert await selector.select(ctx) == "rest-1"
    assert await selector.select(ctx) == "rest-2"
    assert await selector.select(ctx) == "rest-1"  # rotation wraps


@pytest.mark.asyncio
async def test_bucketed_pool_picks_long_idle_after_threshold() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("default-1",),
            "long_idle": ("idle-1", "idle-2"),
        }
    )
    ctx = _ctx(
        idle_ms=(LONG_IDLE_SECONDS + 1) * 1000, coding_today_ms=0
    )
    assert selector.tag_for(ctx) == "long_idle"
    assert await selector.select(ctx) == "idle-1"


@pytest.mark.asyncio
async def test_long_day_wins_over_long_idle() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("default-1",),
            "long_day": ("rest-1",),
            "long_idle": ("idle-1",),
        }
    )
    ctx = _ctx(
        idle_ms=(LONG_IDLE_SECONDS + 1) * 1000,
        coding_today_ms=LONG_DAY_CODING_MS,
    )
    assert selector.tag_for(ctx) == "long_day"
    assert await selector.select(ctx) == "rest-1"


@pytest.mark.asyncio
async def test_default_bucket_picked_when_thresholds_unmet() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("just-a-ping",),
            "long_day": ("rest-1",),
            "long_idle": ("idle-1",),
        }
    )
    ctx = _ctx(idle_ms=60_000, coding_today_ms=30_000)
    assert selector.tag_for(ctx) == "default"
    assert await selector.select(ctx) == "just-a-ping"


@pytest.mark.asyncio
async def test_empty_preferred_bucket_falls_back_to_default() -> None:
    """When a tag resolves but its pool is empty, we cascade to
    ``default`` rather than returning nothing."""
    selector = NudgeSelector(
        pools={
            "default": ("fallback-1",),
            "long_day": (),
        }
    )
    ctx = _ctx(idle_ms=0, coding_today_ms=LONG_DAY_CODING_MS)
    assert selector.tag_for(ctx) == "default"  # cascaded
    assert await selector.select(ctx) == "fallback-1"


@pytest.mark.asyncio
async def test_per_pool_rotation_is_independent() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("d-1", "d-2"),
            "long_day": ("r-1", "r-2"),
        }
    )
    default_ctx = _ctx(idle_ms=0, coding_today_ms=0)
    long_ctx = _ctx(idle_ms=0, coding_today_ms=LONG_DAY_CODING_MS)
    assert await selector.select(default_ctx) == "d-1"
    assert await selector.select(long_ctx) == "r-1"
    assert await selector.select(default_ctx) == "d-2"
    assert await selector.select(long_ctx) == "r-2"


@pytest.mark.asyncio
async def test_custom_thresholds_honoured() -> None:
    selector = NudgeSelector(
        pools={
            "default": ("default",),
            "long_day": ("rest",),
        },
        long_day_coding_ms=10_000,
    )
    # 10 seconds is the new bar; 9.9 seconds stays default.
    assert selector.tag_for(_ctx(coding_today_ms=9_900)) == "default"
    assert selector.tag_for(_ctx(coding_today_ms=10_000)) == "long_day"
