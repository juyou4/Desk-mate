"""CLI diagnostics for persisted chat/profile/tool memory."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


def _seed_memory_dbs(db_dir: Path) -> None:
    db_dir.mkdir(parents=True, exist_ok=True)

    chat = sqlite3.connect(db_dir / "chat.db")
    try:
        chat.executescript(
            """
            CREATE TABLE chat_messages (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id  TEXT NOT NULL,
                role             TEXT NOT NULL,
                content          TEXT,
                tool_calls_json  TEXT,
                tool_call_id     TEXT,
                ts_ms            INTEGER NOT NULL
            );
            CREATE TABLE chat_summaries (
                conversation_id  TEXT PRIMARY KEY,
                summary          TEXT NOT NULL,
                message_count    INTEGER NOT NULL,
                updated_at_ms    INTEGER NOT NULL
            );
            """
        )
        chat.execute(
            """
            INSERT INTO chat_messages
                (conversation_id, role, content, tool_calls_json, tool_call_id, ts_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("default", "user", "remember Cursor", None, None, 1_000),
        )
        chat.execute(
            """
            INSERT INTO chat_summaries
                (conversation_id, summary, message_count, updated_at_ms)
            VALUES (?, ?, ?, ?)
            """,
            ("default", "- User: remember Cursor", 1, 1_000),
        )
        chat.commit()
    finally:
        chat.close()

    profile = sqlite3.connect(db_dir / "profile.db")
    try:
        profile.execute(
            """
            CREATE TABLE profile (
                key         TEXT PRIMARY KEY,
                value_json  TEXT NOT NULL,
                updated_at  INTEGER NOT NULL
            );
            """
        )
        profile.execute(
            "INSERT INTO profile (key, value_json, updated_at) VALUES (?, ?, ?)",
            (
                "memories.facts",
                json.dumps(
                    {
                        "preferred_ide": {
                            "key": "preferred_ide",
                            "value": "Cursor",
                            "updated_at_ms": 2_000,
                            "approval_id": "mem-1",
                        }
                    }
                ),
                2_000,
            ),
        )
        profile.commit()
    finally:
        profile.close()

    tasks = sqlite3.connect(db_dir / "tasks.db")
    try:
        tasks.executescript(
            """
            CREATE TABLE deskmate_tasks (
                task_id            TEXT PRIMARY KEY,
                conversation_id    TEXT NOT NULL,
                title              TEXT NOT NULL,
                status             TEXT NOT NULL,
                notes              TEXT NOT NULL,
                created_at_ms      INTEGER NOT NULL,
                updated_at_ms      INTEGER NOT NULL,
                completed_at_ms    INTEGER
            );
            CREATE TABLE deskmate_task_steps (
                step_id            TEXT PRIMARY KEY,
                task_id            TEXT NOT NULL,
                conversation_id    TEXT NOT NULL,
                position           INTEGER NOT NULL,
                content            TEXT NOT NULL,
                status             TEXT NOT NULL,
                active_form        TEXT NOT NULL,
                created_at_ms      INTEGER NOT NULL,
                updated_at_ms      INTEGER NOT NULL,
                completed_at_ms    INTEGER
            );
            """
        )
        tasks.execute(
            """
            INSERT INTO deskmate_tasks
                (
                    task_id, conversation_id, title, status, notes,
                    created_at_ms, updated_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task-polish-island",
                "default",
                "Polish island task lane",
                "in_progress",
                "Keep the collapsed lane compact.",
                2_500,
                2_600,
                None,
            ),
        )
        tasks.execute(
            """
            INSERT INTO deskmate_task_steps
                (
                    step_id, task_id, conversation_id, position, content,
                    status, active_form, created_at_ms, updated_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "step-hydrating",
                "task-polish-island",
                "default",
                1,
                "Inspect hydrating island snapshot",
                "in_progress",
                "Inspecting hydrating island snapshot",
                2_550,
                2_600,
                None,
            ),
        )
        tasks.commit()
    finally:
        tasks.close()

    tools = sqlite3.connect(db_dir / "tool_actions.db")
    try:
        tools.executescript(
            """
            CREATE TABLE tool_actions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id   TEXT NOT NULL,
                tool_call_id      TEXT NOT NULL,
                task_id           TEXT,
                tool_name         TEXT NOT NULL,
                arguments_json    TEXT,
                summary_json      TEXT,
                result            TEXT NOT NULL,
                status            TEXT NOT NULL,
                started_at_ms     INTEGER NOT NULL,
                completed_at_ms   INTEGER NOT NULL
            );
            CREATE TABLE tool_tasks (
                task_id            TEXT PRIMARY KEY,
                conversation_id    TEXT NOT NULL,
                user_text          TEXT NOT NULL,
                status             TEXT NOT NULL,
                summary            TEXT NOT NULL,
                action_count       INTEGER NOT NULL,
                failed_count       INTEGER NOT NULL,
                duplicate_count    INTEGER NOT NULL,
                started_at_ms      INTEGER NOT NULL,
                updated_at_ms      INTEGER NOT NULL,
                completed_at_ms    INTEGER
            );
            CREATE TABLE tool_lessons (
                lesson_key        TEXT PRIMARY KEY,
                conversation_id   TEXT NOT NULL,
                tool_name         TEXT NOT NULL,
                target            TEXT NOT NULL,
                outcome           TEXT NOT NULL,
                status            TEXT NOT NULL,
                needs_user        INTEGER NOT NULL,
                lesson            TEXT NOT NULL,
                source_action_id  INTEGER,
                task_id           TEXT,
                created_at_ms     INTEGER NOT NULL,
                updated_at_ms     INTEGER NOT NULL,
                seen_count        INTEGER NOT NULL
            );
            """
        )
        tools.execute(
            """
            INSERT INTO tool_tasks
                (
                    task_id, conversation_id, user_text, status, summary,
                    action_count, failed_count, duplicate_count,
                    started_at_ms, updated_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "task-open-terminal",
                "default",
                "Polish island task lane",
                "failed",
                "Tool error while polishing island lane.",
                1,
                1,
                0,
                3_000,
                3_100,
                3_100,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_actions
                (
                    conversation_id, tool_call_id, task_id, tool_name,
                    arguments_json, summary_json, result, status,
                    started_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "call-1",
                "task-open-terminal",
                "deskmate_computer_action",
                '{"command":"open island diagnostics"}',
                json.dumps(
                    {
                        "action": "deskmate_computer_action",
                        "target": "open island diagnostics",
                        "outcome": "Tool error while polishing island lane.",
                        "needs_user": True,
                    }
                ),
                "Tool error while polishing island lane.",
                "failed",
                3_010,
                3_020,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_actions
                (
                    conversation_id, tool_call_id, task_id, tool_name,
                    arguments_json, summary_json, result, status,
                    started_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "call-direct",
                "task-polish-island",
                "deskmate_task_command",
                '{"kind":"start","query":"opaque target"}',
                json.dumps(
                    {
                        "action": "task.start",
                        "target": "opaque target",
                        "outcome": "Task started for opaque target.",
                        "needs_user": False,
                    }
                ),
                "Task started for opaque target.",
                "completed",
                3_030,
                3_040,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_tasks
                (
                    task_id, conversation_id, user_text, status, summary,
                    action_count, failed_count, duplicate_count,
                    started_at_ms, updated_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "tool-task-hydrating",
                "default",
                "Inspecting hydrating island snapshot",
                "completed",
                "Hydrating island snapshot inspected.",
                1,
                0,
                0,
                3_200,
                3_300,
                3_300,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_actions
                (
                    conversation_id, tool_call_id, task_id, tool_name,
                    arguments_json, summary_json, result, status,
                    started_at_ms, completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "default",
                "call-hydrating",
                "tool-task-hydrating",
                "deskmate_computer_action",
                '{"command":"open hydrating snapshot diagnostics"}',
                json.dumps(
                    {
                        "action": "deskmate_computer_action",
                        "target": "open hydrating snapshot diagnostics",
                        "outcome": "Opened hydrating snapshot diagnostics.",
                        "needs_user": False,
                    }
                ),
                "Opened hydrating snapshot diagnostics.",
                "completed",
                3_210,
                3_220,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_lessons
                (
                    lesson_key, conversation_id, tool_name, target, outcome,
                    status, needs_user, lesson, source_action_id, task_id,
                    created_at_ms, updated_at_ms, seen_count
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lesson-island",
                "default",
                "deskmate_computer_action",
                "open island diagnostics",
                "Tool error while polishing island lane.",
                "failed",
                1,
                "deskmate_computer_action on open island diagnostics last failed.",
                1,
                "task-open-terminal",
                3_020,
                3_020,
                2,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_lessons
                (
                    lesson_key, conversation_id, tool_name, target, outcome,
                    status, needs_user, lesson, source_action_id, task_id,
                    created_at_ms, updated_at_ms, seen_count
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lesson-direct",
                "default",
                "task.start",
                "opaque target",
                "Task started for opaque target.",
                "completed",
                0,
                "task.start on opaque target completed.",
                2,
                "task-polish-island",
                3_040,
                3_040,
                1,
            ),
        )
        tools.execute(
            """
            INSERT INTO tool_lessons
                (
                    lesson_key, conversation_id, tool_name, target, outcome,
                    status, needs_user, lesson, source_action_id, task_id,
                    created_at_ms, updated_at_ms, seen_count
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lesson-hydrating",
                "default",
                "deskmate_computer_action",
                "open hydrating snapshot diagnostics",
                "Opened hydrating snapshot diagnostics.",
                "completed",
                0,
                "deskmate_computer_action on hydrating snapshot completed.",
                3,
                "tool-task-hydrating",
                3_220,
                3_220,
                1,
            ),
        )
        tools.commit()
    finally:
        tools.close()


