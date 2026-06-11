"""Persistent log for Deskmate-owned tool calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import aiosqlite

ToolActionStatus = Literal["completed", "failed", "duplicate"]
ToolTaskStatus = Literal["running", "completed", "failed"]
_REDACTED = "[redacted]"
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "bearer",
    "clipboard",
    "clipboard_text",
    "cookie",
    "keychain",
    "password",
    "secret",
    "set_clipboard",
    "token",
}
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "authorization",
    "clipboard",
    "cookie",
    "keychain",
    "password",
    "secret",
    "token",
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tool_actions (
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

CREATE INDEX IF NOT EXISTS idx_tool_actions_conversation_id_id
    ON tool_actions(conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_tool_actions_name_id
    ON tool_actions(tool_name, id);

CREATE TABLE IF NOT EXISTS tool_tasks (
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

CREATE INDEX IF NOT EXISTS idx_tool_tasks_conversation_updated
    ON tool_tasks(conversation_id, updated_at_ms);

CREATE TABLE IF NOT EXISTS tool_lessons (
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

CREATE INDEX IF NOT EXISTS idx_tool_lessons_conversation_updated
    ON tool_lessons(conversation_id, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_tool_lessons_tool_updated
    ON tool_lessons(tool_name, updated_at_ms);
"""


@dataclass(frozen=True)
class ToolActionRecord:
    conversation_id: str
    tool_call_id: str
    tool_name: str
    result: str
    status: ToolActionStatus
    started_at_ms: int
    completed_at_ms: int
    arguments: Any = None
    summary: dict[str, Any] | None = None
    task_id: str | None = None
    row_id: int | None = field(default=None)


@dataclass(frozen=True)
class ToolTaskRecord:
    task_id: str
    conversation_id: str
    user_text: str
    status: ToolTaskStatus
    summary: str
    action_count: int
    failed_count: int
    duplicate_count: int
    started_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None = None


@dataclass(frozen=True)
class ToolLessonRecord:
    lesson_key: str
    conversation_id: str
    tool_name: str
    target: str
    outcome: str
    status: ToolActionStatus
    needs_user: bool
    lesson: str
    created_at_ms: int
    updated_at_ms: int
    seen_count: int
    source_action_id: int | None = None
    task_id: str | None = None


