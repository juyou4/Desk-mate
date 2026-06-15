from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskmate_agent.agent_events import AgentEventReducer, SessionCompleted, SessionStarted
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.codex_transcripts import (
    CodexTranscriptWatcher,
    discover_codex_transcripts,
    events_from_codex_transcript,
    parse_codex_transcript,
)
from deskmate_agent.sessions import SessionPhase, SessionStore


def _write_rollout(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_parse_codex_transcript_extracts_session_shape(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(
        path,
        [
            {
                "timestamp": "2026-06-07T01:00:00.000Z",
                "type": "session_meta",
                "payload": {
                    "id": "s1",
                    "timestamp": "2026-06-07T01:00:00.000Z",
                    "cwd": "/tmp/work",
                },
            },
            {
                "timestamp": "2026-06-07T01:00:01.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Fix the hook doctor."}],
                },
            },
            {
                "timestamp": "2026-06-07T01:00:02.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Implemented the fix."}],
                },
            },
            {
                "timestamp": "2026-06-07T01:00:03.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Done.",
                    "completed_at": 1780794003,
                },
            },
        ],
    )

    summary = parse_codex_transcript(path)

    assert summary is not None
    assert summary.session_id == "s1"
    assert summary.cwd == "/tmp/work"
    assert summary.title == "Fix the hook doctor."
    assert summary.last_user == "Fix the hook doctor."
    assert summary.last_assistant == "Done."
    assert summary.completed is True


def test_codex_transcript_events_update_session_store_extras(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(
        path,
        [
            {
                "type": "session_meta",
                "payload": {"id": "s1", "cwd": "/tmp/work"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Start this task"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Working on it"}],
                },
            },
        ],
    )
    summary = parse_codex_transcript(path)
    assert summary is not None
    events = events_from_codex_transcript(summary)

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    for event in events:
        reducer.apply(event)

    got = sessions.get("s1")
    assert isinstance(events[0], SessionStarted)
    assert got is not None
    assert got.source == "codex"
    assert got.cwd == "/tmp/work"
    assert got.jump_url == "codex://threads/s1"
    assert got.extras["last_user"] == "Start this task"
    assert got.extras["last_assistant"] == "Working on it"
    assert got.phase is SessionPhase.RUNNING


def test_codex_transcript_function_call_sets_tool_phase_and_extras(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s1", "cwd": "/tmp/work"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "pytest tests/test_hooks.py -q"}),
                },
            },
        ],
    )

    summary = parse_codex_transcript(path)
    assert summary is not None
    events = events_from_codex_transcript(summary)

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    for event in events:
        reducer.apply(event)

    got = sessions.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.TESTING
    assert got.summary == "Running exec_command"
    assert got.extras["tool_name"] == "exec_command"
    assert got.extras["command"] == "pytest tests/test_hooks.py -q"


def test_failed_codex_transcript_preserves_failed_phase_and_tool(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s1"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "apply_patch",
                    "arguments": "*** Begin Patch\n*** End Patch",
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_failed", "message": "Patch failed."},
            },
        ],
    )

    summary = parse_codex_transcript(path)
    assert summary is not None
    events = events_from_codex_transcript(summary)

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    for event in events:
        reducer.apply(event)

    got = sessions.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.FAILED
    assert got.summary == "Patch failed."
    assert got.extras["tool_name"] == "apply_patch"
    assert got.extras["command"] == "*** Begin Patch *** End Patch"


def test_completed_codex_transcript_emits_completion_event(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(
        path,
        [
            {"type": "session_meta", "payload": {"id": "s1"}},
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "last_agent_message": "All set."},
            },
        ],
    )
    summary = parse_codex_transcript(path)
    assert summary is not None

    events = events_from_codex_transcript(summary)

    assert len(events) == 2
    assert isinstance(events[-1], SessionCompleted)
    assert events[-1].summary == "All set."


def test_discover_codex_transcripts_returns_recent_limited_paths(tmp_path) -> None:
    old = tmp_path / "sessions" / "2026" / "01" / "01" / "old.jsonl"
    new = tmp_path / "sessions" / "2026" / "01" / "02" / "new.jsonl"
    _write_rollout(old, [{"type": "session_meta", "payload": {"id": "old"}}])
    _write_rollout(new, [{"type": "session_meta", "payload": {"id": "new"}}])

    paths = discover_codex_transcripts(root=tmp_path, limit=1)

    assert paths == [new]


@pytest.mark.asyncio
async def test_codex_transcript_watcher_scans_changed_files_once(tmp_path) -> None:
    path = tmp_path / "sessions" / "2026" / "06" / "07" / "rollout-demo.jsonl"
    _write_rollout(path, [{"type": "session_meta", "payload": {"id": "s1"}}])
    events = []

    async def handle(event):
        events.append(event)

    watcher = CodexTranscriptWatcher(handle, root=tmp_path, poll_interval_s=0.01)

    assert await watcher.scan_once() == 2
    assert await watcher.scan_once() == 0
    assert len(events) == 2