def test_cli_memory_summary_json_reads_persisted_stores(
    tmp_path, monkeypatch, capsys
) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "summary", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["chat"]["message_count"] == 1
    assert payload["chat"]["summary_message_count"] == 1
    assert payload["profile"]["facts_count"] == 1
    assert payload["profile"]["facts"][0]["key"] == "preferred_ide"
    assert payload["tasks"]["task_count"] == 1
    assert payload["tasks"]["active_task_count"] == 1
    assert payload["tasks"]["recent_tasks"][0]["task_id"] == "task-polish-island"
    assert payload["tool_ledger"]["task_count"] == 2
    assert payload["tool_ledger"]["action_count"] == 3
    assert payload["tool_ledger"]["failed_task_count"] == 1
    assert [task["task_id"] for task in payload["tool_ledger"]["recent_tasks"]] == [
        "tool-task-hydrating",
        "task-open-terminal",
    ]


def test_cli_memory_summary_human_degrades_when_dbs_missing(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "missing"))
    from deskmate_agent.cli import main

    assert main(["memory", "summary"]) == 0

    out = capsys.readouterr().out
    assert "chat: 0 messages" in out
    assert "profile facts: 0" in out
    assert "tasks: 0 total, 0 active" in out
    assert "tool ledger: 0 tasks, 0 actions" in out


