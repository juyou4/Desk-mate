from __future__ import annotations

import json

from deskmate_agent.agent_events import (
    AgentEventReducer,
    PermissionRequested,
    QuestionAsked,
    SessionActivityUpdated,
)
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.claude_jsonl import (
    ClaudeJsonlCursor,
    parse_claude_jsonl_lines,
    read_incremental_claude_jsonl,
)
from deskmate_agent.protocol.state import Priority
from deskmate_agent.sessions import SessionPhase, SessionStore


def test_claude_jsonl_parses_summary_tool_question_and_approval() -> None:
    lines = [
        json.dumps({"type": "summary", "summary": "Fix island hover"}),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        }
                    ],
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "q1",
                            "name": "AskUserQuestion",
                            "input": {"question": "Which IDE should I open?"},
                        }
                    ],
                },
            }
        ),
    ]

    events = parse_claude_jsonl_lines(lines, session_id="s1", cwd="/tmp/work", ts_ms=1_000)

    assert len(events) == 3
    assert events[0].title == "Fix island hover"
    assert isinstance(events[1], PermissionRequested)
    assert events[1].approval_id == "t1"
    assert events[1].prompt == "pytest"
    assert isinstance(events[2], QuestionAsked)
    assert events[2].prompt == "Which IDE should I open?"


def test_claude_jsonl_unknown_fields_survive_raw_and_reducer_updates_store() -> None:
    events = parse_claude_jsonl_lines(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "future": {"nested": True},
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Thinking through the task"}],
                    },
                }
            )
        ],
        session_id="s1",
        ts_ms=2_000,
    )

    assert len(events) == 1
    assert isinstance(events[0], SessionActivityUpdated)
    assert '"future"' in events[0].raw_event

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    reducer.apply(events[0])
    got = sessions.get("s1")
    assert got is not None
    assert got.source == "claude_code"
    assert got.kind == "hook_session"
    assert got.phase is SessionPhase.THINKING
    assert got.priority is Priority.P1


def test_claude_jsonl_tool_metadata_reaches_session_extras() -> None:
    events = parse_claude_jsonl_lines(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Edit",
                                "input": {"file_path": "/tmp/work/app.py"},
                            }
                        ],
                    },
                }
            )
        ],
        session_id="s1",
        ts_ms=2_000,
    )

    assert len(events) == 1
    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    reducer.apply(events[0])
    got = sessions.get("s1")
    assert got is not None
    assert got.extras["tool_name"] == "Edit"
    assert got.extras["file_path"] == "/tmp/work/app.py"


def test_claude_jsonl_incremental_reader_keeps_partial_line(tmp_path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text('{"type":"summary","summary":"One"}\n{"type":"summary"', encoding="utf-8")
    cursor = ClaudeJsonlCursor(path=path)

    first = read_incremental_claude_jsonl(
        cursor,
        session_id="s1",
        clock=lambda: 1_000,
    )
    assert [event.summary for event in first] == ["One"]
    assert cursor.partial == '{"type":"summary"'

    with path.open("a", encoding="utf-8") as fh:
        fh.write(',"summary":"Two"}\n')

    second = read_incremental_claude_jsonl(
        cursor,
        session_id="s1",
        clock=lambda: 2_000,
    )
    assert [event.summary for event in second] == ["Two"]
