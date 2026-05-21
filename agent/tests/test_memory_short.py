"""Tests for the short-term in-memory deque (V10 L3-B6)."""

from __future__ import annotations

import pytest

from deskmate_agent.memory import Message, ShortTermMemory


def _msg(text: str, role: str = "user") -> Message:
    return Message(role=role, content=text)  # type: ignore[arg-type]


def test_append_and_len() -> None:
    mem = ShortTermMemory(max_messages=5)
    mem.append(_msg("a"))
    mem.append(_msg("b"))
    assert len(mem) == 2
    assert mem.max_messages == 5


def test_rolling_window_drops_oldest() -> None:
    mem = ShortTermMemory(max_messages=3)
    for i in range(5):
        mem.append(_msg(str(i)))
    contents = [m.content for m in mem.recent()]
    assert contents == ["2", "3", "4"]


def test_recent_with_limit_returns_tail() -> None:
    mem = ShortTermMemory(max_messages=10)
    mem.extend(_msg(str(i)) for i in range(5))
    contents = [m.content for m in mem.recent(limit=2)]
    assert contents == ["3", "4"]


def test_recent_with_limit_gte_len_returns_all() -> None:
    mem = ShortTermMemory(max_messages=10)
    mem.extend(_msg(str(i)) for i in range(3))
    assert len(mem.recent(limit=50)) == 3


def test_clear_resets_buffer() -> None:
    mem = ShortTermMemory(max_messages=5)
    mem.append(_msg("a"))
    mem.clear()
    assert len(mem) == 0
    assert mem.recent() == []


def test_zero_max_rejected() -> None:
    with pytest.raises(ValueError):
        ShortTermMemory(max_messages=0)
