"""Deterministic memory control for explicit user requests.

This layer handles clear "remember this" / "what did we discuss" commands
before the LLM path. It keeps the baseline useful without an API key and avoids
depending on the model to choose a memory tool for unambiguous requests.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..memory import ChatMemory, ProfileStore

Clock = Callable[[], int]
MemoryKind = Literal[
    "remember_fact",
    "recall_fact",
    "list_facts",
    "forget_fact",
    "search_chat",
]


@dataclass(frozen=True)
class MemoryRequest:
    kind: MemoryKind
    text: str


_REMEMBER_PATTERNS = (
    re.compile(r"^(?:please\s+)?remember\s+(?:that\s+)?(?P<text>.+)$", re.I),
    re.compile(r"^(?:can you\s+)?remember\s+(?:that\s+)?(?P<text>.+)$", re.I),
    re.compile(r"^(?:记住|帮我记住|请记住)\s*(?P<text>.+)$", re.I),
)
_RECALL_FACT_PATTERNS = (
    re.compile(r"^(?:what do you remember about|recall memory about|remember anything about)\s+(?P<text>.+)$", re.I),
    re.compile(r"^(?:你记得|记得|回忆一下|回忆)\s*(?:关于)?\s*(?P<text>.+?)(?:\s*吗)?$", re.I),
)
_LIST_FACT_PATTERNS = (
    re.compile(r"^(?:what do you remember|what do you know about me|list memories|list my memories|show memories|show my memories)\??$", re.I),
    re.compile(r"^(?:你记得我什么|你都记得什么|你知道我什么|列出记忆|显示记忆|有什么记忆)\??$", re.I),
)
_FORGET_FACT_PATTERNS = (
    re.compile(r"^(?:forget|delete memory about|remove memory about|forget memory about)\s+(?P<text>.+)$", re.I),
    re.compile(r"^(?:忘记|删除记忆|删掉记忆|不要记住)\s*(?:关于)?\s*(?P<text>.+)$", re.I),
)
_SEARCH_CHAT_PATTERNS = (
    re.compile(r"^(?:what did we discuss about|what did i say about|search chat for|search our chat for)\s+(?P<text>.+)$", re.I),
    re.compile(r"^(?:之前|刚刚|前面)\s*(?:聊过|说过|提到过)\s*(?P<text>.+?)(?:\s*吗)?$", re.I),
)


def memory_control_composer(
    *,
    profile_store: ProfileStore | None = None,
    chat_memory: ChatMemory | None = None,
    conversation_id: str = "default",
    clock: Clock | None = None,
    fallback: ReplyComposer | None = None,
) -> ReplyComposer:
    async def compose(text: str) -> str | None:
        request = parse_memory_request(text)
        if request is None:
            return await fallback(text) if fallback is not None else None
        if request.kind == "remember_fact":
            return await remember_fact(
                request.text,
                profile_store=profile_store,
                clock=clock,
            )
        if request.kind == "recall_fact":
            return recall_fact(request.text, profile_store=profile_store)
        if request.kind == "list_facts":
            return list_facts(profile_store=profile_store)
        if request.kind == "forget_fact":
            return await forget_fact(request.text, profile_store=profile_store)
        return await search_chat_memory(
            request.text,
            chat_memory=chat_memory,
            conversation_id=conversation_id,
        )

    return compose


def memory_control_streaming_composer(
    *,
    profile_store: ProfileStore | None = None,
    chat_memory: ChatMemory | None = None,
    conversation_id: str = "default",
    clock: Clock | None = None,
    fallback: StreamingReplyComposer | None = None,
) -> StreamingReplyComposer:
    async def compose(text: str) -> AsyncIterator[str]:
        request = parse_memory_request(text)
        if request is not None:
            if request.kind == "remember_fact":
                yield await remember_fact(
                    request.text,
                    profile_store=profile_store,
                    clock=clock,
                )
                return
            if request.kind == "recall_fact":
                yield recall_fact(request.text, profile_store=profile_store)
                return
            if request.kind == "list_facts":
                yield list_facts(profile_store=profile_store)
                return
            if request.kind == "forget_fact":
                yield await forget_fact(request.text, profile_store=profile_store)
                return
            yield await search_chat_memory(
                request.text,
                chat_memory=chat_memory,
                conversation_id=conversation_id,
            )
            return
        if fallback is None:
            return
        async for chunk in fallback(text):
            yield chunk

    return compose


def parse_memory_request(text: str) -> MemoryRequest | None:
    stripped = " ".join(text.strip().split())
    if not stripped:
        return None
    for pattern in _LIST_FACT_PATTERNS:
        if pattern.match(stripped):
            return MemoryRequest("list_facts", "")
    for pattern in _SEARCH_CHAT_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("text"))
            return MemoryRequest("search_chat", query) if query else None
    for pattern in _RECALL_FACT_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("text"))
            return MemoryRequest("recall_fact", query) if query else None
    for pattern in _FORGET_FACT_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_text(match.group("text"))
            return MemoryRequest("forget_fact", query) if query else None
    for pattern in _REMEMBER_PATTERNS:
        match = pattern.match(stripped)
        if match:
            fact = _clean_text(match.group("text"))
            if fact and fact.lower() not in {"me", "this", "that"}:
                return MemoryRequest("remember_fact", fact)
    return None


async def remember_fact(
    fact: str,
    *,
    profile_store: ProfileStore | None,
    clock: Clock | None = None,
) -> str:
    if profile_store is None:
        return "I can remember that once profile memory is ready."
    key, value = _fact_key_value(fact)
    if not key or not value:
        return "Tell me the specific fact to remember."
    facts = _memory_facts(profile_store)
    facts[key] = {
        "key": key,
        "value": value,
        "updated_at_ms": (clock or _default_clock)(),
        "source": "memory_control",
    }
    profile_store.set("memories.facts", facts)
    await profile_store.flush()
    return f"Remembered {key}: {value}."


def recall_fact(query: str, *, profile_store: ProfileStore | None) -> str:
    if profile_store is None:
        return "I can recall memories once profile memory is ready."
    needle = query.strip().lower()
    if not needle:
        return "Tell me what to search for."
    matches: list[dict[str, Any]] = []
    for fact in _memory_facts(profile_store).values():
        key = str(fact.get("key") or "")
        value = str(fact.get("value") or "")
        if needle in f"{key} {value}".lower():
            matches.append(fact)
    matches.sort(key=lambda item: int(item.get("updated_at_ms") or 0), reverse=True)
    if not matches:
        return "I do not have a matching memory yet."
    lines = [
        f"{fact.get('key')}: {fact.get('value')}"
        for fact in matches[:5]
    ]
    return "I remember:\n" + "\n".join(lines)


def list_facts(*, profile_store: ProfileStore | None) -> str:
    if profile_store is None:
        return "I can list memories once profile memory is ready."
    facts = list(_memory_facts(profile_store).values())
    facts.sort(key=lambda item: int(item.get("updated_at_ms") or 0), reverse=True)
    if not facts:
        return "I do not have any durable memories yet."
    lines = [
        f"{fact.get('key')}: {fact.get('value')}"
        for fact in facts[:10]
    ]
    return "I remember:\n" + "\n".join(lines)


async def forget_fact(query: str, *, profile_store: ProfileStore | None) -> str:
    if profile_store is None:
        return "I can forget memories once profile memory is ready."
    needle = query.strip().lower()
    if not needle:
        return "Tell me what memory to forget."
    facts = _memory_facts(profile_store)
    if not facts:
        return "I do not have any memories to forget yet."

    normalized = _normalize_key(query)
    keys_to_delete: list[str] = []
    if normalized in facts:
        keys_to_delete = [normalized]
    else:
        for key, fact in facts.items():
            value = str(fact.get("value") or "")
            if needle in f"{key} {value}".lower():
                keys_to_delete.append(key)
    if not keys_to_delete:
        return "I do not have a matching memory to forget."

    removed = []
    for key in keys_to_delete:
        fact = facts.pop(key, None)
        if fact is not None:
            removed.append(f"{fact.get('key', key)}: {fact.get('value', '')}")
    profile_store.set("memories.facts", facts)
    await profile_store.flush()
    return "Forgot:\n" + "\n".join(removed)


async def search_chat_memory(
    query: str,
    *,
    chat_memory: ChatMemory | None,
    conversation_id: str = "default",
) -> str:
    if chat_memory is None:
        return "I can search chat history once chat memory is ready."
    needle = query.strip()
    if not needle:
        return "Tell me what to search for."
    matches = await chat_memory.search(conversation_id, query=needle, limit=5)
    if not matches:
        return "I did not find that in this chat."
    lines = [_format_chat_match(match) for match in matches]
    return "Earlier in this chat:\n" + "\n".join(lines)


def _fact_key_value(fact: str) -> tuple[str, str]:
    text = _clean_text(fact)
    if not text:
        return "", ""
    for pattern in (
        re.compile(r"^(?:my|the)\s+(?P<key>[\w\s-]{2,48}?)\s+(?:is|=)\s+(?P<value>.+)$", re.I),
        re.compile(r"^我的(?P<key>[^是]{1,24})是(?P<value>.+)$", re.I),
    ):
        match = pattern.match(text)
        if match:
            key = _normalize_key(match.group("key"))
            value = _clean_text(match.group("value"))
            return key, value
    return _normalize_key(text)[:64], text


def _memory_facts(profile_store: ProfileStore) -> dict[str, dict[str, Any]]:
    raw = profile_store.get("memories.facts", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(value, dict)
    }


def _normalize_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return key or "memory"


def _clean_text(value: str) -> str:
    return value.strip().strip("\"'“”‘’。.!?？").strip()


def _format_chat_match(message: Any) -> str:
    role = str(getattr(message, "role", "") or "message")
    content = str(getattr(message, "content", "") or "").strip()
    content = " ".join(content.split())
    if len(content) > 160:
        content = content[:157].rstrip() + "..."
    return f"- {role}: {content}"


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


__all__ = [
    "MemoryRequest",
    "memory_control_composer",
    "memory_control_streaming_composer",
    "parse_memory_request",
    "forget_fact",
    "list_facts",
    "recall_fact",
    "remember_fact",
    "search_chat_memory",
]
