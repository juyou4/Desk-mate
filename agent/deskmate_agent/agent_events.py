"""Agent event reducer for island/session state.

External sources (hooks, app-server, transcript discovery) should map their
native payloads into these events first. The reducer is then the single place
that mutates live session and approval stores.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_phase import presentation_for_phase
from .agent_runtime import AgentRuntimeKind
from .approvals import Approval, ApprovalStore
from .protocol.state import Priority
from .sessions import SessionInfo, SessionPhase, SessionState, SessionStore


@dataclass(frozen=True)
class AgentEventBase:
    session_id: str
    source: str
    ts_ms: int
    title: str = ""
    summary: str = ""
    cwd: str | None = None
    jump_url: str | None = None
    raw_event: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_result: str = ""
    tool_result_id: str = ""
    tool_action: str = ""
    tool_target: str = ""
    tool_outcome: str = ""
    tool_needs_user: str = ""
    tool_summary: str = ""
    tool_task_id: str = ""
    tool_task_status: str = ""
    tool_task_summary: str = ""
    command: str = ""
    file_path: str = ""
    last_user: str = ""
    last_assistant: str = ""


@dataclass(frozen=True)
class SessionStarted(AgentEventBase):
    phase: SessionPhase = SessionPhase.RUNNING


@dataclass(frozen=True)
class SessionActivityUpdated(AgentEventBase):
    phase: SessionPhase = SessionPhase.RUNNING


@dataclass(frozen=True)
class PermissionRequested(AgentEventBase):
    approval_id: str = ""
    prompt: str = ""


@dataclass(frozen=True)
class QuestionAsked(AgentEventBase):
    prompt: str = ""


@dataclass(frozen=True)
class SessionCompleted(AgentEventBase):
    failed: bool = False


@dataclass(frozen=True)
class JumpTargetUpdated(AgentEventBase):
    pass


AgentEvent = (
    SessionStarted
    | SessionActivityUpdated
    | PermissionRequested
    | QuestionAsked
    | SessionCompleted
    | JumpTargetUpdated
)


class AgentEventReducer:
    def __init__(
        self,
        *,
        session_store: SessionStore,
        approval_store: ApprovalStore,
    ) -> None:
        self._sessions = session_store
        self._approvals = approval_store

    def apply(self, event: AgentEvent) -> None:
        if isinstance(event, SessionStarted):
            self._upsert_session(
                event,
                phase=event.phase,
                priority=_priority_for_phase(event.phase),
                state=SessionState.ACTIVE,
            )
            return

        if isinstance(event, SessionActivityUpdated):
            existing = self._sessions.get(event.session_id)
            if existing is None:
                self._upsert_session(
                    event,
                    phase=event.phase,
                    priority=_priority_for_phase(event.phase),
                    state=SessionState.ACTIVE,
                )
                return
            if _preserves_actionable_state(existing, event.phase):
                self._upsert_session(
                    event,
                    phase=existing.phase,
                    priority=existing.priority,
                    state=SessionState.ACTIVE,
                )
            else:
                self._upsert_session(
                    event,
                    phase=event.phase,
                    priority=_priority_for_phase(event.phase),
                    state=SessionState.ACTIVE,
                )
            return

        if isinstance(event, PermissionRequested):
            approval_id = event.approval_id or f"{event.session_id}-approval"
            self._upsert_session(
                event,
                phase=SessionPhase.WAITING_FOR_APPROVAL,
                priority=Priority.P0,
                state=SessionState.ACTIVE,
                summary=event.prompt or event.summary,
            )
            self._approvals.add(
                Approval(
                    approval_id=approval_id,
                    prompt=event.prompt or event.summary or "Agent is waiting for approval.",
                    priority=Priority.P0,
                    session_id=event.session_id,
                    surface_id=f"approval:{approval_id}",
                    created_at_ms=event.ts_ms,
                    extras=_approval_extras(event),
                )
            )
            return

        if isinstance(event, QuestionAsked):
            self._upsert_session(
                event,
                phase=SessionPhase.WAITING_FOR_ANSWER,
                priority=Priority.P0,
                state=SessionState.ACTIVE,
                summary=event.prompt or event.summary,
            )
            return

        if isinstance(event, SessionCompleted):
            self._upsert_session(
                event,
                phase=SessionPhase.FAILED if event.failed else SessionPhase.COMPLETED,
                priority=Priority.P2,
                state=SessionState.ACTIVE,
            )
            return

        if isinstance(event, JumpTargetUpdated):
            existing = self._sessions.get(event.session_id)
            if existing is None:
                self._upsert_session(
                    event,
                    phase=SessionPhase.RUNNING,
                    priority=Priority.P1,
                    state=SessionState.ACTIVE,
                )
                return
            self._sessions.upsert(
                existing.model_copy(
                    update={
                        "updated_at_ms": event.ts_ms,
                        "cwd": event.cwd or existing.cwd,
                        "jump_url": event.jump_url or existing.jump_url,
                        "extras": {**(existing.extras or {}), **_extras(event)},
                    }
                )
            )
            return

        raise TypeError(f"unsupported agent event: {event!r}")

    def _upsert_session(
        self,
        event: AgentEventBase,
        *,
        phase: SessionPhase,
        priority: Priority,
        state: SessionState,
        summary: str | None = None,
    ) -> None:
        existing = self._sessions.get(event.session_id)
        existing_extras = existing.extras if existing is not None else {}
        self._sessions.upsert(
            SessionInfo(
                session_id=event.session_id,
                title=event.title or (existing.title if existing is not None else _fallback_title(event)),
                summary=summary if summary is not None else (event.summary or (existing.summary if existing else "")),
                state=state,
                priority=priority,
                created_at_ms=existing.created_at_ms if existing is not None else event.ts_ms,
                updated_at_ms=event.ts_ms,
                phase=phase,
                cwd=event.cwd or (existing.cwd if existing is not None else None),
                jump_url=event.jump_url or (existing.jump_url if existing is not None else None),
                source=event.source,
                kind=AgentRuntimeKind.HOOK_SESSION.value,
                extras={**existing_extras, **_extras(event)},
            )
        )


def event_from_hook(hook: object) -> AgentEvent:
    """Convert a normalized HookEvent-like object into an AgentEvent."""

    session_id = hook.session_id  # type: ignore[attr-defined]
    source = hook.source  # type: ignore[attr-defined]
    ts_ms = hook.ts_ms  # type: ignore[attr-defined]
    base = {
        "session_id": session_id,
        "source": source,
        "ts_ms": ts_ms,
        "title": getattr(hook, "title", ""),
        "summary": getattr(hook, "summary", ""),
        "cwd": getattr(hook, "cwd", None),
        "jump_url": getattr(hook, "jump_url", None),
        "raw_event": getattr(hook, "event", ""),
        "tool_name": _raw_string(getattr(hook, "raw", {}), "tool_name", "tool", "name"),
        "command": _raw_command(getattr(hook, "raw", {})),
        "file_path": _raw_string(
            getattr(hook, "raw", {}),
            "file_path",
            "filePath",
            "path",
        ),
    }
    phase = getattr(hook, "phase", SessionPhase.RUNNING)
    event_name = str(getattr(hook, "event", "")).lower().replace("-", "_")
    prompt = getattr(hook, "prompt", None) or ""

    if phase is SessionPhase.WAITING_FOR_APPROVAL or getattr(hook, "approval_id", None):
        return PermissionRequested(
            **base,
            approval_id=getattr(hook, "approval_id", None) or "",
            prompt=prompt,
        )
    if phase is SessionPhase.WAITING_FOR_ANSWER:
        return QuestionAsked(**base, prompt=prompt)
    if phase in {SessionPhase.COMPLETED, SessionPhase.FAILED}:
        return SessionCompleted(
            **base,
            failed=phase is SessionPhase.FAILED,
        )
    if event_name in {"sessionstart", "session_start", "session.started", "session_started"}:
        return SessionStarted(**base, phase=phase)
    if (
        getattr(hook, "cwd", None) or getattr(hook, "jump_url", None)
    ) and event_name in {"jump_target_updated", "jump.updated", "jump_target"}:
        return JumpTargetUpdated(**base)
    return SessionActivityUpdated(**base, phase=phase)


def _priority_for_phase(phase: SessionPhase) -> Priority:
    return presentation_for_phase(phase, source="agent").priority


def _preserves_actionable_state(existing: SessionInfo, incoming_phase: SessionPhase) -> bool:
    # V10 runtime-phase-observers Property 1 / Requirement 5.4 —
    # passive observers may not silently downgrade an actionable
    # session by emitting any "informational" phase. The original
    # guard only covered RUNNING; the runtime-phase-observers spec
    # broadens this to every non-actionable, non-terminal phase a
    # ``SessionActivityUpdated`` event can carry. Hook-driven
    # transitions remain authoritative because hooks emit explicit
    # ``PermissionRequested`` / ``QuestionAsked`` / ``SessionCompleted``
    # events, which the reducer routes through dedicated branches
    # that bypass this guard.
    informational = {
        SessionPhase.RUNNING,
        SessionPhase.THINKING,
        SessionPhase.EDITING,
        SessionPhase.RUNNING_TOOL,
        SessionPhase.TESTING,
    }
    return incoming_phase in informational and existing.phase in {
        SessionPhase.WAITING_FOR_APPROVAL,
        SessionPhase.WAITING_FOR_ANSWER,
    }


def _extras(event: AgentEventBase) -> dict[str, str]:
    extras = {
        "hook_source": event.source,
        "hook_event": event.raw_event,
    }
    if event.tool_name:
        extras["tool_name"] = event.tool_name
    if event.tool_id:
        extras["tool_id"] = event.tool_id
    if event.tool_result:
        extras["tool_result"] = event.tool_result
    if event.tool_result_id:
        extras["tool_result_id"] = event.tool_result_id
    if event.tool_action:
        extras["tool_action"] = event.tool_action
    if event.tool_target:
        extras["tool_target"] = event.tool_target
    if event.tool_outcome:
        extras["tool_outcome"] = event.tool_outcome
    if event.tool_needs_user:
        extras["tool_needs_user"] = event.tool_needs_user
    if event.tool_summary:
        extras["tool_summary"] = event.tool_summary
    if event.tool_task_id:
        extras["tool_task_id"] = event.tool_task_id
    if event.tool_task_status:
        extras["tool_task_status"] = event.tool_task_status
    if event.tool_task_summary:
        extras["tool_task_summary"] = event.tool_task_summary
    if event.command:
        extras["command"] = event.command
    if event.file_path:
        extras["file_path"] = event.file_path
    if event.last_user:
        extras["last_user"] = event.last_user
    if event.last_assistant:
        extras["last_assistant"] = event.last_assistant
    return extras


def _approval_extras(event: PermissionRequested) -> dict[str, str]:
    extras = _extras(event)
    risk_level, risk_summary = _approval_risk(event)
    extras["risk_level"] = risk_level
    extras["risk_summary"] = risk_summary
    if event.command:
        extras["approval_preview"] = f"cmd: {_clip(event.command, 180)}"
    elif event.file_path:
        extras["approval_preview"] = f"file: {event.file_path}"
    elif event.tool_name:
        extras["approval_preview"] = f"tool: {event.tool_name}"
    return extras


def _approval_risk(event: AgentEventBase) -> tuple[str, str]:
    tool = event.tool_name.strip().lower()
    command = event.command.strip()
    command_lower = command.lower()
    file_path = event.file_path.strip()
    destructive_tokens = (
        "rm ",
        "rm -",
        "mv ",
        "chmod ",
        "chown ",
        "sudo ",
        "git reset",
        "git clean",
        "kill ",
        "pkill ",
    )

    if tool in {"bash", "shell", "terminal", "run_shell"} or command:
        if any(token in f" {command_lower} " for token in destructive_tokens):
            return "high", "Shell command may modify files, processes, or permissions."
        return "medium", "Shell command can affect the local workspace."

    if tool in {"edit", "write", "multiedit"} or file_path:
        return "medium", "Tool may modify a local file."

    if tool in {"read", "grep", "glob", "ls", "search"}:
        return "low", "Read-only tool request."

    if event.tool_name:
        return "medium", f"Agent requested permission to use {event.tool_name}."
    return "medium", "Agent requested permission before continuing."


def _clip(value: str, max_len: int) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _raw_string(raw: object, *keys: str) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in keys:
        value = raw.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _raw_command(raw: object) -> str:
    direct = _raw_string(raw, "command", "cmd", "args")
    if direct:
        return direct
    if not isinstance(raw, dict):
        return ""
    tool_input = raw.get("tool_input") or raw.get("toolInput")
    if isinstance(tool_input, dict):
        return _raw_string(tool_input, "command", "cmd", "args")
    if isinstance(tool_input, str):
        return tool_input.strip()
    return ""


def _fallback_title(event: AgentEventBase) -> str:
    display = event.source.replace("_", " ").title()
    return f"{display} session"


__all__ = [
    "AgentEvent",
    "AgentEventReducer",
    "JumpTargetUpdated",
    "PermissionRequested",
    "QuestionAsked",
    "SessionActivityUpdated",
    "SessionCompleted",
    "SessionStarted",
    "event_from_hook",
]
