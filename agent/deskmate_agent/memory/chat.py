"""Persistent chat transcript memory.

The LLM composer uses this store as a durable rolling context. It stores the
same OpenAI-compatible message shape used on the wire, including assistant
tool calls and tool-result messages.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from .types import Message

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL,
    role             TEXT NOT NULL,
    content          TEXT,
    tool_calls_json  TEXT,
    tool_call_id     TEXT,
    ts_ms            INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_id_id
    ON chat_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS chat_summaries (
    conversation_id  TEXT PRIMARY KEY,
    summary          TEXT NOT NULL,
    message_count    INTEGER NOT NULL,
    updated_at_ms    INTEGER NOT NULL
);
"""

_SUMMARY_MAX_LINES = 24
_SUMMARY_MAX_CHARS = 1_800


@dataclass(frozen=True)
class ChatSummary:
    conversation_id: str
    summary: str
    message_count: int
    updated_at_ms: int


class ChatMemory:
    """Async SQLite-backed chat transcript store."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.executescript(SCHEMA_SQL)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> ChatMemory:
        await self.open()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def append(self, conversation_id: str, message: Message) -> None:
        await self.append_many(conversation_id, [message])

    async def append_many(
        self,
        conversation_id: str,
        messages: list[Message],
    ) -> None:
        if not messages:
            return
        conn = self._require()
        await conn.executemany(
            """
            INSERT INTO chat_messages
                (conversation_id, role, content, tool_calls_json, tool_call_id, ts_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    conversation_id,
                    message.role,
                    message.content,
                    _json_dumps(message.tool_calls),
                    message.tool_call_id,
                    message.ts_ms,
                )
                for message in messages
            ],
        )
        await self._update_summary(conversation_id, messages)
        await conn.commit()

    async def recent(
        self,
        conversation_id: str = "default",
        *,
        limit: int = 20,
    ) -> list[Message]:
        if limit <= 0:
            return []
        conn = self._require()
        async with conn.execute(
            """
            SELECT role, content, tool_calls_json, tool_call_id, ts_ms
            FROM chat_messages
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [self._row_to_message(row) for row in rows]

    async def search(
        self,
        conversation_id: str = "default",
        *,
        query: str,
        limit: int = 5,
    ) -> list[Message]:
        """Return recent transcript messages whose visible text matches ``query``.

        This deliberately searches only ``content``. Tool-call payloads can
        contain arguments or implementation detail the model should not need to
        recall unless a tool already surfaced them as a normal message.
        """
        needle = query.strip()
        if not needle or limit <= 0:
            return []
        limit = max(1, min(limit, 20))
        conn = self._require()
        async with conn.execute(
            """
            SELECT role, content, tool_calls_json, tool_call_id, ts_ms
            FROM chat_messages
            WHERE conversation_id = ?
              AND content IS NOT NULL
              AND content LIKE ? ESCAPE '\\'
            ORDER BY id DESC
            LIMIT ?
            """,
            (conversation_id, f"%{_escape_like(needle)}%", limit),
        ) as cur:
            rows = await cur.fetchall()
        rows.reverse()
        return [self._row_to_message(row) for row in rows]

    async def get_summary(
        self,
        conversation_id: str = "default",
    ) -> ChatSummary | None:
        conn = self._require()
        async with conn.execute(
            """
            SELECT conversation_id, summary, message_count, updated_at_ms
            FROM chat_summaries
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        return ChatSummary(
            conversation_id=str(row[0]),
            summary=str(row[1]),
            message_count=int(row[2]),
            updated_at_ms=int(row[3]),
        )

    async def clear(self, conversation_id: str = "default") -> None:
        conn = self._require()
        await conn.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        await conn.execute(
            "DELETE FROM chat_summaries WHERE conversation_id = ?",
            (conversation_id,),
        )
        await conn.commit()

    async def pragma(self, name: str) -> str | int | None:
        conn = self._require()
        async with conn.execute(f"PRAGMA {name}") as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    @staticmethod
    def _row_to_message(row: tuple[Any, ...]) -> Message:
        return Message(
            role=row[0],
            content=row[1],
            tool_calls=_json_loads(row[2]),
            tool_call_id=row[3],
            ts_ms=row[4],
        )

    def _require(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("ChatMemory used before open()")
        return self._conn

    async def _update_summary(
        self,
        conversation_id: str,
        messages: list[Message],
    ) -> None:
        lines = _summary_lines(messages)
        if not lines:
            return
        conn = self._require()
        async with conn.execute(
            """
            SELECT summary, message_count
            FROM chat_summaries
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ) as cur:
            row = await cur.fetchone()
        old_summary = str(row[0]) if row else ""
        old_count = int(row[1]) if row else 0
        summary = _merge_summary(old_summary, lines)
        await conn.execute(
            """
            INSERT INTO chat_summaries
                (conversation_id, summary, message_count, updated_at_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                summary = excluded.summary,
                message_count = excluded.message_count,
                updated_at_ms = excluded.updated_at_ms
            """,
            (
                conversation_id,
                summary,
                old_count + len(messages),
                _now_ms(),
            ),
        )


def _summary_lines(messages: list[Message]) -> list[str]:
    lines: list[str] = []
    for message in messages:
        text = _visible_message_text(message)
        if not text:
            continue
        if message.role == "tool":
            prefix = "Tool"
        elif message.role == "assistant":
            prefix = "Assistant"
        elif message.role == "user":
            prefix = "User"
        else:
            continue
        lines.append(f"- {prefix}: {_compact_text(text, limit=220)}")
    return lines


def _visible_message_text(message: Message) -> str:
    if message.content:
        return " ".join(message.content.split())
    if message.tool_calls:
        names: list[str] = []
        for call in message.tool_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict):
                name = str(function.get("name") or "").strip()
                if name:
                    names.append(name)
        if names:
            return "called " + ", ".join(names[:4])
    return ""


def _merge_summary(old_summary: str, new_lines: list[str]) -> str:
    lines = [
        line.strip()
        for line in old_summary.splitlines()
        if line.strip()
    ]
    lines.extend(new_lines)
    deduped: list[str] = []
    for line in lines:
        if deduped and deduped[-1] == line:
            continue
        deduped.append(line)
    kept: list[str] = []
    total = 0
    for line in reversed(deduped):
        if len(kept) >= _SUMMARY_MAX_LINES:
            break
        if total + len(line) + 1 > _SUMMARY_MAX_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    return "\n".join(kept)


def _compact_text(value: str, *, limit: int) -> str:
    compacted = " ".join(value.strip().split())
    if len(compacted) <= limit:
        return compacted
    return compacted[: limit - 3].rstrip() + "..."


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json_dumps(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


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


__all__ = ["ChatMemory", "ChatSummary", "SCHEMA_SQL"]
