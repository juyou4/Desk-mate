"""Tests for persistent Deskmate tool-action audit log."""

from __future__ import annotations

from typing import cast

import pytest

from deskmate_agent.memory import (
    ToolActionLog,
    ToolActionRecord,
    ToolActionStatus,
    ToolTaskRecord,
    format_tool_action_summary,
    format_tool_lesson,
    format_tool_task_summary,
    sanitize_tool_arguments,
    tool_action_summary,
)


def _record(
    idx: int,
    *,
    conversation_id: str = "default",
    tool_name: str = "deskmate_schedule_reminder",
    result: str = "Reminder scheduled for 1 minute: stretch.",
    status: str = "completed",
    arguments: object | None = None,
    summary: dict[str, object] | None = None,
    task_id: str | None = None,
) -> ToolActionRecord:
    return ToolActionRecord(
        conversation_id=conversation_id,
        tool_call_id=f"call-{idx}",
        task_id=task_id,
        tool_name=tool_name,
        arguments=arguments if arguments is not None else {"text": "stretch"},
        result=result,
        status=cast(ToolActionStatus, status),
        started_at_ms=1_000 + idx,
        completed_at_ms=1_100 + idx,
        summary=summary,
    )


def test_sanitize_tool_arguments_redacts_sensitive_fields() -> None:
    sanitized = sanitize_tool_arguments(
        {
            "command": "open Terminal",
            "api_key": "sk-secret-value",
            "nested": {
                "password": "hunter2",
                "memory_value": "Cursor",
            },
            "items": [{"token": "tok_123"}, {"path": "/tmp/project"}],
        }
    )

    assert sanitized == {
        "command": "open Terminal",
        "api_key": "[redacted] len=15",
        "nested": {
            "password": "[redacted] len=7",
            "memory_value": "Cursor",
        },
        "items": [{"token": "[redacted] len=7"}, {"path": "/tmp/project"}],
    }


