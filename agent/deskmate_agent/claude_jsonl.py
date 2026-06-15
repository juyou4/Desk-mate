"""Claude Code JSONL incremental parser.

The parser accepts append-only transcript rows and maps the small subset we
need for the island into the shared AgentEvent reducer shape. Unknown fields
stay in ``raw_event`` via a compact JSON string so hook format drift does not
break the live surface.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_events import (
    AgentEvent,
    PermissionRequested,
    QuestionAsked,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
)
from .sessions import SessionPhase


@dataclass
class ClaudeJsonlCursor:
    path: Path
    offset: int = 0
    partial: str = ""
    seen_uuids: set[str] = field(default_factory=set)


def parse_claude_jsonl_lines(
    lines: list[str],
    *,
    session_id: str,
    cwd: str | None = None,
    title: str = "Claude Code",
    ts_ms: int | None = None,
) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            event = event_from_claude_jsonl_row(
                row,
                session_id=session_id,
                cwd=cwd,
                title=title,
                ts_ms=ts_ms,
            )
            if event is not None:
                events.append(event)
    return events


def read_incremental_claude_jsonl(
    cursor: ClaudeJsonlCursor,
    *,
    session_id: str,
    title: str = "Claude Code",
    cwd: str | None = None,
    clock: Callable[[], int] | None = None,
) -> list[AgentEvent]:
    clock_fn = clock or _now_ms
    try:
        with cursor.path.open("r", encoding="utf-8") as fh:
            fh.seek(cursor.offset)
            chunk = fh.read()
            cursor.offset = fh.tell()
    except FileNotFoundError:
        return []

    if not chunk:
        return []
    text = cursor.partial + chunk
    if text.endswith("\n"):
        lines = text.splitlines()
        cursor.partial = ""
    else:
        lines = text.splitlines()
        cursor.partial = lines.pop() if lines else text

    events: list[AgentEvent] = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        uuid = _optional_text(row, "uuid")
        if uuid and uuid in cursor.seen_uuids:
            continue
        if uuid:
            cursor.seen_uuids.add(uuid)
        event = event_from_claude_jsonl_row(
            row,
            session_id=session_id,
            cwd=cwd,
            title=title,
            ts_ms=clock_fn(),
        )
        if event is not None:
            events.append(event)
    return events


def event_from_claude_jsonl_row(
    row: dict[str, Any],
    *,
    session_id: str,
    cwd: str | None = None,
    title: str = "Claude Code",
    ts_ms: int | None = None,
) -> AgentEvent | None:
    ts = ts_ms or _timestamp_ms(row) or _now_ms()
    row_session_id = _optional_text(row, "sessionId", "session_id") or session_id
    row_cwd = _optional_text(row, "cwd") or cwd
    raw_event = _raw_event(row)
    kind = _optional_text(row, "type") or ""

    if kind == "summary":
        summary = _optional_text(row, "summary") or ""
        return SessionStarted(
            session_id=row_session_id,
            source="claude_code",
            ts_ms=ts,
            title=summary or title,
            summary=summary,
            cwd=row_cwd,
            raw_event=raw_event,
            phase=SessionPhase.RUNNING,
        )

    message = row.get("message")
    if not isinstance(message, dict):
        return None

    role = _optional_text(message, "role") or kind
    content = message.get("content")
    texts, tool_uses, tool_results = _extract_content(content)
    text = _clip(" ".join(texts))
    if tool_results:
        result = tool_results[0]
        result_text = _tool_result_text(result)
        result_id = _optional_text(result, "tool_use_id", "toolUseID", "id") or ""
        return SessionActivityUpdated(
            session_id=row_session_id,
            source="claude_code",
            ts_ms=ts,
            title=title,
            summary=result_text or "Claude tool completed.",
            cwd=row_cwd,
            raw_event=raw_event,
            phase=SessionPhase.RUNNING,
            tool_result=result_text,
            tool_result_id=result_id,
        )

    if role == "user":
        if _looks_like_ask_user_question(text):
            return QuestionAsked(
                session_id=row_session_id,
                source="claude_code",
                ts_ms=ts,
                title=title,
                summary=text,
                cwd=row_cwd,
                raw_event=raw_event,
                prompt=text,
                last_user=text,
            )
        return SessionActivityUpdated(
            session_id=row_session_id,
            source="claude_code",
            ts_ms=ts,
            title=title,
            summary=text or "User prompt submitted.",
            cwd=row_cwd,
            raw_event=raw_event,
            phase=SessionPhase.THINKING,
            last_user=text,
        )

    if role == "assistant":
        if tool_uses:
            tool = tool_uses[0]
            tool_name = _optional_text(tool, "name") or "tool"
            tool_id = _optional_text(tool, "id") or ""
            tool_input = tool.get("input")
            prompt = _tool_prompt(tool_name, tool_input)
            command = _tool_command(tool_input)
            file_path = _tool_file_path(tool_input)
            if tool_name.lower() in {"askuserquestion", "ask_user_question"}:
                return QuestionAsked(
                    session_id=row_session_id,
                    source="claude_code",
                    ts_ms=ts,
                    title=title,
                    summary=prompt or "Claude is asking a question.",
                    cwd=row_cwd,
                    raw_event=raw_event,
                    prompt=prompt,
                    tool_name=tool_name,
                    tool_id=tool_id,
                )
            if _tool_needs_approval(tool_name, tool_input):
                approval_id = _optional_text(tool, "id") or f"{row_session_id}-approval"
                return PermissionRequested(
                    session_id=row_session_id,
                    source="claude_code",
                    ts_ms=ts,
                    title=title,
                    summary=prompt or f"Claude wants to use {tool_name}.",
                    cwd=row_cwd,
                    raw_event=raw_event,
                    approval_id=approval_id,
                    prompt=prompt or f"Allow {tool_name}?",
                    tool_name=tool_name,
                    tool_id=tool_id,
                    command=command,
                    file_path=file_path,
                )
            return SessionActivityUpdated(
                session_id=row_session_id,
                source="claude_code",
                ts_ms=ts,
                title=title,
                summary=_tool_summary(tool_name, tool_input),
                cwd=row_cwd,
                raw_event=raw_event,
                phase=_phase_for_tool(tool_name, tool_input),
                tool_name=tool_name,
                tool_id=tool_id,
                command=command,
                file_path=file_path,
            )
        if _is_stop(row):
            return SessionCompleted(
                session_id=row_session_id,
                source="claude_code",
                ts_ms=ts,
                title=title,
                summary=text or "Claude turn completed.",
                cwd=row_cwd,
                raw_event=raw_event,
                failed=False,
                last_assistant=text,
            )
        return SessionActivityUpdated(
            session_id=row_session_id,
            source="claude_code",
            ts_ms=ts,
            title=title,
            summary=text or "Claude is responding.",
            cwd=row_cwd,
            raw_event=raw_event,
            phase=SessionPhase.THINKING,
            last_assistant=text,
        )

    return None


def _extract_content(content: Any) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    texts: list[str] = []
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                item_type = _optional_text(item, "type") or ""
                if item_type == "text":
                    text = _optional_text(item, "text")
                    if text:
                        texts.append(text)
                elif item_type == "tool_result":
                    tool_results.append(item)
                elif item_type in {"tool_use", "server_tool_use"} or "name" in item:
                    tool_uses.append(item)
    return texts, tool_uses, tool_results


def _tool_result_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, str):
        return _clip(content)
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = _optional_text(item, "text", "content")
                if text:
                    parts.append(text)
        return _clip(" ".join(parts))
    text = _optional_text(result, "text", "result", "message")
    return _clip(text) if text else ""


def _tool_prompt(tool_name: str, tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("question", "prompt", "command", "description", "file_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value)
    return ""


def _tool_command(tool_input: Any) -> str:
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "args"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value)
    if isinstance(tool_input, str):
        return _clip(tool_input)
    return ""


def _tool_file_path(tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    for key in ("file_path", "filePath", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _tool_summary(tool_name: str, tool_input: Any) -> str:
    prompt = _tool_prompt(tool_name, tool_input)
    if prompt:
        return f"Claude is using {tool_name}: {prompt}"
    return f"Claude is using {tool_name}."


def _tool_needs_approval(tool_name: str, tool_input: Any) -> bool:
    lowered = tool_name.lower()
    if lowered in {"bash", "edit", "multiedit", "write", "notebookedit"}:
        return True
    if isinstance(tool_input, dict):
        permission = _optional_text(tool_input, "permission", "requires_permission")
        if permission and permission.lower() in {"true", "required", "ask"}:
            return True
    return False


def _phase_for_tool(tool_name: str, tool_input: Any) -> SessionPhase:
    lowered = tool_name.lower()
    prompt = _tool_prompt(tool_name, tool_input).lower()
    if lowered in {"edit", "multiedit", "write", "notebookedit"}:
        return SessionPhase.EDITING
    if "test" in prompt or "pytest" in prompt or "swift test" in prompt:
        return SessionPhase.TESTING
    if lowered in {"task"}:
        return SessionPhase.RUNNING_TOOL
    return SessionPhase.RUNNING_TOOL


def _looks_like_ask_user_question(text: str) -> bool:
    lowered = text.lower()
    return "askuserquestion" in lowered or "ask user question" in lowered


def _is_stop(row: dict[str, Any]) -> bool:
    return (_optional_text(row, "stop_reason", "finish_reason") or "") in {
        "stop_sequence",
        "end_turn",
        "stop",
    }


def _timestamp_ms(row: dict[str, Any]) -> int | None:
    value = row.get("timestamp") or row.get("created_at")
    if isinstance(value, (int, float)):
        return int(value * 1000 if value < 10_000_000_000 else value)
    if isinstance(value, str) and value.strip():
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(parsed.timestamp() * 1000)
        except ValueError:
            return None
    return None


def _optional_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _raw_event(row: dict[str, Any]) -> str:
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True)[:2000]
    except TypeError:
        return str(row)[:2000]


def _clip(value: str, *, limit: int = 180) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "ClaudeJsonlCursor",
    "event_from_claude_jsonl_row",
    "parse_claude_jsonl_lines",
    "read_incremental_claude_jsonl",
]
