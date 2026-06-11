"""Persistent user-visible task memory for Deskmate."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite

from .session import now_ms

DeskmateTaskStatus = Literal["open", "in_progress", "done", "cancelled"]
DeskmateTaskStepStatus = Literal["pending", "in_progress", "completed"]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deskmate_tasks (
    task_id            TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    title              TEXT NOT NULL,
    status             TEXT NOT NULL,
    notes              TEXT NOT NULL,
    created_at_ms      INTEGER NOT NULL,
    updated_at_ms      INTEGER NOT NULL,
    completed_at_ms    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_deskmate_tasks_conversation_updated
    ON deskmate_tasks(conversation_id, updated_at_ms);

CREATE INDEX IF NOT EXISTS idx_deskmate_tasks_conversation_status
    ON deskmate_tasks(conversation_id, status, updated_at_ms);

CREATE TABLE IF NOT EXISTS deskmate_task_steps (
    step_id            TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    conversation_id    TEXT NOT NULL,
    position           INTEGER NOT NULL,
    content            TEXT NOT NULL,
    status             TEXT NOT NULL,
    active_form        TEXT NOT NULL,
    created_at_ms      INTEGER NOT NULL,
    updated_at_ms      INTEGER NOT NULL,
    completed_at_ms    INTEGER,
    FOREIGN KEY(task_id) REFERENCES deskmate_tasks(task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deskmate_task_steps_task_position
    ON deskmate_task_steps(conversation_id, task_id, position);
"""

_VALID_STATUSES: set[str] = {"open", "in_progress", "done", "cancelled"}
_VALID_STEP_STATUSES: set[str] = {"pending", "in_progress", "completed"}
_MAX_TASK_STEPS = 20


@dataclass(frozen=True)
class DeskmateTaskRecord:
    task_id: str
    conversation_id: str
    title: str
    status: DeskmateTaskStatus
    notes: str
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None = None


@dataclass(frozen=True)
class DeskmateTaskStep:
    step_id: str
    task_id: str
    conversation_id: str
    position: int
    content: str
    status: DeskmateTaskStepStatus
    active_form: str
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None = None