def test_cli_memory_tool_task_json_reads_actions(tmp_path, monkeypatch, capsys) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "tool-task", "task-open-terminal", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["task"]["status"] == "failed"
    assert payload["actions"][0]["tool_name"] == "deskmate_computer_action"
    assert payload["actions"][0]["summary"]["target"] == "open island diagnostics"


def test_cli_memory_tool_task_missing_returns_nonzero(
    tmp_path, monkeypatch, capsys
) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "tool-task", "missing"]) == 1

    err = capsys.readouterr().err
    assert "No tool task missing" in err


def test_cli_memory_task_json_reads_task(tmp_path, monkeypatch, capsys) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "task", "task-polish-island", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["task"]["status"] == "in_progress"
    assert payload["task"]["title"] == "Polish island task lane"


def test_cli_memory_task_context_json_reads_related_tool_state(
    tmp_path, monkeypatch, capsys
) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "task-context", "task-polish-island", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["task"]["task_id"] == "task-polish-island"
    assert payload["related_tool_tasks"][0]["task_id"] == "task-open-terminal"
    assert payload["related_tool_tasks"][0]["status"] == "failed"
    assert [row["tool_call_id"] for row in payload["related_tool_actions"]] == [
        "call-direct",
        "call-hydrating",
        "call-1",
    ]
    assert payload["related_tool_actions"][0]["summary"]["target"] == "opaque target"
    assert payload["related_tool_actions"][1]["summary"]["target"] == (
        "open hydrating snapshot diagnostics"
    )
    assert payload["related_tool_actions"][2]["summary"]["target"] == (
        "open island diagnostics"
    )
    assert [row["lesson_key"] for row in payload["related_tool_lessons"]] == [
        "lesson-direct",
        "lesson-hydrating",
        "lesson-island",
    ]
    assert payload["related_tool_lessons"][2]["seen_count"] == 2


