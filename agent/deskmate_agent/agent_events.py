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
    command: str = ""
    file_path: str = ""


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
                    created_at_ms=event.ts_ms,
                    extras=_extras(event),
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
    return incoming_phase is SessionPhase.RUNNING and existing.phase in {
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
    if event.command:
        extras["command"] = event.command
    if event.file_path:
        extras["file_path"] = event.file_path
    return extras


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