class DeskmateTaskStore:
    """Async SQLite store for durable user work items.

    This is separate from ``ToolActionLog`` on purpose: tool tasks describe
    one LLM execution lifecycle, while these records are user-visible todos
    that can survive across many chats and many tool-call lifecycles.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> DeskmateTaskStore:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def create(
        self,
        *,
        conversation_id: str = "default",
        title: str,
        notes: str = "",
        status: DeskmateTaskStatus = "open",
        task_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> DeskmateTaskRecord:
        title = _compact_text(title, limit=160)
        notes = _compact_text(notes, limit=2_000)
        if not title:
            raise ValueError("title is required")
        status = _normalize_status(status)
        ts = int(created_at_ms if created_at_ms is not None else now_ms())
        completed_at_ms = ts if status in {"done", "cancelled"} else None
        record = DeskmateTaskRecord(
            task_id=task_id or "task-" + uuid.uuid4().hex[:12],
            conversation_id=_compact_text(conversation_id, limit=120) or "default",
            title=title,
            status=status,
            notes=notes,
            created_at_ms=ts,
            updated_at_ms=ts,
            completed_at_ms=completed_at_ms,
        )
        conn = self._require()
        await conn.execute(
            """
            INSERT INTO deskmate_tasks
                (
                    task_id,
                    conversation_id,
                    title,
                    status,
                    notes,
                    created_at_ms,
                    updated_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.task_id,
                record.conversation_id,
                record.title,
                record.status,
                record.notes,
                record.created_at_ms,
                record.updated_at_ms,
                record.completed_at_ms,
            ),
        )
        await conn.commit()
        return record

    async def get(self, task_id: str) -> DeskmateTaskRecord | None:
        task_id = str(task_id or "").strip()
        if not task_id:
            return None
        conn = self._require()
        async with conn.execute(
            _TASK_SELECT_SQL + " WHERE task_id = ?",
            (task_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_task(row) if row else None

    async def list(
        self,
        conversation_id: str = "default",
        *,
        status: DeskmateTaskStatus | Literal["active", "all"] | None = "active",
        limit: int = 10,
    ) -> list[DeskmateTaskRecord]:
        limit = _clamp_limit(limit)
        conversation_id = _compact_text(conversation_id, limit=120) or "default"
        conn = self._require()
        if status in (None, "all"):
            async with conn.execute(
                _TASK_SELECT_SQL
                + """
                WHERE conversation_id = ?
                ORDER BY
                    CASE status
                        WHEN 'in_progress' THEN 0
                        WHEN 'open' THEN 1
                        WHEN 'done' THEN 2
                        ELSE 3
                    END,
                    updated_at_ms DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        elif status == "active":
            async with conn.execute(
                _TASK_SELECT_SQL
                + """
                WHERE conversation_id = ? AND status IN ('open', 'in_progress')
                ORDER BY
                    CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,
                    updated_at_ms DESC
                LIMIT ?
                """,
                (conversation_id, limit),
            ) as cur:
                rows = await cur.fetchall()
        else:
            normalized = _normalize_status(status)
            async with conn.execute(
                _TASK_SELECT_SQL
                + """
                WHERE conversation_id = ? AND status = ?
                ORDER BY updated_at_ms DESC
                LIMIT ?
                """,
                (conversation_id, normalized, limit),
            ) as cur:
                rows = await cur.fetchall()
        return [_row_to_task(row) for row in rows]

    async def search(
        self,
        conversation_id: str = "default",
        *,
        query: str,
        status: DeskmateTaskStatus | Literal["active", "all"] | None = "all",
        limit: int = 10,
    ) -> list[DeskmateTaskRecord]:
        query = str(query or "").strip()
        if not query:
            return []
        limit = _clamp_limit(limit)
        conversation_id = _compact_text(conversation_id, limit=120) or "default"
        like = f"%{query.lower()}%"
        conn = self._require()
        status_clause = ""
        params: list[object] = [conversation_id, like, like, like]
        if status == "active":
            status_clause = " AND status IN ('open', 'in_progress')"
        elif status not in (None, "all"):
            status_clause = " AND status = ?"
            params.append(_normalize_status(status))
        params.append(limit)
        async with conn.execute(
            _TASK_SELECT_SQL
            + f"""
            WHERE conversation_id = ?
              AND (
                lower(task_id) LIKE ?
                OR lower(title) LIKE ?
                OR lower(notes) LIKE ?
              )
              {status_clause}
            ORDER BY updated_at_ms DESC
            LIMIT ?
            """,
            tuple(params),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_task(row) for row in rows]

    async def update(
        self,
        task_id: str,
        *,
        conversation_id: str = "default",
        title: str | None = None,
        status: DeskmateTaskStatus | None = None,
        notes: str | None = None,
        updated_at_ms: int | None = None,
    ) -> DeskmateTaskRecord | None:
        existing = await self.get(task_id)
        if existing is None or existing.conversation_id != conversation_id:
            return None
        next_status = _normalize_status(status) if status is not None else existing.status
        next_title = (
            _compact_text(title, limit=160)
            if title is not None
            else existing.title
        )
        next_notes = (
            _compact_text(notes, limit=2_000)
            if notes is not None
            else existing.notes
        )
        if not next_title:
            raise ValueError("title is required")
        ts = int(updated_at_ms if updated_at_ms is not None else now_ms())
        completed_at_ms = existing.completed_at_ms
        if next_status in {"done", "cancelled"} and completed_at_ms is None:
            completed_at_ms = ts
        elif next_status in {"open", "in_progress"}:
            completed_at_ms = None
        record = DeskmateTaskRecord(
            task_id=existing.task_id,
            conversation_id=existing.conversation_id,
            title=next_title,
            status=next_status,
            notes=next_notes,
            created_at_ms=existing.created_at_ms,
            updated_at_ms=ts,
            completed_at_ms=completed_at_ms,
        )
        conn = self._require()
        await conn.execute(
            """
            UPDATE deskmate_tasks
            SET title = ?,
                status = ?,
                notes = ?,
                updated_at_ms = ?,
                completed_at_ms = ?
            WHERE task_id = ? AND conversation_id = ?
            """,
            (
                record.title,
                record.status,
                record.notes,
                record.updated_at_ms,
                record.completed_at_ms,
                record.task_id,
                record.conversation_id,
            ),
        )
        await conn.commit()
        return record

    async def replace_steps(
        self,
        task_id: str,
        steps: list[DeskmateTaskStep | dict[str, object]],
        *,
        conversation_id: str = "default",
        updated_at_ms: int | None = None,
    ) -> list[DeskmateTaskStep] | None:
        task = await self.get(task_id)
        conversation_id = _compact_text(conversation_id, limit=120) or "default"
        if task is None or task.conversation_id != conversation_id:
            return None
        ts = int(updated_at_ms if updated_at_ms is not None else now_ms())
        normalized = _normalize_steps(
            steps,
            task_id=task.task_id,
            conversation_id=conversation_id,
            now=ts,
        )
        conn = self._require()
        await conn.execute(
            "DELETE FROM deskmate_task_steps WHERE task_id = ? AND conversation_id = ?",
            (task.task_id, conversation_id),
        )
        await conn.executemany(
            """
            INSERT INTO deskmate_task_steps
                (
                    step_id,
                    task_id,
                    conversation_id,
                    position,
                    content,
                    status,
                    active_form,
                    created_at_ms,
                    updated_at_ms,
                    completed_at_ms
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    step.step_id,
                    step.task_id,
                    step.conversation_id,
                    step.position,
                    step.content,
                    step.status,
                    step.active_form,
                    step.created_at_ms,
                    step.updated_at_ms,
                    step.completed_at_ms,
                )
                for step in normalized
            ],
        )
        await conn.commit()
        return normalized

    async def list_steps(
        self,
        task_id: str,
        *,
        conversation_id: str = "default",
    ) -> list[DeskmateTaskStep]:
        task_id = str(task_id or "").strip()
        if not task_id:
            return []
        conversation_id = _compact_text(conversation_id, limit=120) or "default"
        conn = self._require()
        async with conn.execute(
            _TASK_STEP_SELECT_SQL
            + """
            WHERE task_id = ? AND conversation_id = ?
            ORDER BY position ASC
            """,
            (task_id, conversation_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_step(row) for row in rows]

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("DeskmateTaskStore is not open")
        return self._conn


_TASK_SELECT_SQL = """
SELECT
    task_id,
    conversation_id,
    title,
    status,
    notes,
    created_at_ms,
    updated_at_ms,
    completed_at_ms
FROM deskmate_tasks
"""

_TASK_STEP_SELECT_SQL = """
SELECT
    step_id,
    task_id,
    conversation_id,
    position,
    content,
    status,
    active_form,
    created_at_ms,
    updated_at_ms,
    completed_at_ms
FROM deskmate_task_steps
"""


def _row_to_task(row: tuple[object, ...]) -> DeskmateTaskRecord:
    return DeskmateTaskRecord(
        task_id=str(row[0]),
        conversation_id=str(row[1]),
        title=str(row[2]),
        status=_normalize_status(str(row[3])),
        notes=str(row[4]),
        created_at_ms=int(row[5]),
        updated_at_ms=int(row[6]),
        completed_at_ms=int(row[7]) if row[7] is not None else None,
    )


def _row_to_step(row: tuple[object, ...]) -> DeskmateTaskStep:
    return DeskmateTaskStep(
        step_id=str(row[0]),
        task_id=str(row[1]),
        conversation_id=str(row[2]),
        position=int(row[3]),
        content=str(row[4]),
        status=_normalize_step_status(str(row[5])),
        active_form=str(row[6]),
        created_at_ms=int(row[7]),
        updated_at_ms=int(row[8]),
        completed_at_ms=int(row[9]) if row[9] is not None else None,
    )


def _normalize_status(value: object) -> DeskmateTaskStatus:
    status = str(value or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise ValueError("status must be open, in_progress, done, or cancelled")
    return status  # type: ignore[return-value]


def _normalize_step_status(value: object) -> DeskmateTaskStepStatus:
    status = str(value or "").strip().lower()
    if status == "done":
        status = "completed"
    if status not in _VALID_STEP_STATUSES:
        raise ValueError("step status must be pending, in_progress, or completed")
    return status  # type: ignore[return-value]


def _normalize_steps(
    steps: list[DeskmateTaskStep | dict[str, object]],
    *,
    task_id: str,
    conversation_id: str,
    now: int,
) -> list[DeskmateTaskStep]:
    if len(steps) > _MAX_TASK_STEPS:
        raise ValueError("task steps are limited to 20 items")
    normalized: list[DeskmateTaskStep] = []
    in_progress_count = 0
    for index, raw in enumerate(steps, start=1):
        if isinstance(raw, DeskmateTaskStep):
            content = raw.content
            status = raw.status
            active_form = raw.active_form
            step_id = raw.step_id
            created_at_ms = raw.created_at_ms
        else:
            content = str(raw.get("content") or "")
            status = _normalize_step_status(raw.get("status") or "pending")
            active_form = str(raw.get("active_form") or raw.get("activeForm") or "")
            step_id = str(raw.get("step_id") or raw.get("id") or "").strip()
            created_at_ms = int(raw.get("created_at_ms") or now)
        content = _compact_text(content, limit=240)
        active_form = _compact_text(active_form, limit=240)
        if not content:
            raise ValueError("step content is required")
        if status == "in_progress":
            in_progress_count += 1
            if not active_form:
                active_form = content
        if in_progress_count > 1:
            raise ValueError("only one task step can be in_progress")
        completed_at_ms = now if status == "completed" else None
        normalized.append(
            DeskmateTaskStep(
                step_id=step_id or "step-" + uuid.uuid4().hex[:12],
                task_id=task_id,
                conversation_id=conversation_id,
                position=index,
                content=content,
                status=status,
                active_form=active_form,
                created_at_ms=created_at_ms,
                updated_at_ms=now,
                completed_at_ms=completed_at_ms,
            )
        )
    return normalized


def _compact_text(value: object, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _clamp_limit(value: int) -> int:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        raw = 10
    return max(1, min(raw, 50))


def format_deskmate_task(record: DeskmateTaskRecord) -> str:
    suffix = f" - {record.notes}" if record.notes else ""
    return f"{record.task_id} [{record.status}]: {record.title}{suffix}"


def format_deskmate_task_step(record: DeskmateTaskStep) -> str:
    active = f" -> {record.active_form}" if record.active_form else ""
    return f"{record.position}. [{record.status}] {record.content}{active}"


__all__ = [
    "DeskmateTaskRecord",
    "DeskmateTaskStep",
    "DeskmateTaskStepStatus",
    "DeskmateTaskStatus",
    "DeskmateTaskStore",
    "format_deskmate_task",
    "format_deskmate_task_step",
]