def test_cli_memory_task_context_human_reads_related_tool_state(
    tmp_path, monkeypatch, capsys
) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "task-context", "task-polish-island"]) == 0

    out = capsys.readouterr().out
    assert "task-polish-island [in_progress]: Polish island task lane" in out
    assert "related tool tasks:" in out
    assert "task-open-terminal [failed]: Tool error while polishing island lane." in out
    assert "tool-task-hydrating [completed]: Hydrating island snapshot inspected." in out
    assert "related tool actions:" in out
    assert "deskmate_task_command [completed] target=opaque target" in out
    assert "deskmate_computer_action [failed] target=open island diagnostics" in out
    assert "deskmate_computer_action [completed] target=open hydrating snapshot diagnostics" in out
    assert "related tool lessons:" in out
    assert "task.start [completed] target=opaque target: Task started for opaque target." in out
    assert "deskmate_computer_action [failed] target=open island diagnostics: Tool error while polishing island lane. seen=2" in out
    assert "deskmate_computer_action [completed] target=open hydrating snapshot diagnostics: Opened hydrating snapshot diagnostics." in out


def test_cli_memory_task_missing_returns_nonzero(
    tmp_path, monkeypatch, capsys
) -> None:
    _seed_memory_dbs(tmp_path / "db")
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["memory", "task", "missing"]) == 1

    err = capsys.readouterr().err
    assert "No task missing" in err


def test_cli_task_add_list_search_and_done_json(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(
        [
            "task",
            "add",
            "Polish task CLI",
            "--notes",
            "Expose task ledger to scripts.",
            "--status",
            "in_progress",
            "--json",
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    task_id = created["task"]["task_id"]
    assert created["ok"] is True
    assert created["task"]["title"] == "Polish task CLI"
    assert created["task"]["status"] == "in_progress"

    assert main(["task", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in listed["tasks"]] == [task_id]

    assert main(["task", "search", "scripts", "--json"]) == 0
    searched = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in searched["tasks"]] == [task_id]

    assert main(["task", "done", task_id, "--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["task"]["status"] == "done"
    assert done["task"]["completed_at_ms"] is not None

    assert main(["task", "list"]) == 0
    assert capsys.readouterr().out == "No matching tasks.\n"

    assert main(["task", "list", "--status", "done", "--json"]) == 0
    done_list = json.loads(capsys.readouterr().out)
    assert [task["task_id"] for task in done_list["tasks"]] == [task_id]


def test_cli_task_update_and_cancel_human_output(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["task", "add", "Initial title", "--json"]) == 0
    task_id = json.loads(capsys.readouterr().out)["task"]["task_id"]

    assert main(
        [
            "task",
            "update",
            task_id,
            "--title",
            "Updated title",
            "--notes",
            "Manual note",
            "--status",
            "open",
        ]
    ) == 0
    assert capsys.readouterr().out.startswith(
        f"{task_id} [open]: Updated title - Manual note\n"
    )

    assert main(["task", "cancel", task_id]) == 0
    assert capsys.readouterr().out.startswith(
        f"{task_id} [cancelled]: Updated title - Manual note\n"
    )

    assert main(["task", "list", "--status", "cancelled"]) == 0
    assert capsys.readouterr().out.startswith(
        f"{task_id} [cancelled]: Updated title - Manual note\n"
    )


def test_cli_task_missing_and_invalid_update_return_nonzero(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("DESKMATE_DB_DIR", str(tmp_path / "db"))
    from deskmate_agent.cli import main

    assert main(["task", "done", "missing"]) == 1
    assert "No task missing" in capsys.readouterr().err

    assert main(["task", "update", "missing"]) == 2
    assert "provide --title, --notes, or --status" in capsys.readouterr().err
