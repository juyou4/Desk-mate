"""Source-specific hook payload normalizers.

The public queue still stores one stable :class:`HookEvent` shape. This module
keeps vendor payload knowledge out of the file-queue watcher so Codex, Claude
Code, Cursor, and future agents can evolve independently.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sessions import SessionPhase


@dataclass(frozen=True)
class NormalizedHookFields:
    source: str
    event: str
    session_id: str
    title: str = ""
    summary: str = ""
    cwd: str | None = None
    jump_url: str | None = None
    phase: SessionPhase = SessionPhase.RUNNING
    approval_id: str | None = None
    prompt: str | None = None
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_source_hook(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    normalized_source = _normalize_source(source)
    if normalized_source == "codex":
        return _normalize_codex(raw, source=normalized_source)
    if normalized_source in {
        "claude",
        "claude_code",
        "qoder",
        "qwen",
        "qwen_code",
        "factory",
        "codebuddy",
        "kimi",
        "kimi_cli",
    }:
        return _normalize_claude_family(raw, source=normalized_source)
    if normalized_source == "cursor":
        return _normalize_cursor(raw, source=normalized_source)
    if normalized_source in {"opencode", "open_code"}:
        return _normalize_opencode(raw, source="opencode")
    return _normalize_generic(raw, source=normalized_source)


def _normalize_codex(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    event = _string_from(raw, "hook_event_name", "event", "type", default="session.updated")
    cwd = _optional_string_from(raw, "cwd", "working_directory", "workingDirectory", "workspace")
    workspace = _workspace_name(cwd)
    session_id = _session_id(raw, source=source)
    tool_name = _optional_string_from(raw, "tool_name", "tool", "name")
    command = _command_from(raw)
    prompt = _optional_string_from(raw, "prompt", "question")
    assistant = _optional_string_from(raw, "last_assistant_message", "summary", "message")

    title = _string_from(raw, "title", "name", default="")
    if not title:
        title = f"Codex · {workspace}" if workspace else "Codex session"

    summary = _string_from(raw, "summary", "message", "detail", default="")
    if not summary:
        summary = _codex_summary(event, prompt=prompt, assistant=assistant, tool_name=tool_name, command=command)

    phase = _phase_from(raw, event=event)
    approval_id = _optional_string_from(raw, "approval_id", "approvalId")
    if phase is SessionPhase.WAITING_FOR_APPROVAL and approval_id is None:
        approval_id = _optional_string_from(raw, "tool_use_id", "toolUseID") or f"{session_id}-approval"

    return NormalizedHookFields(
        source=source,
        event=event,
        session_id=session_id,
        title=title,
        summary=summary,
        cwd=cwd,
        jump_url=_optional_string_from(raw, "jump_url", "jumpUrl", "url"),
        phase=phase,
        approval_id=approval_id,
        prompt=prompt,
        ts_ms=_int_from(raw, "ts_ms", "timestamp_ms", "time_ms", default=_now_ms()),
        raw=raw,
    )


def _normalize_claude_family(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    event = _string_from(raw, "hook_event_name", "event", "type", default="session.updated")
    cwd = _optional_string_from(raw, "cwd", "working_directory", "workingDirectory", "workspace")
    session_id = _session_id(raw, source=source)
    tool_name = _optional_string_from(raw, "tool_name", "tool", "name")
    command = _command_from(raw)
    prompt = _optional_string_from(raw, "prompt", "question", "message")
    title = _string_from(raw, "title", default="")
    if not title:
        title = f"{_display_source(source)} · {_workspace_name(cwd)}" if cwd else f"{_display_source(source)} session"

    summary = _string_from(raw, "summary", "message", "detail", "last_assistant_message", default="")
    if not summary:
        summary = _tool_summary(_display_source(source), event=event, tool_name=tool_name, command=command, prompt=prompt)

    phase = _phase_from(raw, event=event)
    event_key = event.lower().replace("-", "_")
    if event_key in {"permissionrequest", "permission_request"}:
        phase = SessionPhase.WAITING_FOR_APPROVAL
    elif event_key in {"questionasked", "question_asked"}:
        phase = SessionPhase.WAITING_FOR_ANSWER
    elif event_key in {"sessionend", "session_end", "stop"}:
        phase = SessionPhase.COMPLETED
    elif "failure" in event_key:
        phase = SessionPhase.FAILED

    approval_id = _optional_string_from(raw, "approval_id", "approvalId", "tool_use_id", "toolUseID")
    if phase is SessionPhase.WAITING_FOR_APPROVAL and approval_id is None:
        approval_id = f"{session_id}-approval"

    return NormalizedHookFields(
        source=source,
        event=event,
        session_id=session_id,
        title=title,
        summary=summary,
        cwd=cwd,
        jump_url=_optional_string_from(raw, "jump_url", "jumpUrl", "url"),
        phase=phase,
        approval_id=approval_id,
        prompt=prompt,
        ts_ms=_int_from(raw, "ts_ms", "timestamp_ms", "time_ms", default=_now_ms()),
        raw=raw,
    )


def _normalize_cursor(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    event = _string_from(raw, "hook_event_name", "event", "type", default="session.updated")
    workspace_roots = raw.get("workspace_roots")
    primary_workspace = ""
    if isinstance(workspace_roots, list) and workspace_roots:
        primary_workspace = str(workspace_roots[0])
    cwd = _optional_string_from(raw, "cwd") or primary_workspace or None
    conversation_id = _optional_string_from(raw, "conversation_id", "conversationId")
    session_id = conversation_id or _session_id(raw, source=source)
    prompt = _optional_string_from(raw, "prompt", "content")
    command = _optional_string_from(raw, "command", "cmd")
    tool_name = _optional_string_from(raw, "tool_name", "toolName", "server")

    phase = _cursor_phase(raw, event=event)
    title = _string_from(raw, "title", default="")
    if not title:
        title = f"Cursor · {_workspace_name(cwd)}" if cwd else "Cursor session"

    summary = _string_from(raw, "summary", "message", "content", default="")
    if not summary:
        summary = _tool_summary("Cursor", event=event, tool_name=tool_name, command=command, prompt=prompt)

    return NormalizedHookFields(
        source=source,
        event=event,
        session_id=session_id,
        title=title,
        summary=summary,
        cwd=cwd,
        jump_url=_optional_string_from(raw, "jump_url", "jumpUrl", "url"),
        phase=phase,
        approval_id=_optional_string_from(raw, "approval_id", "approvalId", "generation_id", "generationId"),
        prompt=prompt,
        ts_ms=_int_from(raw, "ts_ms", "timestamp_ms", "time_ms", default=_now_ms()),
        raw=raw,
    )


def _normalize_opencode(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    fields = _normalize_claude_family(raw, source=source)
    event = fields.event.lower().replace("-", "_")
    if event in {"permissionrequest", "permission_request"}:
        return fields.__class__(**{**fields.__dict__, "phase": SessionPhase.WAITING_FOR_APPROVAL})
    if event in {"questionasked", "question_asked"}:
        return fields.__class__(**{**fields.__dict__, "phase": SessionPhase.WAITING_FOR_ANSWER})
    return fields


def _normalize_generic(raw: dict[str, Any], *, source: str) -> NormalizedHookFields:
    event = _string_from(raw, "event", "type", "hook_event_name", default="session.updated")
    return NormalizedHookFields(
        source=source,
        event=event,
        session_id=_session_id(raw, source=source),
        title=_string_from(raw, "title", "name", "task", "prompt", default=""),
        summary=_string_from(raw, "summary", "message", "detail", default=""),
        cwd=_optional_string_from(raw, "cwd", "working_directory", "workingDirectory", "workspace"),
        jump_url=_optional_string_from(raw, "jump_url", "jumpUrl", "url"),
        phase=_phase_from(raw, event=event),
        approval_id=_optional_string_from(raw, "approval_id", "approvalId"),
        prompt=_optional_string_from(raw, "prompt", "question"),
        ts_ms=_int_from(raw, "ts_ms", "timestamp_ms", "time_ms", default=_now_ms()),
        raw=raw,
    )


def _phase_from(raw: dict[str, Any], *, event: str) -> SessionPhase:
    phase_raw = _optional_string_from(raw, "phase", "status")
    if phase_raw:
        normalized = phase_raw.lower().replace("-", "_").replace(".", "_")
        try:
            return SessionPhase(normalized)
        except ValueError:
            pass
    text = _event_text(raw, event=event)
    tool_text = _optional_string_from(raw, "tool", "tool_name", "toolName", "name") or ""
    tool_text = tool_text.lower()
    command_text = _command_from(raw) or ""
    command_text = command_text.lower()

    if any(token in text for token in ("approval", "permission", "pretooluse")):
        return SessionPhase.WAITING_FOR_APPROVAL
    if any(token in text for token in ("question", "ask", "answer")):
        return SessionPhase.WAITING_FOR_ANSWER
    if any(token in text for token in ("failed", "failure", "error", "denied")):
        return SessionPhase.FAILED
    if any(
        token in text
        for token in (
            "done",
            "complete",
            "completed",
            "stop",
            "finish",
            "finished",
            "sessionend",
            "session_end",
            "session.completed",
            "tool.end",
            "tool_end",
            "posttooluse",
        )
    ):
        return SessionPhase.COMPLETED
    if any(token in text for token in ("thinking", "thought", "reasoning", "userpromptsubmit")):
        return SessionPhase.THINKING
    if any(
        token in text or token in tool_text
        for token in ("edit", "write", "patch", "multiedit", "strreplace", "afterfileedit")
    ):
        return SessionPhase.EDITING
    if any(
        token in text or token in tool_text or token in command_text
        for token in ("test", "pytest", "swift test", "npm test", "cargo test")
    ):
        return SessionPhase.TESTING
    if any(
        token in text or token in tool_text
        for token in ("tool", "bash", "exec", "command", "shell", "read", "grep", "search")
    ):
        return SessionPhase.RUNNING_TOOL
    return SessionPhase.RUNNING


def _cursor_phase(raw: dict[str, Any], *, event: str) -> SessionPhase:
    key = event.lower().replace("-", "_")
    if key == "beforesubmitprompt":
        return SessionPhase.THINKING
    if key in {"beforeshellexecution", "beforemcpexecution", "beforereadfile"}:
        command = _optional_string_from(raw, "command", "cmd") or ""
        if any(token in command.lower() for token in ("test", "pytest", "swift test", "npm test", "cargo test")):
            return SessionPhase.TESTING
        return SessionPhase.RUNNING_TOOL
    if key == "afterfileedit":
        return SessionPhase.EDITING
    if key == "stop":
        return SessionPhase.COMPLETED
    return _phase_from(raw, event=event)


def _codex_summary(
    event: str,
    *,
    prompt: str | None,
    assistant: str | None,
    tool_name: str | None,
    command: str | None,
) -> str:
    key = event.lower().replace("-", "_")
    if key == "sessionstart":
        return "Codex session started."
    if key == "userpromptsubmit":
        return _clip(prompt) or "Codex received a prompt."
    if key == "posttooluse":
        return "Codex finished a tool call."
    if key == "stop":
        return _clip(assistant) or "Codex turn completed."
    if key == "pretooluse" or "tool" in key or command or tool_name:
        return _tool_summary("Codex", event=event, tool_name=tool_name, command=command, prompt=prompt)
    return _clip(prompt) or _clip(assistant) or "Codex session updated."


def _tool_summary(
    display: str,
    *,
    event: str,
    tool_name: str | None,
    command: str | None,
    prompt: str | None,
) -> str:
    if command:
        return f"{display} is running: {_clip(command, limit=120)}"
    if tool_name:
        return f"{display} is using {tool_name}."
    if prompt:
        return _clip(prompt) or f"{display} session updated."
    return f"{display} session updated ({event})."


def _session_id(raw: dict[str, Any], *, source: str) -> str:
    session_id = _optional_string_from(
        raw,
        "session_id",
        "sessionId",
        "thread_id",
        "threadId",
        "conversation_id",
        "conversationId",
        "id",
    )
    if session_id:
        return session_id
    digest = hashlib.sha1(
        json.dumps(raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return f"{source}-{digest}"


def _command_from(raw: dict[str, Any]) -> str | None:
    direct = _optional_string_from(raw, "command", "cmd", "args")
    if direct:
        return direct
    tool_input = raw.get("tool_input") or raw.get("toolInput")
    if isinstance(tool_input, dict):
        return _optional_string_from(tool_input, "command", "cmd", "args")
    if isinstance(tool_input, str) and tool_input.strip():
        return tool_input.strip()
    return None


def _string_from(raw: dict[str, Any], *keys: str, default: str) -> str:
    found = _optional_string_from(raw, *keys)
    return found if found is not None else default


def _optional_string_from(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _int_from(raw: dict[str, Any], *keys: str, default: int) -> int:
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return default


def _event_text(raw: dict[str, Any], *, event: str) -> str:
    fields = [
        event,
        raw.get("type", ""),
        raw.get("event", ""),
        raw.get("hook_event_name", ""),
        raw.get("status", ""),
        raw.get("message", ""),
        raw.get("summary", ""),
    ]
    return " ".join(str(item) for item in fields if item is not None).lower()


def _workspace_name(cwd: str | None) -> str:
    if not cwd:
        return ""
    return Path(cwd).name or cwd


def _display_source(source: str) -> str:
    return {
        "claude": "Claude",
        "claude_code": "Claude",
        "qoder": "Qoder",
        "qwen": "Qwen",
        "qwen_code": "Qwen",
        "factory": "Factory",
        "codebuddy": "CodeBuddy",
        "kimi": "Kimi",
        "kimi_cli": "Kimi",
        "opencode": "OpenCode",
    }.get(source, source.replace("_", " ").title())


def _normalize_source(source: str) -> str:
    normalized = source.strip().lower().replace("-", "_")
    if normalized == "claude_code_cli":
        return "claude_code"
    if normalized == "open_code":
        return "opencode"
    return normalized


def _clip(value: str | None, *, limit: int = 160) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 1]}…"


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = ["NormalizedHookFields", "normalize_source_hook"]
