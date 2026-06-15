"""Approval-gated durable task suggestions."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from ..approvals import Approval, ApprovalDecision, ApprovalStore
from ..protocol.state import Priority
from .tasks import DeskmateTaskStatus, DeskmateTaskStore, format_deskmate_task

Clock = Callable[[], int]

TASK_SUGGESTION_KIND = "task_suggestion"
_VALID_STATUSES: set[str] = {"open", "in_progress", "done", "cancelled"}


@dataclass(frozen=True)
class TaskSuggestion:
    title: str
    notes: str = ""
    status: DeskmateTaskStatus = "open"
    reason: str = ""
    source: str = "llm"


def create_task_suggestion_approval(
    suggestion: TaskSuggestion,
    *,
    approval_store: ApprovalStore,
    now_ms: int,
    conversation_id: str = "default",
    approval_id: str | None = None,
    session_id: str | None = None,
    ttl_ms: int | None = 24 * 60 * 60 * 1000,
) -> Approval:
    title = _compact_text(suggestion.title, limit=160)
    notes = _compact_text(suggestion.notes, limit=2_000)
    status = _normalize_status(suggestion.status)
    if not title:
        raise ValueError("task title is required")
    if _pending_task_suggestion_exists(approval_store, title, conversation_id):
        existing = _pending_task_suggestion(
            approval_store,
            title,
            conversation_id,
        )
        if existing is not None:
            return existing

    approval_id = approval_id or _default_task_approval_id()
    approval = Approval(
        approval_id=approval_id,
        prompt=f"Add task: {title}?",
        priority=Priority.P1,
        session_id=session_id,
        surface_id=f"approval:{approval_id}",
        created_at_ms=now_ms,
        expires_at_ms=now_ms + ttl_ms if ttl_ms is not None else None,
        extras={
            "kind": TASK_SUGGESTION_KIND,
            "task_title": title,
            "task_notes": notes,
            "task_status": status,
            "task_reason": suggestion.reason.strip(),
            "task_source": suggestion.source.strip() or "llm",
            "conversation_id": _compact_text(conversation_id, limit=120) or "default",
        },
    )
    approval_store.add(approval)
    return approval


async def resolve_task_suggestion(
    approval: Approval,
    *,
    task_store: DeskmateTaskStore,
    clock: Clock | None = None,
) -> str | None:
    extras = approval.extras if isinstance(approval.extras, dict) else {}
    if extras.get("kind") != TASK_SUGGESTION_KIND:
        return None
    title = _compact_text(extras.get("task_title"), limit=160)
    notes = _compact_text(extras.get("task_notes"), limit=2_000)
    conversation_id = _compact_text(extras.get("conversation_id"), limit=120) or "default"
    if not title:
        return "Task suggestion was incomplete."
    if approval.decision is not ApprovalDecision.ALLOW:
        return f"Skipped task: {title}."

    try:
        status = _normalize_status(str(extras.get("task_status") or "open"))
    except ValueError:
        status = "open"
    task = await task_store.create(
        conversation_id=conversation_id,
        title=title,
        notes=notes,
        status=status,
        created_at_ms=(clock or _default_clock)(),
    )
    return "Task created:\n" + format_deskmate_task(task)


def _pending_task_suggestion_exists(
    approval_store: ApprovalStore,
    title: str,
    conversation_id: str,
) -> bool:
    return _pending_task_suggestion(approval_store, title, conversation_id) is not None


def _pending_task_suggestion(
    approval_store: ApprovalStore,
    title: str,
    conversation_id: str,
) -> Approval | None:
    needle = _compact_text(title, limit=160).lower()
    conversation = _compact_text(conversation_id, limit=120) or "default"
    for approval in approval_store.list_pending():
        extras = approval.extras if isinstance(approval.extras, dict) else {}
        if extras.get("kind") != TASK_SUGGESTION_KIND:
            continue
        if _compact_text(extras.get("conversation_id"), limit=120) != conversation:
            continue
        if _compact_text(extras.get("task_title"), limit=160).lower() == needle:
            return approval
    return None


def _normalize_status(value: object) -> DeskmateTaskStatus:
    status = str(value or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise ValueError("status must be open, in_progress, done, or cancelled")
    return status  # type: ignore[return-value]


def _compact_text(value: object, *, limit: int) -> str:
    import re

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def _default_task_approval_id() -> str:
    return "task-suggestion-" + uuid.uuid4().hex[:12]


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


__all__ = [
    "TASK_SUGGESTION_KIND",
    "TaskSuggestion",
    "create_task_suggestion_approval",
    "resolve_task_suggestion",
]