class ToolActionLog:
    """Async SQLite-backed audit trail for local Deskmate tool calls."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA_SQL)
        await self._ensure_summary_column()
        await self._ensure_task_id_column()
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> ToolActionLog:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def append(self, record: ToolActionRecord) -> int:
        conn = self._require()
        arguments = sanitize_tool_arguments(record.arguments)
        summary = record.summary or summarize_tool_action(
            tool_name=record.tool_name,
            arguments=arguments,
            result=record.result,
            status=record.status,
        )
        cur = await conn.execute(
            """
            INSERT INTO tool_actions
                (
                    conversation_id,
                    tool_call_id,
                    task_id,
                    tool_name,
                    arguments_json,
                    summary_json,
                    result,
                    status,
                    started_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.conversation_id,
                record.tool_call_id,
                record.task_id,
                record.tool_name,
                _json_dumps(arguments),
                _json_dumps(summary),
                record.result,
                record.status,
                record.started_at_ms,
                record.completed_at_ms,
            ),
        )
        row_id = int(cur.lastrowid or 0)
        await _upsert_tool_lesson(
            conn,
            record=record,
            row_id=row_id,
            arguments=arguments,
            summary=summary,
        )
        await conn.commit()
        return row_id

    async def upsert_task(self, record: ToolTaskRecord) -> None:
        conn = self._require()
        await conn.execute(
            """
            INSERT INTO tool_tasks
                (
                    task_id,
                    conversation_id,
                    user_text,
                    status,
                    summary,
                    action_count,
                    failed_count,
                    duplicate_count,
                    started_at_ms,
                    updated_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                status = excluded.status,
                summary = excluded.summary,
                action_count = excluded.action_count,
                failed_count = excluded.failed_count,
                duplicate_count = excluded.duplicate_count,
                updated_at_ms = excluded.updated_at_ms,
                completed_at_ms = excluded.completed_at_ms
            """,
            (
                record.task_id,
                record.conversation_id,
                _compact_text(record.user_text, limit=240),
                record.status,
                _compact_text(record.summary, limit=240),
                max(0, record.action_count),
                max(0, record.failed_count),
                max(0, record.duplicate_count),
                record.started_at_ms,
                record.updated_at_ms,
                record.completed_at_ms,
            ),
        )
        await conn.commit()

    async def get_task(self, task_id: str) -> ToolTaskRecord | None:
        conn = self._require()
        async with conn.execute(
            """
            SELECT
                task_id,
                conversation_id,
                user_text,
                status,
                summary,
                action_count,
                failed_count,
                duplicate_count,
                started_at_ms,
                updated_at_ms,
                completed_at_ms
            FROM tool_tasks
            WHERE task_id = ?
            """,
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def recent_tasks(
        self,
        conversation_id: str = "default",
        *,
        limit: int = 5,
    ) -> list[ToolTaskRecord]:
        if limit <= 0:
            return []
        limit = max(1, min(limit, 25))
        conn = self._require()
        async with conn.execute(
            """
            SELECT
                task_id,
                conversation_id,
                user_text,
                status,
                summary,
                action_count,
                failed_count,
                duplicate_count,
                started_at_ms,
                updated_at_ms,
                completed_at_ms
            FROM tool_tasks
            WHERE conversation_id = ?
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_task(row) for row in rows]

    async def search_tasks(
        self,
        conversation_id: str = "default",
        *,
        query: str,
        limit: int = 5,
    ) -> list[ToolTaskRecord]:
        needle = query.strip()
        if not needle or limit <= 0:
            return []
        limit = max(1, min(limit, 25))
        conn = self._require()
        async with conn.execute(
            """
            SELECT
                task_id,
                conversation_id,
                user_text,
                status,
                summary,
                action_count,
                failed_count,
                duplicate_count,
                started_at_ms,
                updated_at_ms,
                completed_at_ms
            FROM tool_tasks
            WHERE conversation_id = ?
              AND (
                task_id LIKE ? ESCAPE '\\'
                OR user_text LIKE ? ESCAPE '\\'
                OR summary LIKE ? ESCAPE '\\'
              )
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            (
                conversation_id,
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                limit,
            ),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_task(row) for row in rows]

    async def mark_stale_running_tasks_failed(
        self,
        *,
        cutoff_updated_at_ms: int,
        completed_at_ms: int,
        summary: str = "Interrupted before Deskmate could finish the tool task.",
    ) -> int:
        """Finalize old running task lifecycles after an agent restart.

        Tool tasks are persisted before their first tool call runs. If the
        Python process exits mid-turn, no future code path owns that task id, so
        leaving it as ``running`` would pollute future LLM context and island
        state. This method only updates metadata; it never replays tool calls.
        """
        conn = self._require()
        cur = await conn.execute(
            """
            UPDATE tool_tasks
            SET
                status = 'failed',
                summary = ?,
                failed_count = CASE
                    WHEN failed_count > 0 THEN failed_count
                    WHEN action_count > 0 THEN 1
                    ELSE 0
                END,
                updated_at_ms = ?,
                completed_at_ms = ?
            WHERE status = 'running'
              AND updated_at_ms < ?
            """,
            (
                _compact_text(summary, limit=240),
                completed_at_ms,
                completed_at_ms,
                cutoff_updated_at_ms,
            ),
        )
        await conn.commit()
        return int(cur.rowcount or 0)

    async def recent(
        self,
        conversation_id: str = "default",
        *,
        tool_name: str | None = None,
        task_id: str | None = None,
        status: ToolActionStatus | None = None,
        limit: int = 10,
    ) -> list[ToolActionRecord]:
        if limit <= 0:
            return []
        limit = max(1, min(limit, 50))
        clauses = ["conversation_id = ?"]
        params: list[Any] = [conversation_id]
        if tool_name:
            clauses.append("tool_name = ?")
            params.append(tool_name)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        params.append(limit)

        conn = self._require()
        async with conn.execute(
            f"""
            SELECT
                id,
                conversation_id,
                tool_call_id,
                task_id,
                tool_name,
                arguments_json,
                summary_json,
                result,
                status,
                started_at_ms,
                completed_at_ms
            FROM tool_actions
            WHERE {" AND ".join(clauses)}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_record(row) for row in rows]

    async def search(
        self,
        conversation_id: str = "default",
        *,
        query: str,
        limit: int = 10,
    ) -> list[ToolActionRecord]:
        needle = query.strip()
        if not needle or limit <= 0:
            return []
        limit = max(1, min(limit, 50))
        conn = self._require()
        async with conn.execute(
            """
            SELECT
                id,
                conversation_id,
                tool_call_id,
                task_id,
                tool_name,
                arguments_json,
                summary_json,
                result,
                status,
                started_at_ms,
                completed_at_ms
            FROM tool_actions
            WHERE conversation_id = ?
              AND (
                tool_name LIKE ? ESCAPE '\\'
                OR task_id LIKE ? ESCAPE '\\'
                OR result LIKE ? ESCAPE '\\'
                OR arguments_json LIKE ? ESCAPE '\\'
                OR summary_json LIKE ? ESCAPE '\\'
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                conversation_id,
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                limit,
            ),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_record(row) for row in rows]

    async def recent_lessons(
        self,
        conversation_id: str = "default",
        *,
        task_id: str | None = None,
        limit: int = 5,
    ) -> list[ToolLessonRecord]:
        if limit <= 0:
            return []
        limit = max(1, min(limit, 25))
        clauses = ["conversation_id = ?"]
        params: list[Any] = [conversation_id]
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        params.append(limit)
        conn = self._require()
        async with conn.execute(
            f"""
            SELECT
                lesson_key,
                conversation_id,
                tool_name,
                target,
                outcome,
                status,
                needs_user,
                lesson,
                source_action_id,
                task_id,
                created_at_ms,
                updated_at_ms,
                seen_count
            FROM tool_lessons
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            params,
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_lesson(row) for row in rows]

    async def search_lessons(
        self,
        conversation_id: str = "default",
        *,
        query: str,
        limit: int = 5,
    ) -> list[ToolLessonRecord]:
        needle = query.strip()
        if not needle or limit <= 0:
            return []
        limit = max(1, min(limit, 25))
        conn = self._require()
        async with conn.execute(
            """
            SELECT
                lesson_key,
                conversation_id,
                tool_name,
                target,
                outcome,
                status,
                needs_user,
                lesson,
                source_action_id,
                task_id,
                created_at_ms,
                updated_at_ms,
                seen_count
            FROM tool_lessons
            WHERE conversation_id = ?
              AND (
                tool_name LIKE ? ESCAPE '\\'
                OR target LIKE ? ESCAPE '\\'
                OR outcome LIKE ? ESCAPE '\\'
                OR lesson LIKE ? ESCAPE '\\'
              )
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            (
                conversation_id,
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                f"%{_escape_like(needle)}%",
                limit,
            ),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [_row_to_lesson(row) for row in rows]

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ToolActionLog used before open()")
        return self._conn

    async def _ensure_summary_column(self) -> None:
        conn = self._require()
        async with conn.execute("PRAGMA table_info(tool_actions)") as cur:
            rows = await cur.fetchall()
        columns = {str(row[1]) for row in rows}
        if "summary_json" not in columns:
            await conn.execute("ALTER TABLE tool_actions ADD COLUMN summary_json TEXT")

    async def _ensure_task_id_column(self) -> None:
        conn = self._require()
        async with conn.execute("PRAGMA table_info(tool_actions)") as cur:
            rows = await cur.fetchall()
        columns = {str(row[1]) for row in rows}
        if "task_id" not in columns:
            await conn.execute("ALTER TABLE tool_actions ADD COLUMN task_id TEXT")


def _row_to_record(row: tuple[Any, ...]) -> ToolActionRecord:
    return ToolActionRecord(
        row_id=int(row[0]),
        conversation_id=str(row[1]),
        tool_call_id=str(row[2]),
        task_id=str(row[3]) if row[3] is not None else None,
        tool_name=str(row[4]),
        arguments=_json_loads(row[5]),
        summary=_json_loads(row[6]),
        result=str(row[7]),
        status=str(row[8]),
        started_at_ms=int(row[9]),
        completed_at_ms=int(row[10]),
    )


def _row_to_task(row: tuple[Any, ...]) -> ToolTaskRecord:
    return ToolTaskRecord(
        task_id=str(row[0]),
        conversation_id=str(row[1]),
        user_text=str(row[2]),
        status=str(row[3]),
        summary=str(row[4]),
        action_count=int(row[5]),
        failed_count=int(row[6]),
        duplicate_count=int(row[7]),
        started_at_ms=int(row[8]),
        updated_at_ms=int(row[9]),
        completed_at_ms=int(row[10]) if row[10] is not None else None,
    )


def _row_to_lesson(row: tuple[Any, ...]) -> ToolLessonRecord:
    return ToolLessonRecord(
        lesson_key=str(row[0]),
        conversation_id=str(row[1]),
        tool_name=str(row[2]),
        target=str(row[3]),
        outcome=str(row[4]),
        status=str(row[5]),
        needs_user=bool(row[6]),
        lesson=str(row[7]),
        source_action_id=int(row[8]) if row[8] is not None else None,
        task_id=str(row[9]) if row[9] is not None else None,
        created_at_ms=int(row[10]),
        updated_at_ms=int(row[11]),
        seen_count=int(row[12]),
    )


def sanitize_tool_arguments(value: Any) -> Any:
    """Return an audit-safe copy of tool-call arguments.

    The log is durable and later injected into LLM context, so secret-like
    fields and user-authored payload values are stored as summaries.
    """
    return _sanitize_value(value)


def summarize_tool_action(
    *,
    tool_name: str,
    arguments: Any,
    result: str,
    status: ToolActionStatus,
) -> dict[str, Any]:
    """Build a compact structured summary for LLM/island consumption."""
    outcome = _compact_text(result, limit=180)
    return {
        "action": tool_name or "tool",
        "target": _summarize_target(arguments),
        "outcome": outcome,
        "needs_user": _needs_user(result, status),
    }


def tool_action_summary(record: ToolActionRecord) -> dict[str, Any]:
    if isinstance(record.summary, dict):
        return dict(record.summary)
    return summarize_tool_action(
        tool_name=record.tool_name,
        arguments=record.arguments,
        result=record.result,
        status=record.status,
    )


def format_tool_action_summary(record: ToolActionRecord) -> str:
    summary = tool_action_summary(record)
    action = _compact_text(str(summary.get("action") or record.tool_name), limit=80)
    target = _compact_text(str(summary.get("target") or ""), limit=120)
    outcome = _compact_text(str(summary.get("outcome") or record.result), limit=180)
    needs_user = bool(summary.get("needs_user"))
    parts = [
        f"action={action}",
        f"status={record.status}",
    ]
    if target:
        parts.append(f"target={target}")
    if outcome:
        parts.append(f"outcome={outcome}")
    parts.append(f"needs_user={'true' if needs_user else 'false'}")
    return "; ".join(parts)


def format_tool_task_summary(record: ToolTaskRecord) -> str:
    parts = [
        f"task={_compact_text(record.task_id, limit=80)}",
        f"status={record.status}",
        f"actions={record.action_count}",
    ]
    if record.failed_count:
        parts.append(f"failed={record.failed_count}")
    if record.duplicate_count:
        parts.append(f"duplicate={record.duplicate_count}")
    if record.summary:
        parts.append(f"summary={_compact_text(record.summary, limit=180)}")
    return "; ".join(parts)


def format_tool_lesson(record: ToolLessonRecord) -> str:
    parts = [
        f"tool={_compact_text(record.tool_name, limit=80)}",
        f"status={record.status}",
    ]
    if record.target:
        parts.append(f"target={_compact_text(record.target, limit=120)}")
    if record.outcome:
        parts.append(f"outcome={_compact_text(record.outcome, limit=180)}")
    if record.needs_user:
        parts.append("needs_user=true")
    if record.seen_count > 1:
        parts.append(f"seen={record.seen_count}")
    return "; ".join(parts)


async def _upsert_tool_lesson(
    conn: aiosqlite.Connection,
    *,
    record: ToolActionRecord,
    row_id: int,
    arguments: Any,
    summary: dict[str, Any],
) -> None:
    if record.status == "duplicate":
        return
    lesson = _build_tool_lesson(record, arguments=arguments, summary=summary)
    if lesson is None:
        return
    await conn.execute(
        """
        INSERT INTO tool_lessons
            (
                lesson_key,
                conversation_id,
                tool_name,
                target,
                outcome,
                status,
                needs_user,
                lesson,
                source_action_id,
                task_id,
                created_at_ms,
                updated_at_ms,
                seen_count
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(lesson_key) DO UPDATE SET
            outcome = excluded.outcome,
            status = excluded.status,
            needs_user = excluded.needs_user,
            lesson = excluded.lesson,
            source_action_id = excluded.source_action_id,
            task_id = excluded.task_id,
            updated_at_ms = excluded.updated_at_ms,
            seen_count = tool_lessons.seen_count + 1
        """,
        (
            lesson.lesson_key,
            lesson.conversation_id,
            lesson.tool_name,
            lesson.target,
            lesson.outcome,
            lesson.status,
            1 if lesson.needs_user else 0,
            lesson.lesson,
            row_id or None,
            record.task_id,
            lesson.created_at_ms,
            lesson.updated_at_ms,
        ),
    )


def _build_tool_lesson(
    record: ToolActionRecord,
    *,
    arguments: Any,
    summary: dict[str, Any],
) -> ToolLessonRecord | None:
    action = _compact_text(str(summary.get("action") or record.tool_name), limit=120)
    target = _compact_text(str(summary.get("target") or _summarize_target(arguments)), limit=160)
    outcome = _compact_text(str(summary.get("outcome") or record.result), limit=220)
    if not action or not outcome:
        return None
    needs_user = bool(summary.get("needs_user"))
    status = record.status
    lesson_key = _lesson_key(
        conversation_id=record.conversation_id,
        tool_name=action,
        target=target,
        status=status,
    )
    lesson_text = _lesson_text(
        tool_name=action,
        target=target,
        outcome=outcome,
        status=status,
        needs_user=needs_user,
    )
    return ToolLessonRecord(
        lesson_key=lesson_key,
        conversation_id=record.conversation_id,
        tool_name=action,
        target=target,
        outcome=outcome,
        status=status,
        needs_user=needs_user,
        lesson=lesson_text,
        source_action_id=record.row_id,
        task_id=record.task_id,
        created_at_ms=record.completed_at_ms,
        updated_at_ms=record.completed_at_ms,
        seen_count=1,
    )


def _lesson_text(
    *,
    tool_name: str,
    target: str,
    outcome: str,
    status: ToolActionStatus,
    needs_user: bool,
) -> str:
    target_part = f" on {target}" if target else ""
    user_part = " Requires user action." if needs_user else ""
    return f"{tool_name}{target_part} last {status}: {outcome}.{user_part}"


def _lesson_key(
    *,
    conversation_id: str,
    tool_name: str,
    target: str,
    status: ToolActionStatus,
) -> str:
    raw = "\x1f".join(
        [
            conversation_id.strip().lower(),
            tool_name.strip().lower(),
            target.strip().lower(),
            status,
        ]
    )
    import hashlib

    return "lesson-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _sanitize_value(value: Any, *, key: str = "") -> Any:
    if _is_sensitive_key(key):
        return _redacted_summary(value)
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value[:20]]
    if isinstance(value, tuple):
        return [_sanitize_value(item) for item in value[:20]]
    if isinstance(value, str):
        return _sanitize_string(value)
    return value


def _summarize_target(arguments: Any) -> str:
    if isinstance(arguments, dict):
        for key in (
            "command",
            "text",
            "reminder_id",
            "query",
            "memory_key",
            "tool_name",
            "path",
            "cwd",
        ):
            value = arguments.get(key)
            if value not in (None, ""):
                return _compact_text(str(value), limit=120)
        if arguments:
            return ", ".join(str(key) for key in list(arguments.keys())[:4])
    if isinstance(arguments, str) and arguments:
        return _compact_text(arguments, limit=120)
    return ""


def _needs_user(result: str, status: ToolActionStatus) -> bool:
    lowered = result.lower()
    if "pending approval" in lowered or "approval required" in lowered:
        return True
    if "i need your approval" in lowered or "requires an explicit user" in lowered:
        return True
    return status == "failed" and "tool error:" in lowered


def _is_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    if not normalized:
        return False
    if normalized in _SENSITIVE_KEYS:
        return True
    return any(fragment in normalized for fragment in _SENSITIVE_KEY_FRAGMENTS)


def _redacted_summary(value: Any) -> str:
    if isinstance(value, str):
        return f"{_REDACTED} len={len(value)}"
    if isinstance(value, (list, tuple, dict)):
        return f"{_REDACTED} {type(value).__name__}"
    return _REDACTED


def _sanitize_string(value: str) -> str:
    return _compact_text(value, limit=240)


def _compact_text(value: str, *, limit: int) -> str:
    compacted = " ".join(value.strip().split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


__all__ = [
    "SCHEMA_SQL",
    "ToolActionLog",
    "ToolActionRecord",
    "ToolActionStatus",
    "ToolLessonRecord",
    "ToolTaskRecord",
    "ToolTaskStatus",
    "format_tool_action_summary",
    "format_tool_lesson",
    "format_tool_task_summary",
    "sanitize_tool_arguments",
    "summarize_tool_action",
    "tool_action_summary",
]
