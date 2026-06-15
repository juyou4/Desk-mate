"""Hook event v1 ingestion + watcher tests."""

from __future__ import annotations

import io

import pytest

from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.cli import main
from deskmate_agent.hooks import (
    HookEventConsumer,
    HookEventWatcher,
    normalize_hook_event,
    write_hook_event,
)
from deskmate_agent.sessions import SessionPhase, SessionStore


def test_codex_hook_normalizes_unknown_payload_and_preserves_raw() -> None:
    event = normalize_hook_event(
        {"event": "mystery", "future": {"shape": "kept"}},
        source="codex",
    )

    assert event.source == "codex"
    assert event.session_id.startswith("codex-")
    assert event.raw["future"] == {"shape": "kept"}
    assert event.phase is SessionPhase.RUNNING


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"event": "agent.thinking"}, SessionPhase.THINKING),
        ({"event": "tool.start", "tool": "Edit"}, SessionPhase.EDITING),
        ({"event": "tool.start", "tool": "Bash", "command": "pytest"}, SessionPhase.TESTING),
        ({"event": "tool.start", "tool": "Bash"}, SessionPhase.RUNNING_TOOL),
        ({"event": "tool.end", "tool": "Bash"}, SessionPhase.COMPLETED),
        ({"hook_event_name": "PostToolUse", "tool_name": "Bash"}, SessionPhase.COMPLETED),
        ({"event": "session.completed"}, SessionPhase.COMPLETED),
        ({"event": "tool.failed"}, SessionPhase.FAILED),
    ],
)
def test_hook_normalizes_fine_grained_phase(payload, expected) -> None:
    event = normalize_hook_event({"session_id": "s1", **payload}, source="codex")
    assert event.phase is expected


def test_codex_hook_normalizes_real_hook_event_name_and_tool_input() -> None:
    event = normalize_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "s1",
            "cwd": "/tmp/work",
            "tool_name": "shell",
            "tool_input": {"command": "pytest"},
        },
        source="codex",
    )

    assert event.title == "Codex · work"
    assert event.phase is SessionPhase.WAITING_FOR_APPROVAL
    assert "pytest" in event.summary
    assert event.approval_id == "s1-approval"


def test_claude_permission_request_normalizes_to_approval() -> None:
    event = normalize_hook_event(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "c1",
            "cwd": "/tmp/work",
            "prompt": "Allow Bash?",
        },
        source="claude",
    )

    assert event.source == "claude"
    assert event.phase is SessionPhase.WAITING_FOR_APPROVAL
    assert event.approval_id == "c1-approval"
    assert event.title == "Claude · work"


def test_cursor_file_edit_normalizes_to_editing() -> None:
    event = normalize_hook_event(
        {
            "hook_event_name": "afterFileEdit",
            "conversation_id": "cur1",
            "workspace_roots": ["/tmp/work"],
            "file_path": "/tmp/work/app.py",
        },
        source="cursor",
    )

    assert event.session_id == "cur1"
    assert event.title == "Cursor · work"
    assert event.phase is SessionPhase.EDITING


def test_hook_consumer_upserts_session_and_approval() -> None:
    sessions = SessionStore()
    approvals = ApprovalStore()
    consumer = HookEventConsumer(session_store=sessions, approval_store=approvals)
    event = normalize_hook_event(
        {
            "session_id": "s1",
            "event": "permission.requested",
            "title": "Codex demo",
            "prompt": "Allow file write?",
            "cwd": "/tmp",
            "jump_url": "codex://session/s1",
        },
        source="codex",
    )

    consumer.handle(event)

    got = sessions.get("s1")
    assert got is not None
    assert got.title == "Codex demo"
    assert got.phase is SessionPhase.WAITING_FOR_APPROVAL
    assert got.cwd == "/tmp"
    assert got.jump_url == "codex://session/s1"
    assert got.source == "codex"
    assert got.kind == "hook_session"
    pending = approvals.list_pending()
    assert len(pending) == 1
    assert pending[0].prompt == "Allow file write?"


@pytest.mark.asyncio
async def test_hook_watcher_consumes_event_file(tmp_path) -> None:
    sessions = SessionStore()
    approvals = ApprovalStore()
    consumer = HookEventConsumer(session_store=sessions, approval_store=approvals)
    event = normalize_hook_event(
        {"session_id": "s1", "event": "session.started", "title": "Codex demo"},
        source="codex",
    )
    path = write_hook_event(event, queue_dir=tmp_path)

    async def handle(item):
        consumer.handle(item)

    watcher = HookEventWatcher(tmp_path, handle)
    count = await watcher.drain_once()

    assert count == 1
    assert not path.exists()
    assert sessions.get("s1") is not None


def test_cli_hook_ingest_writes_event_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"session_id":"s1","event":"session.started","title":"Codex demo"}'),
    )

    code = main(["hook", "ingest", "--source", "codex", "--queue-dir", str(tmp_path)])

    assert code == 0
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert '"session_id":"s1"' in files[0].read_text(encoding="utf-8")
