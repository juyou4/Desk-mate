"""Tests for trace_id propagation (V10 L3 Instrumentation)."""

from __future__ import annotations

import asyncio

import pytest

from deskmate_agent.logging_setup import (
    bind_trace_id,
    get_trace_id,
    trace_scope,
)


def test_bind_and_get_trace_id() -> None:
    bind_trace_id("abc123")
    try:
        assert get_trace_id() == "abc123"
    finally:
        bind_trace_id(None)
    assert get_trace_id() is None


def test_trace_scope_restores_previous_value() -> None:
    bind_trace_id("outer")
    try:
        with trace_scope("inner"):
            assert get_trace_id() == "inner"
        assert get_trace_id() == "outer"
    finally:
        bind_trace_id(None)


@pytest.mark.asyncio
async def test_trace_id_propagates_across_async_tasks() -> None:
    """ContextVar copies with ``asyncio.create_task`` — each child inherits."""
    seen: dict[str, str | None] = {}

    async def child(key: str) -> None:
        seen[key] = get_trace_id()

    with trace_scope("parent-trace"):
        await asyncio.gather(
            asyncio.create_task(child("a")),
            asyncio.create_task(child("b")),
        )

    assert seen == {"a": "parent-trace", "b": "parent-trace"}
    assert get_trace_id() is None
