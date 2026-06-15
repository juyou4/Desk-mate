"""Tests for persistent Deskmate task memory."""

from __future__ import annotations

import pytest

from deskmate_agent.approvals import ApprovalDecision, ApprovalStore
from deskmate_agent.memory import (
    DeskmateTaskStore,
    format_deskmate_task,
    format_deskmate_task_step,
)
from deskmate_agent.memory.task_suggestions import (
    TASK_SUGGESTION_KIND,
    TaskSuggestion,
    create_task_suggestion_approval,
    resolve_task_suggestion,
)


@pytest.mark.asyncio
async def test_deskmate_task_store_round_trips_and_filters(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        first = await store.create(
            conversation_id="default",
            task_id="task-a",
            title="Polish island hover",
            notes="Check collapsed and expanded state.",
            created_at_ms=1_000,
        )
        second = await store.create(
            conversation_id="default",
            task_id="task-b",
            title="Wire LLM task tools",
            status="in_progress",
            created_at_ms=2_000,
        )
        await store.create(
            conversation_id="other",
            task_id="task-c",
            title="Other conversation task",
            created_at_ms=3_000,
        )

        active = await store.list("default", status="active", limit=10)
        in_progress = await store.list("default", status="in_progress", limit=10)
        matches = await store.search(
            "default",
            query="hover",
            status="active",
            limit=10,
        )

    assert first.status == "open"
    assert second.status == "in_progress"
    assert [task.task_id for task in active] == ["task-b", "task-a"]
    assert [task.task_id for task in in_progress] == ["task-b"]
    assert [task.task_id for task in matches] == ["task-a"]
    assert format_deskmate_task(matches[0]) == (
        "task-a [open]: Polish island hover - Check collapsed and expanded state."
    )


@pytest.mark.asyncio
async def test_deskmate_task_store_update_sets_completion_timestamp(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            conversation_id="default",
            task_id="task-a",
            title="Ship task ledger",
            created_at_ms=1_000,
        )

        done = await store.update(
            "task-a",
            conversation_id="default",
            status="done",
            notes="Verified.",
            updated_at_ms=2_000,
        )
        reopened = await store.update(
            "task-a",
            conversation_id="default",
            status="open",
            updated_at_ms=3_000,
        )
        missing = await store.update(
            "task-a",
            conversation_id="other",
            status="done",
        )

    assert done is not None
    assert done.status == "done"
    assert done.completed_at_ms == 2_000
    assert done.notes == "Verified."
    assert reopened is not None
    assert reopened.status == "open"
    assert reopened.completed_at_ms is None
    assert missing is None


@pytest.mark.asyncio
async def test_deskmate_task_store_persists_to_disk(tmp_path) -> None:
    db_path = tmp_path / "tasks.db"
    async with DeskmateTaskStore(db_path) as store:
        await store.create(
            conversation_id="default",
            task_id="task-a",
            title="Persistent task",
            created_at_ms=1_000,
        )

    async with DeskmateTaskStore(db_path) as reopened:
        task = await reopened.get("task-a")

    assert task is not None
    assert task.title == "Persistent task"


@pytest.mark.asyncio
async def test_deskmate_task_store_replaces_checklist_steps(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            conversation_id="default",
            task_id="task-a",
            title="Ship persistent checklist",
            created_at_ms=1_000,
        )

        steps = await store.replace_steps(
            "task-a",
            [
                {"content": "Read reference TodoTool", "status": "completed"},
                {
                    "content": "Implement step store",
                    "status": "in_progress",
                    "active_form": "Implementing step store",
                },
                {"content": "Wire LLM tool", "status": "pending"},
            ],
            conversation_id="default",
            updated_at_ms=2_000,
        )
        listed = await store.list_steps("task-a", conversation_id="default")
        replaced = await store.replace_steps(
            "task-a",
            [{"content": "Verify tests", "status": "done"}],
            conversation_id="default",
            updated_at_ms=3_000,
        )

    assert steps is not None
    assert [step.position for step in steps] == [1, 2, 3]
    assert steps[0].status == "completed"
    assert steps[0].completed_at_ms == 2_000
    assert steps[1].active_form == "Implementing step store"
    assert listed == steps
    assert replaced is not None
    assert len(replaced) == 1
    assert replaced[0].status == "completed"
    assert format_deskmate_task_step(replaced[0]) == (
        "1. [completed] Verify tests"
    )


@pytest.mark.asyncio
async def test_deskmate_task_store_validates_checklist_steps(tmp_path) -> None:
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            conversation_id="default",
            task_id="task-a",
            title="Validate checklist",
        )

        with pytest.raises(ValueError, match="limited to 20"):
            await store.replace_steps(
                "task-a",
                [{"content": f"Step {idx}", "status": "pending"} for idx in range(21)],
                conversation_id="default",
            )
        with pytest.raises(ValueError, match="step content is required"):
            await store.replace_steps(
                "task-a",
                [{"content": " ", "status": "pending"}],
                conversation_id="default",
            )
        with pytest.raises(ValueError, match="step status"):
            await store.replace_steps(
                "task-a",
                [{"content": "Bad status", "status": "blocked"}],
                conversation_id="default",
            )
        with pytest.raises(ValueError, match="only one"):
            await store.replace_steps(
                "task-a",
                [
                    {"content": "First", "status": "in_progress"},
                    {"content": "Second", "status": "in_progress"},
                ],
                conversation_id="default",
            )
        missing = await store.replace_steps(
            "task-a",
            [{"content": "Wrong conversation", "status": "pending"}],
            conversation_id="other",
        )
        listed = await store.list_steps("task-a", conversation_id="default")

    assert missing is None
    assert listed == []


@pytest.mark.asyncio
async def test_task_suggestion_requires_approval_before_writing(tmp_path) -> None:
    approvals = ApprovalStore()
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        approval = create_task_suggestion_approval(
            TaskSuggestion(
                title="Review island task lane",
                notes="Keep rows compact.",
                reason="Useful follow-up.",
            ),
            approval_store=approvals,
            now_ms=1_000,
            conversation_id="default",
            approval_id="task-suggestion",
        )

        assert await store.list(status="all", limit=10) == []
        assert approval.prompt == "Add task: Review island task lane?"
        assert approval.extras["kind"] == TASK_SUGGESTION_KIND
        assert approval.extras["task_reason"] == "Useful follow-up."

        approvals.resolve("task-suggestion", ApprovalDecision.ALLOW, 2_000)
        resolved = approvals.get("task-suggestion")
        assert resolved is not None
        message = await resolve_task_suggestion(
            resolved,
            task_store=store,
            clock=lambda: 3_000,
        )
        tasks = await store.list(status="all", limit=10)

    assert message is not None
    assert message.startswith("Task created:\ntask-")
    assert tasks[0].title == "Review island task lane"
    assert tasks[0].notes == "Keep rows compact."
    assert tasks[0].created_at_ms == 3_000


@pytest.mark.asyncio
async def test_denied_task_suggestion_does_not_write(tmp_path) -> None:
    approvals = ApprovalStore()
    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        create_task_suggestion_approval(
            TaskSuggestion(title="Optional cleanup"),
            approval_store=approvals,
            now_ms=1_000,
            approval_id="task-deny",
        )
        approvals.resolve("task-deny", ApprovalDecision.DENY, 2_000)
        resolved = approvals.get("task-deny")
        assert resolved is not None
        message = await resolve_task_suggestion(resolved, task_store=store)
        tasks = await store.list(status="all", limit=10)

    assert message == "Skipped task: Optional cleanup."
    assert tasks == []
