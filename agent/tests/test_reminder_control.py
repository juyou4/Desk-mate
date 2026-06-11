"""Natural-language reminder creation tests."""

from __future__ import annotations

import pytest

from deskmate_agent.reminders import Reminder, ReminderStore
from deskmate_agent.skills import (
    parse_reminder_command,
    parse_reminder_request,
    reminder_control_composer,
    reminder_control_streaming_composer,
)


def test_parse_reminder_relative_minutes() -> None:
    request = parse_reminder_request("remind me to stretch in 10 minutes")

    assert request is not None
    assert request.text == "stretch"
    assert request.delay_ms == 10 * 60 * 1000
    assert request.display_delay == "10 minutes"


def test_parse_timer_defaults_text() -> None:
    request = parse_reminder_request("timer for 5 seconds")

    assert request is not None
    assert request.text == "Timer done"
    assert request.delay_ms == 5_000
    assert request.display_delay == "5 seconds"


def test_parse_chinese_relative_reminder() -> None:
    request = parse_reminder_request("10分钟后提醒我喝水")

    assert request is not None
    assert request.text == "喝水"
    assert request.delay_ms == 10 * 60 * 1000


def test_parse_chinese_countdown_timer() -> None:
    request = parse_reminder_request("帮我设置一个 3 分钟倒计时")

    assert request is not None
    assert request.text == "Timer done"
    assert request.delay_ms == 3 * 60 * 1000
    assert request.display_delay == "3 minutes"


def test_parse_rejects_missing_or_zero_time() -> None:
    assert parse_reminder_request("remind me to stretch") is None
    assert parse_reminder_request("remind me to stretch in 0 minutes") is None


def test_parse_reminder_management_commands() -> None:
    listed = parse_reminder_command("what reminders do I have?")
    cancelled = parse_reminder_command("cancel reminder r1")
    zh_cancelled = parse_reminder_command("取消提醒 r2")

    assert listed is not None
    assert listed.kind == "list_reminders"
    assert cancelled is not None
    assert cancelled.kind == "cancel_reminder"
    assert cancelled.reminder_id == "r1"
    assert zh_cancelled is not None
    assert zh_cancelled.kind == "cancel_reminder"
    assert zh_cancelled.reminder_id == "r2"


@pytest.mark.asyncio
async def test_reminder_control_composer_adds_pending_reminder() -> None:
    store = ReminderStore()
    compose = reminder_control_composer(
        reminder_store=store,
        clock=lambda: 1_000,
        id_factory=lambda: "r1",
    )

    reply = await compose("remind me to stretch in 2 minutes")

    assert reply == "Reminder set for 2 minutes: stretch."
    reminder = store.get("r1")
    assert reminder is not None
    assert reminder.text == "stretch"
    assert reminder.created_at_ms == 1_000
    assert reminder.due_at_ms == 121_000
    assert reminder.extras["source"] == "reminder_control"


@pytest.mark.asyncio
async def test_reminder_control_composer_lists_and_cancels_reminders() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r-soon",
            text="stretch",
            due_at_ms=31_000,
            created_at_ms=1_000,
        )
    )
    store.add(
        Reminder(
            reminder_id="r-later",
            text="drink water",
            due_at_ms=121_000,
            created_at_ms=1_000,
        )
    )
    compose = reminder_control_composer(
        reminder_store=store,
        clock=lambda: 1_000,
    )

    listed = await compose("list reminders")
    cancelled = await compose("cancel reminder r-soon")
    listed_after = await compose("what reminders do I have?")

    assert listed == (
        "Pending reminders:\n"
        "r-soon [due in 30 seconds]: stretch\n"
        "r-later [due in 2 minutes]: drink water"
    )
    assert cancelled == "Cancelled reminder r-soon: stretch."
    reminder = store.get("r-soon")
    assert reminder is not None
    assert reminder.status.value == "cancelled"
    assert listed_after == (
        "Pending reminders:\n"
        "r-later [due in 2 minutes]: drink water"
    )


@pytest.mark.asyncio
async def test_reminder_control_composer_falls_back_for_chat() -> None:
    async def fallback(text: str) -> str:
        return f"chat:{text}"

    compose = reminder_control_composer(fallback=fallback)

    assert await compose("hello") == "chat:hello"


@pytest.mark.asyncio
async def test_reminder_control_streaming_composer_adds_reminder() -> None:
    store = ReminderStore()
    compose = reminder_control_streaming_composer(
        reminder_store=store,
        clock=lambda: 1_000,
        id_factory=lambda: "r1",
    )

    chunks = [chunk async for chunk in compose("timer for 1 minute")]

    assert chunks == ["Reminder set for 1 minute: Timer done."]
    assert store.get("r1") is not None


@pytest.mark.asyncio
async def test_reminder_control_streaming_composer_cancels_reminder() -> None:
    store = ReminderStore()
    store.add(
        Reminder(
            reminder_id="r1",
            text="stretch",
            due_at_ms=61_000,
            created_at_ms=1_000,
        )
    )
    compose = reminder_control_streaming_composer(
        reminder_store=store,
        clock=lambda: 1_000,
    )

    chunks = [chunk async for chunk in compose("delete timer r1")]

    assert chunks == ["Cancelled reminder r1: stretch."]
    reminder = store.get("r1")
    assert reminder is not None
    assert reminder.status.value == "cancelled"


@pytest.mark.asyncio
async def test_reminder_control_streaming_composer_falls_back_for_chat() -> None:
    async def fallback(text: str):
        yield "chat:"
        yield text

    compose = reminder_control_streaming_composer(fallback=fallback)

    chunks = [chunk async for chunk in compose("hello")]

    assert chunks == ["chat:", "hello"]
