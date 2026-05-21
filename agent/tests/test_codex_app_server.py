"""Codex.app app-server bridge tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from deskmate_agent.agent_events import (
    PermissionRequested,
    QuestionAsked,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
)
from deskmate_agent.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerCoordinator,
    CodexThread,
    CodexThreadStatus,
    CodexThreadStatusType,
    CodexTurn,
    CodexTurnStatus,
    agent_event_from_codex_notification,
    parse_codex_notification,
)
from deskmate_agent.sessions import SessionPhase


def test_parse_thread_started_notification_to_session_started() -> None:
    notification = parse_codex_notification(
        {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": "t1",
                    "cwd": "/tmp/work",
                    "name": "Refactor",
                    "preview": "working on it",
                    "ephemeral": False,
                    "status": {"type": "active", "activeFlags": []},
                }
            },
        }
    )

    assert notification is not None
    event = agent_event_from_codex_notification(notification, ts_ms=1_000)

    assert isinstance(event, SessionStarted)
    assert event.session_id == "t1"
    assert event.title == "Codex · Refactor"
    assert event.cwd == "/tmp/work"
    assert event.jump_url == "codex://threads/t1"
    assert event.phase is SessionPhase.RUNNING


@pytest.mark.parametrize(
    ("flags", "expected_type"),
    [
        (["waitingOnApproval"], PermissionRequested),
        (["waitingOnUserInput"], QuestionAsked),
        ([], SessionActivityUpdated),
    ],
)
def test_thread_status_changed_maps_active_flags(flags, expected_type) -> None:
    notification = parse_codex_notification(
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "t1",
                "status": {"type": "active", "activeFlags": flags},
            },
        }
    )

    assert notification is not None
    event = agent_event_from_codex_notification(notification, ts_ms=2_000)

    assert isinstance(event, expected_type)
    assert event.session_id == "t1"


def test_turn_completed_failed_maps_to_failed_session_completed() -> None:
    notification = parse_codex_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "t1",
                "turn": {"id": "turn1", "status": "failed"},
            },
        }
    )

    assert notification is not None
    event = agent_event_from_codex_notification(notification, ts_ms=3_000)

    assert isinstance(event, SessionCompleted)
    assert event.failed
    assert event.summary == "Codex turn failed."


class FakeCodexClient:
    def __init__(self, _path: Path, handler):
        self.handler = handler
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def list_loaded_threads(self):
        return [
            CodexThread(
                id="loaded",
                cwd="/tmp/work",
                name="Loaded",
                preview="loaded preview",
                status=CodexThreadStatus(CodexThreadStatusType.ACTIVE),
            )
        ]


@pytest.mark.asyncio
async def test_coordinator_syncs_loaded_threads() -> None:
    events = []

    async def handle(event):
        events.append(event)

    coordinator = CodexAppServerCoordinator(
        event_handler=handle,
        codex_path_provider=lambda: Path("/tmp/codex"),
        client_factory=lambda path, handler: FakeCodexClient(path, handler),  # type: ignore[arg-type]
        clock=lambda: 12_000,
    )

    await coordinator.start()
    await asyncio.sleep(0)
    await coordinator.stop()

    assert len(events) == 1
    assert isinstance(events[0], SessionStarted)
    assert events[0].session_id == "loaded"


def test_client_is_importable() -> None:
    assert CodexAppServerClient(Path("/tmp/codex")) is not None
    assert CodexTurn("turn", CodexTurnStatus.COMPLETED).status is CodexTurnStatus.COMPLETED