@pytest.mark.asyncio
async def test_tool_action_log_round_trips_recent_and_filters(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.append(_record(1, summary=None))
        await log.append(
            _record(
                2,
                tool_name="deskmate_computer_action",
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                task_id="task-1",
            )
        )
        await log.append(
            _record(
                3,
                conversation_id="other",
                result="Reminder scheduled for 5 minutes: water.",
            )
        )

        recent = await log.recent("default", limit=10)
        failed = await log.recent("default", status="failed", limit=10)
        task = await log.recent("default", task_id="task-1", limit=10)
        computer = await log.recent(
            "default",
            tool_name="deskmate_computer_action",
            limit=10,
        )

    assert [record.tool_call_id for record in recent] == ["call-1", "call-2"]
    assert recent[0].row_id is not None
    assert recent[0].arguments == {"text": "stretch"}
    assert recent[1].task_id == "task-1"
    assert [record.tool_call_id for record in failed] == ["call-2"]
    assert [record.tool_call_id for record in task] == ["call-2"]
    assert [record.tool_call_id for record in computer] == ["call-2"]
    assert recent[0].summary == {
        "action": "deskmate_schedule_reminder",
        "target": "stretch",
        "outcome": "Reminder scheduled for 1 minute: stretch.",
        "needs_user": False,
    }
    assert tool_action_summary(recent[1]) == {
        "action": "deskmate_computer_action",
        "target": "stretch",
        "outcome": "Tool error: command was not recognized or allowed.",
        "needs_user": True,
    }


@pytest.mark.asyncio
async def test_tool_action_log_sanitizes_arguments_before_persisting(
    tmp_path,
) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.append(
            _record(
                1,
                arguments={
                    "command": "copy launch code",
                    "clipboard_text": "launch code",
                    "token": "tok_private",
                    "memory_value": "Cursor",
                },
                tool_name="deskmate_computer_action",
                result="Updated the clipboard.",
            )
        )

        recent = await log.recent("default", limit=10)
        secret_matches = await log.search("default", query="tok_private", limit=10)
        token_matches = await log.search("default", query="redacted", limit=10)
        memory_matches = await log.search("default", query="Cursor", limit=10)

    assert recent[0].arguments == {
        "command": "copy launch code",
        "clipboard_text": "[redacted] len=11",
        "token": "[redacted] len=11",
        "memory_value": "Cursor",
    }
    assert secret_matches == []
    assert [record.tool_call_id for record in token_matches] == ["call-1"]
    assert [record.tool_call_id for record in memory_matches] == ["call-1"]


@pytest.mark.asyncio
async def test_tool_action_log_searches_structured_summary(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.append(
            _record(
                1,
                summary={
                    "action": "deskmate_schedule_reminder",
                    "target": "hydration",
                    "outcome": "Reminder scheduled.",
                    "needs_user": False,
                },
            )
        )

        matches = await log.search("default", query="hydration", limit=10)

    assert [record.tool_call_id for record in matches] == ["call-1"]
    assert format_tool_action_summary(matches[0]) == (
        "action=deskmate_schedule_reminder; status=completed; "
        "target=hydration; outcome=Reminder scheduled.; needs_user=false"
    )


@pytest.mark.asyncio
async def test_tool_action_log_persists_and_searches_tool_lessons(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.append(
            _record(
                1,
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                task_id="task-open-terminal",
            )
        )
        await log.append(
            _record(
                2,
                tool_name="deskmate_computer_action",
                arguments={"command": "open Terminal"},
                result="Tool error: command was not recognized or allowed.",
                status="failed",
                task_id="task-open-terminal",
            )
        )
        await log.append(
            _record(
                3,
                tool_name="deskmate_schedule_reminder",
                arguments={"text": "stretch"},
                result="Reminder scheduled for 1 minute: stretch.",
                status="completed",
            )
        )
        await log.append(
            _record(
                4,
                tool_name="deskmate_schedule_reminder",
                arguments={"text": "stretch"},
                result="Reminder scheduled for 1 minute: stretch.",
                status="duplicate",
            )
        )

        lessons = await log.recent_lessons("default", limit=10)
        terminal = await log.search_lessons("default", query="Terminal", limit=10)

    assert len(lessons) == 2
    assert [lesson.tool_name for lesson in lessons] == [
        "deskmate_computer_action",
        "deskmate_schedule_reminder",
    ]
    assert lessons[0].target == "open Terminal"
    assert lessons[0].status == "failed"
    assert lessons[0].needs_user is True
    assert lessons[0].seen_count == 2
    assert lessons[0].task_id == "task-open-terminal"
    assert format_tool_lesson(lessons[0]) == (
        "tool=deskmate_computer_action; status=failed; target=open Terminal; "
        "outcome=Tool error: command was not recognized or allowed.; "
        "needs_user=true; seen=2"
    )
    assert [lesson.lesson_key for lesson in terminal] == [lessons[0].lesson_key]
    assert "Requires user action." in terminal[0].lesson


@pytest.mark.asyncio
async def test_tool_action_log_migrates_legacy_schema(tmp_path) -> None:
    db_path = tmp_path / "tool_actions.db"
    import aiosqlite

    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE tool_actions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id   TEXT NOT NULL,
                tool_call_id      TEXT NOT NULL,
                tool_name         TEXT NOT NULL,
                arguments_json    TEXT,
                result            TEXT NOT NULL,
                status            TEXT NOT NULL,
                started_at_ms     INTEGER NOT NULL,
                completed_at_ms   INTEGER NOT NULL
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO tool_actions
                (
                    conversation_id,
                    tool_call_id,
                    tool_name,
                    arguments_json,
                    result,
                    status,
                    started_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "legacy-call",
                "deskmate_computer_action",
                '{"command":"open Terminal"}',
                "Opened Terminal.",
                "completed",
                1_000,
                1_010,
            ),
        )
        await conn.commit()

    async with ToolActionLog(db_path) as log:
        await log.append(_record(2))
        recent = await log.recent("default", limit=10)

    assert [record.tool_call_id for record in recent] == ["legacy-call", "call-2"]
    assert recent[0].summary is None
    assert tool_action_summary(recent[0]) == {
        "action": "deskmate_computer_action",
        "target": "open Terminal",
        "outcome": "Opened Terminal.",
        "needs_user": False,
    }
    assert recent[1].summary is not None


@pytest.mark.asyncio
async def test_tool_action_log_round_trips_tool_tasks(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-1",
                conversation_id="default",
                user_text="please remind me",
                status="running",
                summary="Starting tools",
                action_count=0,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_000,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-1",
                conversation_id="default",
                user_text="please remind me",
                status="completed",
                summary="Reminder scheduled.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_100,
                completed_at_ms=1_100,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-other",
                conversation_id="other",
                user_text="open terminal",
                status="failed",
                summary="Tool error.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )

        task = await log.get_task("task-1")
        recent = await log.recent_tasks("default", limit=10)

    assert task is not None
    assert task.status == "completed"
    assert task.action_count == 1
    assert task.completed_at_ms == 1_100
    assert [record.task_id for record in recent] == ["task-1"]
    assert format_tool_task_summary(task) == (
        "task=task-1; status=completed; actions=1; summary=Reminder scheduled."
    )


@pytest.mark.asyncio
async def test_tool_action_log_marks_stale_running_tasks_failed(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.upsert_task(
            ToolTaskRecord(
                task_id="old-running",
                conversation_id="default",
                user_text="open terminal",
                status="running",
                summary="Opening Terminal",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_100,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="fresh-running",
                conversation_id="default",
                user_text="set reminder",
                status="running",
                summary="Scheduling reminder",
                action_count=0,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=9_000,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="completed",
                conversation_id="default",
                user_text="done",
                status="completed",
                summary="Done.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=3_000,
                updated_at_ms=3_100,
                completed_at_ms=3_100,
            )
        )

        count = await log.mark_stale_running_tasks_failed(
            cutoff_updated_at_ms=5_000,
            completed_at_ms=10_000,
            summary="Interrupted.",
        )
        old = await log.get_task("old-running")
        fresh = await log.get_task("fresh-running")
        completed = await log.get_task("completed")

    assert count == 1
    assert old is not None
    assert old.status == "failed"
    assert old.summary == "Interrupted."
    assert old.failed_count == 1
    assert old.completed_at_ms == 10_000
    assert fresh is not None
    assert fresh.status == "running"
    assert completed is not None
    assert completed.status == "completed"


@pytest.mark.asyncio
async def test_tool_action_log_searches_tool_tasks(tmp_path) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-open-terminal",
                conversation_id="default",
                user_text="open terminal",
                status="completed",
                summary="Opened Terminal.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=1_000,
                updated_at_ms=1_100,
                completed_at_ms=1_100,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-literal-percent",
                conversation_id="default",
                user_text="remember 100% literal",
                status="failed",
                summary="Tool error.",
                action_count=1,
                failed_count=1,
                duplicate_count=0,
                started_at_ms=2_000,
                updated_at_ms=2_100,
                completed_at_ms=2_100,
            )
        )
        await log.upsert_task(
            ToolTaskRecord(
                task_id="task-other",
                conversation_id="other",
                user_text="open terminal in other conversation",
                status="completed",
                summary="Opened Terminal elsewhere.",
                action_count=1,
                failed_count=0,
                duplicate_count=0,
                started_at_ms=3_000,
                updated_at_ms=3_100,
                completed_at_ms=3_100,
            )
        )

        terminal = await log.search_tasks("default", query="terminal", limit=10)
        literal = await log.search_tasks("default", query="100%", limit=10)

    assert [task.task_id for task in terminal] == ["task-open-terminal"]
    assert [task.task_id for task in literal] == ["task-literal-percent"]


@pytest.mark.asyncio
async def test_tool_action_log_limits_recent_to_newest_then_returns_chronological(
    tmp_path,
) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        for idx in range(5):
            await log.append(_record(idx))

        recent = await log.recent("default", limit=2)

    assert [record.tool_call_id for record in recent] == ["call-3", "call-4"]


@pytest.mark.asyncio
async def test_tool_action_log_search_is_scoped_and_escapes_like_wildcards(
    tmp_path,
) -> None:
    async with ToolActionLog(tmp_path / "tool_actions.db") as log:
        await log.append(_record(1, result="100 percent complete"))
        await log.append(_record(2, result="100% literal complete"))
        await log.append(
            _record(
                3,
                conversation_id="other",
                result="100% literal in other conversation",
            )
        )

        matches = await log.search("default", query="100%", limit=10)

    assert [record.tool_call_id for record in matches] == ["call-2"]


@pytest.mark.asyncio
async def test_tool_action_log_persists_across_reopen(tmp_path) -> None:
    db_path = tmp_path / "tool_actions.db"
    async with ToolActionLog(db_path) as log:
        await log.append(
            _record(
                1,
                arguments={"command": "open Terminal"},
                tool_name="deskmate_computer_action",
                result="Approval required: open Terminal.",
            )
        )

    async with ToolActionLog(db_path) as reopened:
        recent = await reopened.recent("default", limit=10)

    assert len(recent) == 1
    assert recent[0].tool_name == "deskmate_computer_action"
    assert recent[0].arguments == {"command": "open Terminal"}


@pytest.mark.asyncio
async def test_tool_action_log_rejects_use_before_open(tmp_path) -> None:
    log = ToolActionLog(tmp_path / "tool_actions.db")

    with pytest.raises(RuntimeError):
        await log.recent("default")
