"""Codex local transcript discovery.

Reads Codex rollout JSONL files from ``~/.codex/sessions`` and maps the
recent session shape into shared AgentEvents. This is intentionally read-only
and best-effort: malformed rows, missing directories, and format drift are
ignored without affecting live hooks or the app-server bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .agent_events import (
    AgentEvent,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
)
from .logging_setup import get_logger
from .sessions import SessionPhase

_LOG = get_logger("deskmate_agent.codex_transcripts")

CodexEventHandler = Callable[[AgentEvent], Awaitable[None]]
Clock = Callable[[], int]


@dataclass(frozen=True)
class CodexTranscriptSummary:
    session_id: str
    path: Path
    cwd: str | None = None
    title: str = "Codex session"
    summary: str = ""
    last_user: str = ""
    last_assistant: str = ""
    phase: SessionPhase = SessionPhase.RUNNING
    tool_name: str = ""
    command: str = ""
    started_at_ms: int = 0
    updated_at_ms: int = 0
    completed: bool = False
    failed: bool = False


def default_codex_transcript_root() -> Path:
    return Path.home() / ".codex"


def discover_codex_transcripts(
    *,
    root: Path | None = None,
    limit: int = 10,
) -> list[Path]:
    base = root or default_codex_transcript_root()
    sessions = base / "sessions"
    try:
        paths = [p for p in sessions.rglob("*.jsonl") if p.is_file()]
    except OSError:
        return []
    paths.sort(key=lambda p: _safe_mtime_ns(p), reverse=True)
    return paths[: max(0, limit)]


def parse_codex_transcript(path: Path) -> CodexTranscriptSummary | None:
    state: dict[str, Any] = {
        "session_id": "",
        "cwd": None,
        "title": "",
        "summary": "",
        "last_user": "",
        "last_assistant": "",
        "phase": SessionPhase.RUNNING,
        "tool_name": "",
        "command": "",
        "started_at_ms": 0,
        "updated_at_ms": _safe_mtime_ms(path),
        "completed": False,
        "failed": False,
    }
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                _consume_row(state, line)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _LOG.warning("codex_transcripts.read_failed", path=str(path), error=str(exc))
        return None
    session_id = str(state["session_id"] or _session_id_from_path(path))
    if not session_id:
        return None
    title = str(state["title"] or _title_from_cwd(state["cwd"]) or "Codex session")
    summary = str(state["summary"] or state["last_assistant"] or state["last_user"] or "")
    return CodexTranscriptSummary(
        session_id=session_id,
        path=path,
        cwd=state["cwd"],
        title=title,
        summary=summary,
        last_user=str(state["last_user"] or ""),
        last_assistant=str(state["last_assistant"] or ""),
        phase=state["phase"] if isinstance(state["phase"], SessionPhase) else SessionPhase.RUNNING,
        tool_name=str(state["tool_name"] or ""),
        command=str(state["command"] or ""),
        started_at_ms=int(state["started_at_ms"] or 0),
        updated_at_ms=int(state["updated_at_ms"] or 0),
        completed=bool(state["completed"]),
        failed=bool(state["failed"]),
    )


def events_from_codex_transcript(summary: CodexTranscriptSummary) -> list[AgentEvent]:
    ts_ms = summary.updated_at_ms or int(time.time() * 1000)
    title = f"Codex · {summary.title}" if not summary.title.startswith("Codex") else summary.title
    started = SessionStarted(
        session_id=summary.session_id,
        source="codex",
        ts_ms=summary.started_at_ms or ts_ms,
        title=title,
        summary=summary.summary or "Codex transcript discovered.",
        cwd=summary.cwd,
        jump_url=f"codex://threads/{summary.session_id}",
        raw_event="transcript/session_meta",
        phase=SessionPhase.COMPLETED if summary.completed else SessionPhase.RUNNING,
        tool_name=summary.tool_name,
        command=summary.command,
        last_user=summary.last_user,
        last_assistant=summary.last_assistant,
    )
    if summary.completed or summary.failed:
        return [
            started,
            SessionCompleted(
                session_id=summary.session_id,
                source="codex",
                ts_ms=ts_ms,
                title=title,
                summary=summary.summary or "Codex transcript completed.",
                cwd=summary.cwd,
                jump_url=f"codex://threads/{summary.session_id}",
                raw_event="transcript/task_complete",
                failed=summary.failed,
                tool_name=summary.tool_name,
                command=summary.command,
                last_user=summary.last_user,
                last_assistant=summary.last_assistant,
            ),
        ]
    return [
        started,
        SessionActivityUpdated(
            session_id=summary.session_id,
            source="codex",
            ts_ms=ts_ms,
            title=title,
            summary=summary.summary or "Codex transcript active.",
            cwd=summary.cwd,
            jump_url=f"codex://threads/{summary.session_id}",
            raw_event="transcript/activity",
            phase=summary.phase,
            tool_name=summary.tool_name,
            command=summary.command,
            last_user=summary.last_user,
            last_assistant=summary.last_assistant,
        ),
    ]


class CodexTranscriptWatcher:
    def __init__(
        self,
        handler: CodexEventHandler,
        *,
        root: Path | None = None,
        poll_interval_s: float = 5.0,
        limit: int = 10,
        clock: Clock | None = None,
    ) -> None:
        self._handler = handler
        self._root = root or default_codex_transcript_root()
        self._poll = poll_interval_s
        self._limit = limit
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._seen_mtimes: dict[Path, int] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="codex-transcript-watcher")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def scan_once(self) -> int:
        count = 0
        for path in discover_codex_transcripts(root=self._root, limit=self._limit):
            mtime = _safe_mtime_ms(path)
            if self._seen_mtimes.get(path) == mtime:
                continue
            summary = parse_codex_transcript(path)
            if summary is None:
                continue
            self._seen_mtimes[path] = mtime
            for event in events_from_codex_transcript(summary):
                await self._handler(event)
                count += 1
        return count

    async def _run(self) -> None:
        while not self._stopping:
            await self.scan_once()
            await asyncio.sleep(self._poll)


def _consume_row(state: dict[str, Any], line: str) -> None:
    stripped = line.strip()
    if not stripped:
        return
    try:
        row = json.loads(stripped)
    except json.JSONDecodeError:
        return
    if not isinstance(row, dict):
        return
    ts = _timestamp_ms(row.get("timestamp"))
    if ts:
        state["updated_at_ms"] = max(int(state["updated_at_ms"] or 0), ts)
    row_type = str(row.get("type") or "")
    payload = row.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if row_type == "session_meta":
        session_id = _optional_text(payload, "id", "session_id")
        if session_id:
            state["session_id"] = session_id
        cwd = _optional_text(payload, "cwd")
        if cwd:
            state["cwd"] = cwd
        started = _timestamp_ms(payload.get("timestamp")) or ts
        if started:
            state["started_at_ms"] = started
        return
    if row_type == "turn_context":
        cwd = _optional_text(payload, "cwd")
        if cwd:
            state["cwd"] = cwd
        return
    if row_type == "response_item":
        _consume_response_item(state, payload)
        return
    if row_type == "event_msg":
        _consume_event_msg(state, payload)


def _consume_response_item(state: dict[str, Any], payload: dict[str, Any]) -> None:
    item_type = str(payload.get("type") or "")
    role = str(payload.get("role") or "")
    if item_type == "message":
        text = _message_text(payload.get("content"))
        if not text:
            return
        if role == "user":
            state["last_user"] = text
            state["summary"] = text
            if not state.get("title"):
                state["title"] = _clip(text, limit=48)
        elif role == "assistant":
            state["last_assistant"] = text
            state["summary"] = text
        return
    if item_type == "function_call":
        name = _optional_text(payload, "name")
        if name:
            state["tool_name"] = name
            command = _command_from_function_call(payload)
            if command:
                state["command"] = command
            state["summary"] = f"Running {name}"
            state["phase"] = _phase_for_tool(name, command)


def _consume_event_msg(state: dict[str, Any], payload: dict[str, Any]) -> None:
    msg_type = str(payload.get("type") or "")
    if msg_type == "task_complete":
        state["completed"] = True
        last = _optional_text(payload, "last_agent_message")
        if last:
            text = _clip(last, limit=240)
            state["last_assistant"] = text
            state["summary"] = text
        completed_at = payload.get("completed_at")
        if isinstance(completed_at, (int, float)):
            state["updated_at_ms"] = int(completed_at * 1000)
    elif msg_type in {"task_failed", "error"}:
        state["failed"] = True
        state["completed"] = True
        state["phase"] = SessionPhase.FAILED
        message = _optional_text(payload, "message", "error")
        if message:
            state["summary"] = _clip(message, limit=240)


def _command_from_function_call(payload: dict[str, Any]) -> str:
    raw_args = payload.get("arguments")
    if isinstance(raw_args, str) and raw_args.strip():
        try:
            decoded = json.loads(raw_args)
        except json.JSONDecodeError:
            return _clip(raw_args, limit=240)
        if isinstance(decoded, dict):
            command = _optional_text(decoded, "cmd", "command", "args")
            return _clip(command or "", limit=240)
    if isinstance(raw_args, dict):
        command = _optional_text(raw_args, "cmd", "command", "args")
        return _clip(command or "", limit=240)
    return ""


def _phase_for_tool(name: str, command: str) -> SessionPhase:
    normalized = name.strip().lower()
    command_lower = command.lower()
    if normalized in {"apply_patch", "edit", "write_file"}:
        return SessionPhase.EDITING
    if normalized in {"exec_command", "shell", "bash"} and any(
        needle in command_lower
        for needle in ("pytest", "swift test", "npm test", "pnpm test", "ruff", "xcodebuild test")
    ):
        return SessionPhase.TESTING
    return SessionPhase.RUNNING_TOOL


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return _clip(content, limit=240)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = _optional_text(item, "text")
            if text:
                parts.append(text)
    return _clip(" ".join(parts), limit=240)


def _timestamp_ms(value: object) -> int | None:
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


def _session_id_from_path(path: Path) -> str:
    name = path.stem
    parts = name.split("-")
    return parts[-1] if parts else name


def _title_from_cwd(cwd: object) -> str:
    if not isinstance(cwd, str) or not cwd:
        return ""
    return Path(cwd).name or cwd


def _optional_text(row: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _clip(value: str, *, limit: int = 180) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}..."


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _safe_mtime_ms(path: Path) -> int:
    return _safe_mtime_ns(path) // 1_000_000


__all__ = [
    "CodexTranscriptSummary",
    "CodexTranscriptWatcher",
    "default_codex_transcript_root",
    "discover_codex_transcripts",
    "events_from_codex_transcript",
    "parse_codex_transcript",
]
