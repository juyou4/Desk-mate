"""AgentEvent reducer tests."""

from __future__ import annotations

from deskmate_agent.agent_events import (
    AgentEventReducer,
    PermissionRequested,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
    event_from_hook,
)
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.hooks import normalize_hook_event
from deskmate_agent.protocol.state import Priority
from deskmate_agent.sessions import SessionPhase, SessionStore


def _reducer() -> tuple[AgentEventReducer, SessionStore, ApprovalStore]:
    sessions = SessionStore()
    approvals = ApprovalStore()
    return AgentEventReducer(session_store=sessions, approval_store=approvals), sessions, approvals


def test_reducer_starts_and_updates_session() -> None:
    reducer, sessions, _approvals = _reducer()

    reducer.apply(
        SessionStarted(
            session_id="s1",
            source="codex",
            ts_ms=1_000,
            title="Codex",
            summary="started",
        )
    )
    reducer.apply(
        SessionActivityUpdated(
            session_id="s1",
            source="codex",
            ts_ms=2_000,
            summary="editing",
            phase=SessionPhase.EDITING,
        )
    )

    got = sessions.get("s1")
    assert got is not None
    assert got.title == "Codex"
    assert got.summary == "editing"
    assert got.phase is SessionPhase.EDITING
    assert got.priority is Priority.P1
    assert got.created_at_ms == 1_000
    assert got.updated_at_ms == 2_000
    assert got.kind == "hook_session"


def test_event_from_hook_preserves_tool_display_extras() -> None:
    reducer, sessions, _approvals = _reducer()
    event = event_from_hook(
        normalize_hook_event(
            {
                "session_id": "s1",
                "event": "tool.start",
                "tool": "Bash",
                "command": "pytest tests/test_app.py",
                "file_path": "/tmp/work/app.py",
            },
            source="codex",
        )
    )

    reducer.apply(event)

    got = sessions.get("s1")
    assert got is not None
    assert got.extras["tool_name"] == "Bash"
    assert got.extras["command"] == "pytest tests/test_app.py"
    assert got.extras["file_path"] == "/tmp/work/app.py"


def test_reducer_creates_pending_approval() -> None:
    reducer, sessions, approvals = _reducer()

    reducer.apply(
        PermissionRequested(
            session_id="s1",
            source="claude",
            ts_ms=1_000,
            title="Claude",
            summary="needs permission",
            approval_id="a1",
            prompt="Allow Bash?",
        )
    )

    got = sessions.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.WAITING_FOR_APPROVAL
    assert got.priority is Priority.P0
    pending = approvals.list_pending()
    assert len(pending) == 1
    assert pending[0].approval_id == "a1"
    assert pending[0].prompt == "Allow Bash?"


def test_reducer_adds_approval_risk_and_preview_extras() -> None:
    reducer, _sessions, approvals = _reducer()

    reducer.apply(
        PermissionRequested(
            session_id="s1",
            source="claude",
            ts_ms=1_000,
            title="Claude",
            summary="needs permission",
            approval_id="a1",
            prompt="Allow Bash?",
            tool_name="Bash",
            command="sudo rm -rf build/cache",
        )
    )

    pending = approvals.list_pending()
    assert len(pending) == 1
    extras = pending[0].extras
    assert extras["tool_name"] == "Bash"
    assert extras["command"] == "sudo rm -rf build/cache"
    assert extras["risk_level"] == "high"
    assert "Shell command" in extras["risk_summary"]
    assert extras["approval_preview"] == "cmd: sudo rm -rf build/cache"


def test_hook_permission_approval_gets_file_risk_context() -> None:
    reducer, _sessions, approvals = _reducer()
    event = event_from_hook(
        normalize_hook_event(
            {
                "session_id": "s1",
                "event": "PermissionRequest",
                "approval_id": "a1",
                "prompt": "Allow edit?",
                "tool": "Edit",
                "file_path": "/tmp/work/App.swift",
            },
            source="claude",
        )
    )

    reducer.apply(event)

    pending = approvals.list_pending()
    assert len(pending) == 1
    extras = pending[0].extras
    assert extras["tool_name"] == "Edit"
    assert extras["file_path"] == "/tmp/work/App.swift"
    assert extras["risk_level"] == "medium"
    assert extras["risk_summary"] == "Tool may modify a local file."
    assert extras["approval_preview"] == "file: /tmp/work/App.swift"


def test_reducer_preserves_actionable_state_until_resolved() -> None:
    reducer, sessions, _approvals = _reducer()
    reducer.apply(
        PermissionRequested(
            session_id="s1",
            source="codex",
            ts_ms=1_000,
            approval_id="a1",
            prompt="Allow command?",
        )
    )

    reducer.apply(
        SessionActivityUpdated(
            session_id="s1",
            source="codex",
            ts_ms=2_000,
            summary="working",
            phase=SessionPhase.RUNNING,
        )
    )

    got = sessions.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.WAITING_FOR_APPROVAL
    assert got.summary == "working"


def test_reducer_marks_completed_without_removing_session() -> None:
    reducer, sessions, _approvals = _reducer()

    reducer.apply(
        SessionCompleted(
            session_id="s1",
            source="cursor",
            ts_ms=3_000,
            title="Cursor",
            summary="done",
        )
    )

    got = sessions.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.COMPLETED
    assert got.summary == "done"


def test_hook_event_maps_to_permission_agent_event() -> None:
    hook = normalize_hook_event(
        {
            "hook_event_name": "PermissionRequest",
            "session_id": "s1",
            "prompt": "Allow?",
        },
        source="claude",
    )

    event = event_from_hook(hook)

    assert isinstance(event, PermissionRequested)
    assert event.approval_id == "s1-approval"
    assert event.prompt == "Allow?"
