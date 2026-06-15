"""Approval-gated durable memory suggestions.

The LLM may notice stable preferences in ordinary conversation, but it should
not silently write them into long-term profile memory. This module stores those
candidate facts as normal approvals; only an explicit Allow commits them.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from ..approvals import Approval, ApprovalDecision, ApprovalStore
from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..protocol.state import Priority
from .profile import ProfileStore

Clock = Callable[[], int]
IdFactory = Callable[[], str]

MEMORY_SUGGESTION_KIND = "memory_suggestion"


@dataclass(frozen=True)
class MemorySuggestion:
    key: str
    value: str
    reason: str = ""
    source: str = "llm"


def memory_suggestion_composer(
    *,
    approval_store: ApprovalStore | None = None,
    profile_store: ProfileStore | None = None,
    conversation_id: str = "default",
    clock: Clock | None = None,
    fallback: ReplyComposer | None = None,
) -> ReplyComposer:
    async def compose(text: str) -> str | None:
        reply = await fallback(text) if fallback is not None else None
        suggest_memory_from_text(
            text,
            approval_store=approval_store,
            profile_store=profile_store,
            conversation_id=conversation_id,
            now_ms=(clock or _default_clock)(),
        )
        return reply

    return compose


def memory_suggestion_streaming_composer(
    *,
    approval_store: ApprovalStore | None = None,
    profile_store: ProfileStore | None = None,
    conversation_id: str = "default",
    clock: Clock | None = None,
    fallback: StreamingReplyComposer | None = None,
) -> StreamingReplyComposer:
    async def compose(text: str) -> AsyncIterator[str]:
        if fallback is not None:
            async for chunk in fallback(text):
                yield chunk
        suggest_memory_from_text(
            text,
            approval_store=approval_store,
            profile_store=profile_store,
            conversation_id=conversation_id,
            now_ms=(clock or _default_clock)(),
        )

    return compose


def suggest_memory_from_text(
    text: str,
    *,
    approval_store: ApprovalStore | None,
    profile_store: ProfileStore | None,
    conversation_id: str = "default",
    now_ms: int | None = None,
) -> Approval | None:
    if approval_store is None or profile_store is None:
        return None
    suggestion = extract_memory_suggestion(text)
    if suggestion is None:
        return None
    if _memory_exists(profile_store, suggestion.key, suggestion.value):
        return None
    if _pending_memory_suggestion_exists(
        approval_store,
        suggestion.key,
        suggestion.value,
    ):
        return None
    return create_memory_suggestion_approval(
        suggestion,
        approval_store=approval_store,
        profile_store=profile_store,
        now_ms=now_ms if now_ms is not None else _default_clock(),
        session_id=f"deskmate-memory-{conversation_id}",
    )


def extract_memory_suggestion(text: str) -> MemorySuggestion | None:
    cleaned = _clean_fact_text(text)
    if not cleaned:
        return None
    if _looks_like_command(cleaned):
        return None

    for pattern in (
        re.compile(
            r"^(?:my|the)\s+(?P<key>[\w\s-]{2,48}?)\s+(?:is|=)\s+(?P<value>.+)$",
            re.I,
        ),
        re.compile(r"^我的(?P<key>[^是]{1,24})是(?P<value>.+)$", re.I),
    ):
        match = pattern.match(cleaned)
        if match:
            key = normalize_memory_key(match.group("key"))
            value = _clean_fact_text(match.group("value"))
            if _valid_candidate(key, value):
                return MemorySuggestion(
                    key=key,
                    value=value,
                    reason="User stated a stable personal fact.",
                    source="auto",
                )

    for pattern in (
        re.compile(
            r"^i\s+(?:usually\s+)?(?:use|work\s+with)\s+(?P<value>.+?)\s+for\s+(?P<context>[\w\s-]{2,48})$",
            re.I,
        ),
        re.compile(
            r"^i\s+prefer\s+(?P<value>.+?)\s+(?:for|when)\s+(?P<context>[\w\s-]{2,48})$",
            re.I,
        ),
    ):
        match = pattern.match(cleaned)
        if match:
            context = normalize_memory_key(match.group("context"))
            value = _clean_fact_text(match.group("value"))
            key = f"preferred_{context}" if context else "preference"
            if _valid_candidate(key, value):
                return MemorySuggestion(
                    key=key,
                    value=value,
                    reason="User stated a recurring tool or workflow preference.",
                    source="auto",
                )
    return None


def create_memory_suggestion_approval(
    suggestion: MemorySuggestion,
    *,
    approval_store: ApprovalStore,
    now_ms: int,
    profile_store: ProfileStore | None = None,
    approval_id: str | None = None,
    session_id: str | None = None,
    ttl_ms: int | None = 24 * 60 * 60 * 1000,
) -> Approval:
    key = normalize_memory_key(suggestion.key)
    value = suggestion.value.strip()
    if not key:
        raise ValueError("memory key is required")
    if not value:
        raise ValueError("memory value is required")

    existing = (
        _existing_memory(profile_store, key)
        if profile_store is not None
        else None
    )
    old_value = str(existing.get("value") or "").strip() if existing else ""
    operation = (
        "update"
        if old_value and old_value.lower() != value.lower()
        else "create"
    )
    prompt = (
        f"Update {key} from {old_value} to {value}?"
        if operation == "update"
        else f"Remember {key}: {value}?"
    )
    approval_id = approval_id or _default_memory_approval_id()
    approval = Approval(
        approval_id=approval_id,
        prompt=prompt,
        priority=Priority.P1,
        session_id=session_id,
        surface_id=f"approval:{approval_id}",
        created_at_ms=now_ms,
        expires_at_ms=now_ms + ttl_ms if ttl_ms is not None else None,
        extras={
            "kind": MEMORY_SUGGESTION_KIND,
            "memory_key": key,
            "memory_value": value,
            "memory_operation": operation,
            "memory_old_value": old_value,
            "memory_reason": suggestion.reason.strip(),
            "memory_source": suggestion.source.strip() or "llm",
        },
    )
    approval_store.add(approval)
    return approval


async def resolve_memory_suggestion(
    approval: Approval,
    *,
    profile_store: ProfileStore,
    clock: Clock | None = None,
) -> str | None:
    extras = approval.extras if isinstance(approval.extras, dict) else {}
    if extras.get("kind") != MEMORY_SUGGESTION_KIND:
        return None
    key = normalize_memory_key(str(extras.get("memory_key") or ""))
    value = str(extras.get("memory_value") or "").strip()
    if not key or not value:
        return "Memory suggestion was incomplete."
    operation = str(extras.get("memory_operation") or "create")
    old_value = str(extras.get("memory_old_value") or "").strip()
    if approval.decision is not ApprovalDecision.ALLOW:
        if operation == "update" and old_value:
            return f"Skipped memory update: {key} stays {old_value}."
        return f"Skipped memory: {key}."

    facts = memory_facts(profile_store)
    existing = facts.get(key)
    if not old_value and isinstance(existing, dict):
        old_value = str(existing.get("value") or "").strip()
    is_update = bool(old_value and old_value.lower() != value.lower())
    facts[key] = {
        "key": key,
        "value": value,
        "updated_at_ms": (clock or _default_clock)(),
        "source": str(extras.get("memory_source") or "approval"),
        "approved_at_ms": approval.resolved_at_ms,
        "approval_id": approval.approval_id,
    }
    reason = str(extras.get("memory_reason") or "").strip()
    if reason:
        facts[key]["reason"] = reason
    if is_update:
        facts[key]["previous_value"] = old_value
    profile_store.set("memories.facts", facts)
    await profile_store.flush()
    if is_update:
        return f"Updated {key}: {old_value} -> {value}."
    return f"Remembered {key}: {value}."


def memory_facts(profile_store: ProfileStore) -> dict[str, dict[str, Any]]:
    raw = profile_store.get("memories.facts", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, dict)
    }


def normalize_memory_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return key[:64]


def _memory_exists(profile_store: ProfileStore, key: str, value: str) -> bool:
    existing = _existing_memory(profile_store, key)
    if existing is None:
        return False
    return str(existing.get("value") or "").strip().lower() == value.strip().lower()


def _existing_memory(
    profile_store: ProfileStore,
    key: str,
) -> dict[str, Any] | None:
    facts = memory_facts(profile_store)
    existing = facts.get(normalize_memory_key(key))
    return existing if isinstance(existing, dict) else None


def _pending_memory_suggestion_exists(
    approval_store: ApprovalStore,
    key: str,
    value: str,
) -> bool:
    normalized = normalize_memory_key(key)
    needle = value.strip().lower()
    for approval in approval_store.list_pending():
        extras = approval.extras if isinstance(approval.extras, dict) else {}
        if extras.get("kind") != MEMORY_SUGGESTION_KIND:
            continue
        if normalize_memory_key(str(extras.get("memory_key") or "")) != normalized:
            continue
        if str(extras.get("memory_value") or "").strip().lower() == needle:
            return True
    return False


def _valid_candidate(key: str, value: str) -> bool:
    if not key or not value:
        return False
    if len(key) > 64 or len(value) > 160:
        return False
    return value.lower() not in {"it", "this", "that", "me", "you"}


def _looks_like_command(text: str) -> bool:
    lowered = text.lower()
    return lowered.startswith((
        "remember ",
        "forget ",
        "delete memory ",
        "remove memory ",
        "what do you remember ",
        "what did we discuss ",
        "search chat ",
        "记住",
        "忘记",
        "删除记忆",
    ))


def _clean_fact_text(value: str) -> str:
    return " ".join(value.strip().strip("\"'“”‘’。.!?？").split())


def _default_memory_approval_id() -> str:
    return "memory-suggestion-" + uuid.uuid4().hex[:12]


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


__all__ = [
    "MEMORY_SUGGESTION_KIND",
    "MemorySuggestion",
    "create_memory_suggestion_approval",
    "extract_memory_suggestion",
    "memory_facts",
    "memory_suggestion_composer",
    "memory_suggestion_streaming_composer",
    "normalize_memory_key",
    "resolve_memory_suggestion",
    "suggest_memory_from_text",
]
