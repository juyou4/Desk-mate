"""Tests for persistent chat transcript memory."""

from __future__ import annotations

import pytest

from deskmate_agent.memory import ChatMemory, Message


@pytest.mark.asyncio
async def test_chat_memory_journal_mode_is_wal(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        mode = await mem.pragma("journal_mode")
        assert str(mode).lower() == "wal"


@pytest.mark.asyncio
async def test_chat_memory_round_trips_messages_with_tool_data(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="remind me"),
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "deskmate_schedule_reminder",
                                "arguments": "{\"text\":\"stretch\",\"delay_ms\":60000}",
                            },
                        }
                    ],
                ),
                Message(
                    role="tool",
                    tool_call_id="call-1",
                    content="Reminder scheduled for 1 minute: stretch.",
                ),
            ],
        )

        recent = await mem.recent("default", limit=10)

    assert [m.role for m in recent] == ["user", "assistant", "tool"]
    assert recent[1].tool_calls is not None
    assert recent[1].tool_calls[0]["function"]["name"] == "deskmate_schedule_reminder"
    assert recent[2].tool_call_id == "call-1"


@pytest.mark.asyncio
async def test_chat_memory_maintains_persistent_summary(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="I am planning a Cursor workflow"),
                Message(role="assistant", content="We can use Codex and reminders."),
                Message(
                    role="assistant",
                    tool_calls=[
                        {
                            "type": "function",
                            "function": {"name": "deskmate_schedule_reminder"},
                        }
                    ],
                ),
                Message(
                    role="tool",
                    tool_call_id="call-1",
                    content="Reminder scheduled for 1 minute: stretch.",
                ),
            ],
        )

        summary = await mem.get_summary("default")

    assert summary is not None
    assert summary.conversation_id == "default"
    assert summary.message_count == 4
    assert "- User: I am planning a Cursor workflow" in summary.summary
    assert "- Assistant: We can use Codex and reminders." in summary.summary
    assert "- Assistant: called deskmate_schedule_reminder" in summary.summary
    assert "- Tool: Reminder scheduled for 1 minute: stretch." in summary.summary


@pytest.mark.asyncio
async def test_chat_memory_recent_is_scoped_and_limited(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "a",
            [Message(role="user", content=str(i)) for i in range(4)],
        )
        await mem.append("b", Message(role="user", content="other"))

        recent = await mem.recent("a", limit=2)

    assert [m.content for m in recent] == ["2", "3"]


@pytest.mark.asyncio
async def test_chat_memory_search_is_scoped_limited_and_ordered(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="we talked about Cursor"),
                Message(role="assistant", content="Cursor is your IDE"),
                Message(role="user", content="unrelated"),
            ],
        )
        await mem.append("other", Message(role="user", content="Cursor in other chat"))

        matches = await mem.search("default", query="Cursor", limit=1)

    assert [m.content for m in matches] == ["Cursor is your IDE"]


@pytest.mark.asyncio
async def test_chat_memory_search_escapes_like_wildcards(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append_many(
            "default",
            [
                Message(role="user", content="100 percent"),
                Message(role="user", content="100% literal"),
            ],
        )

        matches = await mem.search("default", query="100%", limit=5)

    assert [m.content for m in matches] == ["100% literal"]


@pytest.mark.asyncio
async def test_chat_memory_clear_removes_conversation(tmp_path) -> None:
    async with ChatMemory(tmp_path / "chat.db") as mem:
        await mem.append("default", Message(role="user", content="hi"))
        await mem.clear("default")

        assert await mem.recent("default") == []
        assert await mem.get_summary("default") is None
