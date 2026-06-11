"""Tests for durable-task stale nudges."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from deskmate_agent.island_notifications import NotificationOutcome
from deskmate_agent.memory import DeskmateTaskStore
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.task_nudges import TaskNudgeWatcher


@dataclass
class FakeIslandNotifications:
    calls: list[dict[str, object]]

    async def show_notification(self, **kwargs: object) -> NotificationOutcome:
        self.calls.append(kwargs)
        return NotificationOutcome(emitted=True)


@pytest.mark.asyncio
async def test_task_nudge_picks_old_in_progress_before_open_and_cools_down(
    tmp_path,
) -> None:
    intents: list[CompanionIntent] = []
    island = FakeIslandNotifications([])

    async def sink(intent: CompanionIntent) -> None:
        intents.append(intent)

    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            task_id="task-open",
            title="Open task",
            created_at_ms=1_000,
        )
        await store.create(
            task_id="task-active",
            title="Active task",
            status="in_progress",
            created_at_ms=2_000,
        )
        await store.replace_steps(
            "task-active",
            [
                {"content": "Read stale task state", "status": "completed"},
                {
                    "content": "Show task step in nudge",
                    "status": "in_progress",
                    "active_form": "Showing task step in nudge",
                },
            ],
        )
        await store.create(
            task_id="task-done",
            title="Done task",
            status="done",
            created_at_ms=1_000,
        )
        watcher = TaskNudgeWatcher(
            store,
            sink,
            island_notifications=island,  # type: ignore[arg-type]
            stale_after_ms=1_000,
            cooldown_ms=10_000,
            id_factory=lambda: "nudge-1",
        )

        first = await watcher.process_once(now_ms=4_000)
        second = await watcher.process_once(now_ms=4_100)
        third = await watcher.process_once(now_ms=14_001)

    assert first is not None
    assert first.task_id == "task-active"
    assert second is not None
    assert second.task_id == "task-open"
    assert third is not None
    assert third.task_id == "task-active"
    assert [intent.kind for intent in intents] == [
        IntentKind.SHOW_PET_BUBBLE,
        IntentKind.SHOW_PET_BUBBLE,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    assert intents[0].payload["task_id"] == "task-active"
    assert intents[0].payload["bubble"]["text"] == (
        "Still working on: Active task - step: Showing task step in nudge"
    )
    assert intents[1].payload["task_id"] == "task-open"
    assert intents[1].payload["bubble"]["text"] == "Still on your list: Open task"
    assert island.calls[0]["detail"] == (
        "Still working on: Active task - step: Showing task step in nudge"
    )
    assert [call["activity_id"] for call in island.calls] == [
        "task-nudge-task-active",
        "task-nudge-task-open",
        "task-nudge-task-active",
    ]


@pytest.mark.asyncio
async def test_task_nudge_waits_for_stale_threshold_and_ignores_closed_tasks(
    tmp_path,
) -> None:
    intents: list[CompanionIntent] = []

    async def sink(intent: CompanionIntent) -> None:
        intents.append(intent)

    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            task_id="task-new",
            title="Fresh task",
            created_at_ms=10_000,
        )
        await store.create(
            task_id="task-cancelled",
            title="Cancelled task",
            status="cancelled",
            created_at_ms=1_000,
        )
        watcher = TaskNudgeWatcher(
            store,
            sink,
            stale_after_ms=5_000,
        )

        assert await watcher.process_once(now_ms=12_000) is None
        nudged = await watcher.process_once(now_ms=15_001)

    assert nudged is not None
    assert nudged.task_id == "task-new"
    assert len(intents) == 1


@pytest.mark.asyncio
async def test_task_nudge_allows_renudging_after_task_updates(tmp_path) -> None:
    intents: list[CompanionIntent] = []

    async def sink(intent: CompanionIntent) -> None:
        intents.append(intent)

    async with DeskmateTaskStore(tmp_path / "tasks.db") as store:
        await store.create(
            task_id="task-a",
            title="Task A",
            created_at_ms=1_000,
        )
        watcher = TaskNudgeWatcher(
            store,
            sink,
            stale_after_ms=1_000,
            cooldown_ms=100_000,
        )

        first = await watcher.process_once(now_ms=3_000)
        await store.update("task-a", title="Task A updated", updated_at_ms=4_000)
        second = await watcher.process_once(now_ms=5_001)

    assert first is not None
    assert first.title == "Task A"
    assert second is not None
    assert second.title == "Task A updated"
    assert len(intents) == 2
