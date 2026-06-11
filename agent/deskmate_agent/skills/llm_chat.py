"""LLM-backed chat skill (V10 Phase 12-ii + L3-B1 streaming).

A :data:`dispatcher.ReplyComposer` (or
:data:`dispatcher.StreamingReplyComposer`) that posts to any OpenAI
chat-completions compatible endpoint — OpenAI itself, OpenRouter,
Ollama's ``/v1`` shim, vLLM, etc. Drop-in replacement for the canned
composer at the exact same seam; everything downstream
(dispatcher / intent sink / bridge / Swift UI) stays untouched.

Design notes:

- **In-memory rolling context.** The composer keeps the last
  ``memory_window`` user/assistant turns so multi-turn chat feels
  coherent without needing a persistent conversation store on disk
  (Phase 13 territory).
- **Fail soft.** Any HTTP / decode / network error falls back to the
  optional ``fallback`` composer (typically the canned skill). That
  way a flaky LLM never blanks the pet bubble — the user always sees
  *something*.
- **Rollback on error.** A failed turn retracts the user message it
  tentatively appended so retries don't send phantom context.
- **Pluggable ``httpx.AsyncClient``.** Tests pass a client wired to
  ``httpx.MockTransport``; production uses a freshly constructed
  client with the configured timeout.
- **Streaming (V10 L3-B1).**
  :func:`openai_compat_streaming_composer` opens a
  ``stream=True`` POST and yields tokens as they arrive. The
  dispatcher accumulates them into a single bubble via
  ``UPDATE_PET_BUBBLE`` intents, throttled to ~50 ms windows.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from ..agent_events import AgentEvent, SessionActivityUpdated, SessionCompleted
from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..logging_setup import get_logger
from ..memory import (
    ChatMemory,
    DeskmateTaskRecord,
    DeskmateTaskStep,
    DeskmateTaskStore,
    Message,
    ProfileStore,
    ToolActionLog,
    ToolActionRecord,
    ToolLessonRecord,
    ToolTaskRecord,
    ToolTaskStatus,
    format_tool_action_summary,
    format_tool_lesson,
    format_tool_task_summary,
    sanitize_tool_arguments,
    summarize_tool_action,
)
from ..memory.suggestions import (
    memory_suggestion_composer,
    memory_suggestion_streaming_composer,
)
from ..sessions import SessionPhase
from .canned_chat import canned_reply_composer
from .computer_control import (
    PendingComputerActionStore,
    computer_control_composer,
    computer_control_streaming_composer,
)
from .memory_control import (
    memory_control_composer,
    memory_control_streaming_composer,
)
from .registry import SkillMode, SkillRegistry
from .reminder_control import (
    reminder_control_composer,
    reminder_control_streaming_composer,
)
from .task_control import (
    task_control_composer,
    task_control_streaming_composer,
)
from .tool_calls import DESKMATE_TOOLS, DeskmateToolExecutor

_LOGGER = get_logger(__name__)
ToolEventSink = Callable[[AgentEvent], Awaitable[None] | None]

_DEFAULT_SYSTEM_PROMPT = (
    "You are Deskmate, a tiny, warm, and witty macOS desktop pet. "
    "Reply in ≤ 60 words. Prefer plain conversational text. "
    "No markdown, no code fences, no lists."
)


def openai_compat_composer(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    timeout_s: float = 15.0,
    memory_window: int = 6,
    max_tokens: int = 200,
    client: httpx.AsyncClient | None = None,
    fallback: ReplyComposer | None = None,
    skill_registry: SkillRegistry | None = None,
    skill_mode: SkillMode = "reactive",
    first_token_observer: Callable[[float], None] | None = None,
    chat_memory: ChatMemory | None = None,
    profile_store: ProfileStore | None = None,
    task_store: DeskmateTaskStore | None = None,
    conversation_id: str = "default",
    tool_executor: DeskmateToolExecutor | None = None,
    tool_event_sink: ToolEventSink | None = None,
    tool_action_log: ToolActionLog | None = None,
    tool_timeout_s: float | None = 8.0,
    tool_round_limit: int = 3,
) -> ReplyComposer:
    """Return a :data:`ReplyComposer` talking to an OpenAI-compat API.

    Phase 10 · ``skill_registry``: when provided, the composer
    consults the registry on every turn, loads the matched skills'
    bodies (cached after first load), and appends each
    ``system_prompt`` fragment as an extra ``system`` message
    *after* the always-on base prompt. Zero matches → exactly the
    old single-system-prompt shape, so this stays backwards
    compatible for callers that don't pass a registry.

    V10 L2-#8A · ``skill_mode``: pinning ``"proactive"`` here narrows
    the registry's view to skills that have explicitly opted into the
    proactive contract via ``proactive_safe=True``. Default is
    ``"reactive"`` which preserves the historical full-catalog
    behaviour for the user-driven chat path.
    """
    effective_client = client or httpx.AsyncClient(timeout=timeout_s)
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    history: list[Message] = []

    def _trim() -> None:
        if len(history) > memory_window:
            del history[: len(history) - memory_window]

    async def _history_with_user(user_msg: Message) -> list[Message]:
        if chat_memory is not None:
            persisted = await chat_memory.recent(
                conversation_id,
                limit=memory_window,
            )
            return [*persisted, user_msg]
        history.append(user_msg)
        _trim()
        return list(history)

    async def _persist(messages_to_store: list[Message]) -> None:
        if chat_memory is not None:
            await chat_memory.append_many(conversation_id, messages_to_store)
            return
        history.extend(messages_to_store[1:])
        _trim()

    async def _resolve_skill_injections(text: str) -> list[dict[str, str]]:
        """Return extra ``system`` messages from matched skill bodies.

        Failures to load any one body never block the turn — they
        just drop that skill's injection. This keeps the composer
        fail-soft for a broken third-party skill pack.
        """
        if skill_registry is None:
            return []
        matches = skill_registry.select(text, mode=skill_mode)
        if not matches:
            return []
        bodies = await asyncio.gather(
            *(skill_registry.load_body(meta.id) for meta in matches)
        )
        injections: list[dict[str, str]] = []
        for body in bodies:
            if body is None or not body.system_prompt:
                continue
            injections.append(
                {"role": "system", "content": body.system_prompt}
            )
        return injections

    async def compose(text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None

        user_msg = Message(role="user", content=stripped)
        turn_history = await _history_with_user(user_msg)

        skill_injections = await _resolve_skill_injections(stripped)
        memory_injections = _profile_memory_injections(profile_store)
        chat_summary_injections = await _chat_summary_injections(
            chat_memory,
            conversation_id=conversation_id,
            covered_message_count=max(0, len(turn_history) - 1),
        )
        tool_action_injections = await _tool_action_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        tool_task_injections = await _tool_task_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        tool_lesson_injections = await _tool_lesson_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        task_focus_injections = await _task_focus_injections(
            task_store,
            conversation_id=conversation_id,
        )
        task_resume_injections = await _task_resume_context_injections(
            task_store=task_store,
            tool_action_log=tool_action_log,
            tool_executor=tool_executor,
            text=stripped,
            conversation_id=conversation_id,
        )
        task_injections = await _task_injections(
            task_store,
            conversation_id=conversation_id,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *memory_injections,
            *chat_summary_injections,
            *task_focus_injections,
            *task_resume_injections,
            *task_injections,
            *tool_task_injections,
            *tool_lesson_injections,
            *tool_action_injections,
            *skill_injections,
            *(_message_to_wire(msg) for msg in turn_history),
        ]

        try:
            started = time.perf_counter()
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            }
            if tool_executor is not None:
                payload["tools"] = DESKMATE_TOOLS
                payload["tool_choice"] = "auto"
            resp = await effective_client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            assistant_wire = data["choices"][0]["message"]
            elapsed_s = time.perf_counter() - started
            _LOGGER.info(
                "llm_composer.first_token",
                model=model,
                seconds=elapsed_s,
            )
            if first_token_observer is not None:
                try:
                    first_token_observer(elapsed_s)
                except Exception as exc:  # noqa: BLE001 — metrics must not break chat
                    _LOGGER.warning(
                        "llm_composer.first_token_observer_error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )

            tool_calls = assistant_wire.get("tool_calls")
            if tool_executor is not None and _has_tool_calls(tool_calls):
                reply, tool_transcript = await _complete_tool_call_loop(
                    assistant_wire,
                    client=effective_client,
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    memory_injections=memory_injections,
                    chat_summary_injections=chat_summary_injections,
                    task_focus_injections=task_focus_injections,
                    task_resume_injections=task_resume_injections,
                    task_injections=task_injections,
                    tool_task_injections=tool_task_injections,
                    tool_lesson_injections=tool_lesson_injections,
                    tool_action_injections=tool_action_injections,
                    skill_injections=skill_injections,
                    turn_history=turn_history,
                    user_msg=user_msg,
                    tool_executor=tool_executor,
                    tool_event_sink=tool_event_sink,
                    tool_action_log=tool_action_log,
                    conversation_id=conversation_id,
                    timeout_s=tool_timeout_s,
                    round_limit=tool_round_limit,
                )
                if reply:
                    await _persist(
                        [
                            user_msg,
                            *tool_transcript,
                            Message(role="assistant", content=reply),
                        ]
                    )
                return reply

            content = assistant_wire.get("content")
        except Exception as exc:  # noqa: BLE001 — fail-soft is the point
            _LOGGER.warning(
                "llm_composer.error",
                model=model,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Retract the failed user turn so a subsequent call isn't
            # anchored to context the LLM never saw.
            if chat_memory is None and history and history[-1] is user_msg:
                history.pop()
            if fallback is not None:
                return await fallback(text)
            return None

        reply = (content or "").strip() or None
        if reply:
            await _persist([user_msg, Message(role="assistant", content=reply)])
        return reply

    return compose


def openai_compat_streaming_composer(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    timeout_s: float = 60.0,
    first_token_timeout_s: float | None = 8.0,
    memory_window: int = 6,
    max_tokens: int = 200,
    client: httpx.AsyncClient | None = None,
    fallback: ReplyComposer | None = None,
    skill_registry: SkillRegistry | None = None,
    skill_mode: SkillMode = "reactive",
    first_token_observer: Callable[[float], None] | None = None,
    chat_memory: ChatMemory | None = None,
    profile_store: ProfileStore | None = None,
    task_store: DeskmateTaskStore | None = None,
    conversation_id: str = "default",
    tool_executor: DeskmateToolExecutor | None = None,
    tool_event_sink: ToolEventSink | None = None,
    tool_action_log: ToolActionLog | None = None,
    tool_timeout_s: float | None = 8.0,
    tool_round_limit: int = 3,
) -> StreamingReplyComposer:
    """Return a :data:`StreamingReplyComposer` for V10 L3-B1.

    Mirrors :func:`openai_compat_composer` (system prompt, skill
    injection, rolling history, fail-soft fallback) but uses
    ``stream=True`` and yields content tokens as they arrive.

    The first token's wall-clock arrival is observed via
    ``first_token_observer`` so the §3.1 row 7 ``llm_first_token_s``
    budget can be filled honestly. The previous non-streaming
    observer measured the whole response — keep it for backwards
    compat, but treat the streaming number as the canonical one.

    V10 L3-B5 · ``first_token_timeout_s`` (default ``8s``) is the
    upper bound on time-to-first-token. When set, a model that
    refuses to start streaming within the budget falls through to
    the configured fallback so the user never stares at the ``"…"``
    placeholder forever. Set to ``None`` to disable the deadline
    (the global ``timeout_s`` still bounds the underlying request).

    Failure modes:

    - Stream open fails (DNS, TLS, 401, 5xx, network blip mid-call):
      log + retract the user turn + delegate to ``fallback`` (if
      configured) and yield its result as a single chunk so the
      dispatcher's streaming chain still sees something to render.
    - Empty stream: yield nothing; the dispatcher leaves the
      placeholder in place per its existing contract.
    - First-token deadline exceeded: same as stream open failure.
    """
    effective_client = client or httpx.AsyncClient(timeout=timeout_s)
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    history: list[Message] = []

    def _trim() -> None:
        if len(history) > memory_window:
            del history[: len(history) - memory_window]

    async def _history_with_user(user_msg: Message) -> list[Message]:
        if chat_memory is not None:
            persisted = await chat_memory.recent(
                conversation_id,
                limit=memory_window,
            )
            return [*persisted, user_msg]
        history.append(user_msg)
        _trim()
        return list(history)

    async def _persist(messages_to_store: list[Message]) -> None:
        if chat_memory is not None:
            await chat_memory.append_many(conversation_id, messages_to_store)
            return
        history.extend(messages_to_store[1:])
        _trim()

    async def _resolve_skill_injections(text: str) -> list[dict[str, str]]:
        if skill_registry is None:
            return []
        matches = skill_registry.select(text, mode=skill_mode)
        if not matches:
            return []
        bodies = await asyncio.gather(
            *(skill_registry.load_body(meta.id) for meta in matches)
        )
        out: list[dict[str, str]] = []
        for body in bodies:
            if body is None or not body.system_prompt:
                continue
            out.append({"role": "system", "content": body.system_prompt})
        return out

    async def compose(text: str) -> AsyncIterator[str]:
        stripped = text.strip()
        if not stripped:
            return

        user_msg = Message(role="user", content=stripped)
        turn_history = await _history_with_user(user_msg)

        skill_injections = await _resolve_skill_injections(stripped)
        memory_injections = _profile_memory_injections(profile_store)
        chat_summary_injections = await _chat_summary_injections(
            chat_memory,
            conversation_id=conversation_id,
            covered_message_count=max(0, len(turn_history) - 1),
        )
        tool_action_injections = await _tool_action_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        tool_task_injections = await _tool_task_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        tool_lesson_injections = await _tool_lesson_injections(
            tool_action_log,
            conversation_id=conversation_id,
        )
        task_focus_injections = await _task_focus_injections(
            task_store,
            conversation_id=conversation_id,
        )
        task_resume_injections = await _task_resume_context_injections(
            task_store=task_store,
            tool_action_log=tool_action_log,
            tool_executor=tool_executor,
            text=stripped,
            conversation_id=conversation_id,
        )
        task_injections = await _task_injections(
            task_store,
            conversation_id=conversation_id,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            *memory_injections,
            *chat_summary_injections,
            *task_focus_injections,
            *task_resume_injections,
            *task_injections,
            *tool_task_injections,
            *tool_lesson_injections,
            *tool_action_injections,
            *skill_injections,
            *(_message_to_wire(msg) for msg in turn_history),
        ]

        accumulated_parts: list[str] = []
        streamed_tool_calls: dict[int, dict[str, Any]] = {}
        first_token_logged = False
        started = time.perf_counter()
        try:
            request_payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "stream": True,
            }
            if tool_executor is not None:
                request_payload["tools"] = DESKMATE_TOOLS
                request_payload["tool_choice"] = "auto"
            async with effective_client.stream(
                "POST",
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                },
                json=request_payload,
            ) as resp:
                resp.raise_for_status()
                line_iter = resp.aiter_lines().__aiter__()
                while True:
                    try:
                        if not first_token_logged and first_token_timeout_s:
                            line = await asyncio.wait_for(
                                line_iter.__anext__(),
                                timeout=first_token_timeout_s,
                            )
                        else:
                            line = await line_iter.__anext__()
                    except StopAsyncIteration:
                        break
                    if not line:
                        continue
                    # OpenAI-compatible streaming uses SSE-style
                    # lines: ``data: {...}`` with a terminating
                    # ``data: [DONE]``. Some Ollama-style proxies
                    # also send raw JSON one per line — accept both.
                    payload_str = line.removeprefix("data:").lstrip()
                    if not payload_str:
                        continue
                    if payload_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload_str)
                    except json.JSONDecodeError:
                        # Heartbeat / comment lines — ignore.
                        continue
                    _accumulate_stream_tool_calls(chunk, streamed_tool_calls)
                    delta = _extract_delta_content(chunk)
                    if not delta:
                        continue
                    if not first_token_logged:
                        elapsed_s = time.perf_counter() - started
                        _LOGGER.info(
                            "llm_composer.first_token",
                            model=model,
                            seconds=elapsed_s,
                            streaming=True,
                        )
                        if first_token_observer is not None:
                            try:
                                first_token_observer(elapsed_s)
                            except Exception as exc:  # noqa: BLE001
                                _LOGGER.warning(
                                    "llm_composer.first_token_observer_error",
                                    error=str(exc),
                                    error_type=type(exc).__name__,
                                )
                        first_token_logged = True
                    accumulated_parts.append(delta)
                    yield delta
        except Exception as exc:  # noqa: BLE001 — fail-soft is the point
            _LOGGER.warning(
                "llm_composer.stream_error",
                model=model,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Retract the failed user turn so a subsequent call isn't
            # anchored to context the LLM never saw.
            if chat_memory is None and history and history[-1] is user_msg:
                history.pop()
            if fallback is not None and not accumulated_parts:
                # Only fall back when the stream produced no tokens —
                # a partial reply already on screen is better than
                # silently re-rendering a canned echo over it.
                fallback_text = await fallback(text)
                if fallback_text:
                    yield fallback_text
            return

        full_reply = "".join(accumulated_parts).strip()
        if full_reply:
            await _persist([user_msg, Message(role="assistant", content=full_reply)])
            return

        tool_calls = _stream_tool_calls_to_wire(streamed_tool_calls)
        if tool_executor is not None and tool_calls:
            try:
                started = time.perf_counter()
                reply, tool_transcript = await _complete_tool_call_loop(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    },
                    client=effective_client,
                    endpoint=endpoint,
                    api_key=api_key,
                    model=model,
                    max_tokens=max_tokens,
                    system_prompt=system_prompt,
                    memory_injections=memory_injections,
                    chat_summary_injections=chat_summary_injections,
                    task_focus_injections=task_focus_injections,
                    task_resume_injections=task_resume_injections,
                    task_injections=task_injections,
                    tool_task_injections=tool_task_injections,
                    tool_lesson_injections=tool_lesson_injections,
                    tool_action_injections=tool_action_injections,
                    skill_injections=skill_injections,
                    turn_history=turn_history,
                    user_msg=user_msg,
                    tool_executor=tool_executor,
                    tool_event_sink=tool_event_sink,
                    tool_action_log=tool_action_log,
                    conversation_id=conversation_id,
                    timeout_s=tool_timeout_s,
                    round_limit=tool_round_limit,
                )
                if reply:
                    if not first_token_logged:
                        elapsed_s = time.perf_counter() - started
                        _LOGGER.info(
                            "llm_composer.first_token",
                            model=model,
                            seconds=elapsed_s,
                            streaming=True,
                            tool_followup=True,
                        )
                        if first_token_observer is not None:
                            try:
                                first_token_observer(elapsed_s)
                            except Exception as exc:  # noqa: BLE001
                                _LOGGER.warning(
                                    "llm_composer.first_token_observer_error",
                                    error=str(exc),
                                    error_type=type(exc).__name__,
                                )
                    await _persist(
                        [
                            user_msg,
                            *tool_transcript,
                            Message(role="assistant", content=reply),
                        ]
                    )
                    yield reply
            except Exception as exc:  # noqa: BLE001 — fail-soft is the point
                _LOGGER.warning(
                    "llm_composer.tool_followup_error",
                    model=model,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                if chat_memory is None and history and history[-1] is user_msg:
                    history.pop()
                if fallback is not None:
                    fallback_text = await fallback(text)
                    if fallback_text:
                        yield fallback_text

    return compose


def _message_to_wire(message: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": message.role}
    if message.content is not None:
        out["content"] = message.content
    if message.tool_calls is not None:
        out["tool_calls"] = message.tool_calls
    if message.tool_call_id is not None:
        out["tool_call_id"] = message.tool_call_id
    return out


def _profile_memory_injections(
    profile_store: ProfileStore | None,
    *,
    limit: int = 8,
    max_chars: int = 1_200,
) -> list[dict[str, str]]:
    if profile_store is None:
        return []
    raw = profile_store.get("memories.facts", {})
    if not isinstance(raw, dict):
        return []
    facts: list[dict[str, Any]] = [
        dict(value)
        for value in raw.values()
        if isinstance(value, dict)
        and str(value.get("key") or "").strip()
        and str(value.get("value") or "").strip()
    ]
    if not facts:
        return []
    facts.sort(key=lambda item: int(item.get("updated_at_ms") or 0), reverse=True)
    lines: list[str] = []
    total = len("Known durable memories:")
    for fact in facts[:limit]:
        key = str(fact.get("key") or "").strip()
        value = str(fact.get("value") or "").strip()
        line = f"- {key}: {value}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return []
    return [
        {
            "role": "system",
            "content": "Known durable memories:\n" + "\n".join(lines),
        }
    ]


async def _chat_summary_injections(
    chat_memory: ChatMemory | None,
    *,
    conversation_id: str = "default",
    covered_message_count: int = 0,
    max_chars: int = 1_200,
) -> list[dict[str, str]]:
    if chat_memory is None:
        return []
    try:
        summary = await chat_memory.get_summary(conversation_id)
    except Exception as exc:  # noqa: BLE001 — summary recall must not break chat
        _LOGGER.warning(
            "llm_composer.chat_summary_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if summary is None:
        return []
    if summary.message_count <= covered_message_count:
        return []
    text = summary.summary.strip()
    if not text:
        return []
    older_line_count = max(1, summary.message_count - covered_message_count)
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > older_line_count:
        text = "\n".join(lines[:older_line_count])
    if len(text) > max_chars:
        text = text[-max_chars:].lstrip()
    return [
        {
            "role": "system",
            "content": "Persistent conversation summary:\n" + text,
        }
    ]


async def _tool_action_injections(
    tool_action_log: ToolActionLog | None,
    *,
    conversation_id: str = "default",
    limit: int = 5,
    max_chars: int = 1_000,
) -> list[dict[str, str]]:
    if tool_action_log is None:
        return []
    try:
        records = await tool_action_log.recent(conversation_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 — action recall must not break chat
        _LOGGER.warning(
            "llm_composer.tool_action_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not records:
        return []

    lines: list[str] = []
    total = len("Recent Deskmate tool actions:")
    for record in records[-limit:]:
        line = _format_tool_action_memory(record)
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return []
    return [
        {
            "role": "system",
            "content": "Recent Deskmate tool actions:\n" + "\n".join(lines),
        }
    ]


async def _tool_task_injections(
    tool_action_log: ToolActionLog | None,
    *,
    conversation_id: str = "default",
    limit: int = 3,
    max_chars: int = 800,
) -> list[dict[str, str]]:
    if tool_action_log is None:
        return []
    try:
        tasks = await tool_action_log.recent_tasks(conversation_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 — task recall must not break chat
        _LOGGER.warning(
            "llm_composer.tool_task_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not tasks:
        return []
    lines: list[str] = []
    total = len("Recent Deskmate tool tasks:")
    for task in tasks[-limit:]:
        line = f"- {format_tool_task_summary(task)}"
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return []
    return [
        {
            "role": "system",
            "content": "Recent Deskmate tool tasks:\n" + "\n".join(lines),
        }
    ]


async def _tool_lesson_injections(
    tool_action_log: ToolActionLog | None,
    *,
    conversation_id: str = "default",
    limit: int = 3,
    max_chars: int = 800,
) -> list[dict[str, str]]:
    if tool_action_log is None:
        return []
    try:
        lessons = await tool_action_log.recent_lessons(conversation_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 — lesson recall must not break chat
        _LOGGER.warning(
            "llm_composer.tool_lesson_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not lessons:
        return []
    lines: list[str] = []
    total = len("Durable Deskmate tool lessons:")
    for lesson in lessons[-limit:]:
        line = _format_tool_lesson_memory(lesson)
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return []
    return [
        {
            "role": "system",
            "content": "Durable Deskmate tool lessons:\n" + "\n".join(lines),
        }
    ]


async def _task_focus_injections(
    task_store: DeskmateTaskStore | None,
    *,
    conversation_id: str = "default",
    max_chars: int = 700,
) -> list[dict[str, str]]:
    if task_store is None:
        return []
    try:
        tasks = await task_store.list(
            conversation_id,
            status="active",
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — task focus recall must not break chat
        _LOGGER.warning(
            "llm_composer.task_focus_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not tasks:
        return []

    task = _select_task_focus(tasks)
    steps = await _task_steps_for_injection(
        task_store,
        task,
        conversation_id=conversation_id,
        limit=8,
    )
    content = _format_task_focus_memory(task, steps=steps)
    if not content:
        return []
    if len(content) > max_chars:
        content = content[: max(0, max_chars - 3)].rstrip() + "..."
    return [
        {
            "role": "system",
            "content": "Current Deskmate task focus:\n" + content,
        }
    ]


async def _task_resume_context_injections(
    *,
    task_store: DeskmateTaskStore | None,
    tool_action_log: ToolActionLog | None,
    tool_executor: DeskmateToolExecutor | None,
    text: str,
    conversation_id: str = "default",
    max_chars: int = 1_800,
) -> list[dict[str, str]]:
    if not _has_task_resume_intent(text):
        return []
    if task_store is None or tool_action_log is None:
        return []

    try:
        tasks = await task_store.list(
            conversation_id,
            status="active",
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 — resume context must not break chat
        _LOGGER.warning(
            "llm_composer.task_resume_lookup_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not tasks:
        return []

    task = _select_task_focus(tasks)
    executor = (
        tool_executor
        if tool_executor is not None
        and tool_executor.task_store is not None
        and tool_executor.tool_action_log is not None
        else DeskmateToolExecutor(
            task_store=task_store,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
        )
    )
    try:
        context = await executor.execute(
            "deskmate_task_context",
            {
                "task_id": task.task_id,
                "limit": 5,
            },
        )
    except Exception as exc:  # noqa: BLE001 — read-only context is best effort
        _LOGGER.warning(
            "llm_composer.task_resume_context_error",
            conversation_id=conversation_id,
            task_id=task.task_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    context = context.strip()
    if not context or context.startswith("Tool error:") or context == "No matching task.":
        return []
    if len(context) > max_chars:
        context = context[: max(0, max_chars - 3)].rstrip() + "..."
    return [
        {
            "role": "system",
            "content": "Resume context for current Deskmate task:\n" + context,
        }
    ]


async def _task_injections(
    task_store: DeskmateTaskStore | None,
    *,
    conversation_id: str = "default",
    limit: int = 5,
    max_chars: int = 1_000,
) -> list[dict[str, str]]:
    if task_store is None:
        return []
    try:
        tasks = await task_store.list(
            conversation_id,
            status="active",
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001 — task recall must not break chat
        _LOGGER.warning(
            "llm_composer.task_injection_error",
            conversation_id=conversation_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    if not tasks:
        return []
    lines: list[str] = []
    total = len("Active Deskmate tasks:")
    for task in tasks[:limit]:
        steps = await _task_steps_for_injection(
            task_store,
            task,
            conversation_id=conversation_id,
        )
        line = _format_task_memory(task, steps=steps)
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return []
    return [
        {
            "role": "system",
            "content": "Active Deskmate tasks:\n" + "\n".join(lines),
        }
    ]


def _select_task_focus(tasks: list[DeskmateTaskRecord]) -> DeskmateTaskRecord:
    for task in tasks:
        if task.status == "in_progress":
            return task
    return tasks[0]


def _has_task_resume_intent(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    if normalized in {
        "continue",
        "resume",
        "carry on",
        "keep going",
        "继续",
        "继续吧",
        "接着",
        "接着做",
    }:
        return True
    if any(
        phrase in normalized
        for phrase in (
            "继续任务",
            "继续当前任务",
            "继续刚才",
            "接着刚才",
            "接着任务",
            "恢复任务",
            "恢复当前任务",
            "当前任务",
            "刚才的任务",
        )
    ):
        return True
    has_resume_verb = any(
        phrase in normalized
        for phrase in (
            "continue",
            "resume",
            "pick up",
            "carry on",
            "keep working",
        )
    )
    has_task_reference = any(
        phrase in normalized
        for phrase in (
            "task",
            "todo",
            "current work",
            "that work",
            "this work",
            "where we left off",
        )
    )
    return has_resume_verb and has_task_reference


def _format_task_focus_memory(
    record: DeskmateTaskRecord,
    *,
    steps: list[DeskmateTaskStep] | None = None,
) -> str:
    lines = [
        f"- task={record.task_id}; status={record.status}; title={record.title}"
    ]
    if record.notes:
        lines.append(f"- notes={_compact_inline(record.notes, limit=160)}")
    step_list = steps or []
    current = _current_task_step(step_list)
    next_step = _next_task_step(step_list, after=current)
    completed_count = sum(1 for step in step_list if step.status == "completed")
    if current is not None:
        lines.append(
            "- current_step="
            f"{current.position}/{max(len(step_list), current.position)} "
            f"[{current.status}] {_task_step_text(current)}"
        )
    elif next_step is not None:
        lines.append(
            "- current_step="
            f"{next_step.position}/{max(len(step_list), next_step.position)} "
            f"[pending] {_task_step_text(next_step)}"
        )
    if next_step is not None and next_step is not current:
        lines.append(
            "- next_step="
            f"{next_step.position}/{max(len(step_list), next_step.position)} "
            f"{_task_step_text(next_step)}"
        )
    if step_list:
        lines.append(f"- progress={completed_count}/{len(step_list)} steps completed")
    return "\n".join(lines)


def _current_task_step(steps: list[DeskmateTaskStep]) -> DeskmateTaskStep | None:
    for step in steps:
        if step.status == "in_progress":
            return step
    for step in steps:
        if step.status == "pending":
            return step
    return steps[-1] if steps else None


def _next_task_step(
    steps: list[DeskmateTaskStep],
    *,
    after: DeskmateTaskStep | None,
) -> DeskmateTaskStep | None:
    for step in steps:
        if step.status == "pending" and (
            after is None or step.position >= after.position
        ):
            return step
    return None


def _task_step_text(step: DeskmateTaskStep) -> str:
    text = step.active_form if step.status == "in_progress" and step.active_form else step.content
    return _compact_inline(text, limit=140)


def _format_tool_action_memory(record: ToolActionRecord) -> str:
    return f"- {format_tool_action_summary(record)}"


def _format_tool_lesson_memory(record: ToolLessonRecord) -> str:
    return f"- {format_tool_lesson(record)}"


async def _task_steps_for_injection(
    task_store: DeskmateTaskStore,
    task: DeskmateTaskRecord,
    *,
    conversation_id: str,
    limit: int = 4,
) -> list[DeskmateTaskStep]:
    try:
        steps = await task_store.list_steps(
            task.task_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:  # noqa: BLE001 — task recall must not break chat
        _LOGGER.warning(
            "llm_composer.task_step_injection_error",
            conversation_id=conversation_id,
            task_id=task.task_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []
    return steps[: max(0, limit)]


def _format_task_memory(
    record: DeskmateTaskRecord,
    *,
    steps: list[DeskmateTaskStep] | None = None,
) -> str:
    notes = f"; notes={record.notes}" if record.notes else ""
    step_text = _format_task_steps_memory(steps or [])
    step_suffix = f"; steps={step_text}" if step_text else ""
    return f"- {record.task_id}; status={record.status}; title={record.title}{notes}{step_suffix}"


def _format_task_steps_memory(steps: list[DeskmateTaskStep]) -> str:
    if not steps:
        return ""
    parts: list[str] = []
    for step in steps:
        text = step.active_form if step.status == "in_progress" and step.active_form else step.content
        text = " ".join(text.split())
        if not text:
            continue
        parts.append(f"{step.status}: {_compact_inline(text, limit=72)}")
    return " | ".join(parts)


def _compact_inline(value: str, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


@dataclass
class _ToolTaskAccumulator:
    task_id: str
    conversation_id: str
    user_text: str
    started_at_ms: int
    action_count: int = 0
    failed_count: int = 0
    duplicate_count: int = 0
    last_summary: str = ""

    def note(self, record: ToolActionRecord) -> None:
        self.action_count += 1
        if record.status == "failed":
            self.failed_count += 1
        if record.status == "duplicate":
            self.duplicate_count += 1
        self.last_summary = format_tool_action_summary(record)

    def status(self) -> ToolTaskStatus:
        return "failed" if self.failed_count else "completed"

    def summary(self) -> str:
        if self.last_summary:
            return self.last_summary
        return f"User requested: {self.user_text}"

    def record(
        self,
        *,
        status: ToolTaskStatus,
        ts_ms: int | None = None,
    ) -> ToolTaskRecord:
        now = ts_ms if ts_ms is not None else int(time.time() * 1000)
        return ToolTaskRecord(
            task_id=self.task_id,
            conversation_id=self.conversation_id,
            user_text=self.user_text,
            status=status,
            summary=self.summary(),
            action_count=self.action_count,
            failed_count=self.failed_count,
            duplicate_count=self.duplicate_count,
            started_at_ms=self.started_at_ms,
            updated_at_ms=now,
            completed_at_ms=now if status in {"completed", "failed"} else None,
        )


def _has_tool_calls(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


async def _complete_tool_call_loop(
    assistant_wire: dict[str, Any],
    *,
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    model: str,
    max_tokens: int,
    system_prompt: str,
    memory_injections: list[dict[str, str]],
    chat_summary_injections: list[dict[str, str]],
    task_focus_injections: list[dict[str, str]],
    task_resume_injections: list[dict[str, str]],
    task_injections: list[dict[str, str]],
    tool_task_injections: list[dict[str, str]],
    tool_lesson_injections: list[dict[str, str]],
    tool_action_injections: list[dict[str, str]],
    skill_injections: list[dict[str, str]],
    turn_history: list[Message],
    user_msg: Message,
    tool_executor: DeskmateToolExecutor,
    tool_event_sink: ToolEventSink | None,
    tool_action_log: ToolActionLog | None,
    conversation_id: str,
    timeout_s: float | None,
    round_limit: int,
) -> tuple[str | None, list[Message]]:
    rounds_remaining = max(1, min(round_limit, 5))
    transcript: list[Message] = []
    current_wire = assistant_wire
    seen_results: dict[str, str] = {}
    task = _ToolTaskAccumulator(
        task_id=_tool_task_id(conversation_id),
        conversation_id=conversation_id,
        user_text=user_msg.content or "",
        started_at_ms=int(time.time() * 1000),
    )
    await _persist_tool_task(tool_action_log, task, status="running")

    for round_index in range(rounds_remaining):
        tool_calls = current_wire.get("tool_calls")
        if not _has_tool_calls(tool_calls):
            content = current_wire.get("content")
            return (content or "").strip() or None, transcript

        assistant_msg = Message(
            role="assistant",
            content=current_wire.get("content"),
            tool_calls=tool_calls,
        )
        tool_messages = await _execute_tool_calls(
            tool_calls,
            tool_executor=tool_executor,
            tool_event_sink=tool_event_sink,
            tool_action_log=tool_action_log,
            tool_task=task,
            conversation_id=conversation_id,
            timeout_s=timeout_s,
            seen_results=seen_results,
            user_text=user_msg.content or "",
        )
        transcript.extend([assistant_msg, *tool_messages])

        followup_messages = [
            {"role": "system", "content": system_prompt},
            *memory_injections,
            *chat_summary_injections,
            *task_focus_injections,
            *task_resume_injections,
            *task_injections,
            *tool_task_injections,
            *tool_lesson_injections,
            *tool_action_injections,
            *skill_injections,
            *(_message_to_wire(msg) for msg in turn_history[:-1]),
            _message_to_wire(user_msg),
            *(_message_to_wire(msg) for msg in transcript),
        ]
        payload: dict[str, Any] = {
            "model": model,
            "messages": followup_messages,
            "max_tokens": max_tokens,
        }
        if round_index < rounds_remaining - 1:
            payload["tools"] = DESKMATE_TOOLS
            payload["tool_choice"] = "auto"
        else:
            followup_messages.insert(
                1,
                {
                    "role": "system",
                    "content": (
                        "Tool-call round limit reached. Summarize the tool "
                        "results for the user without calling another tool."
                    ),
                },
            )

        resp = await client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        current_wire = data["choices"][0]["message"]

    content = current_wire.get("content")
    return (content or "").strip() or None, transcript


async def _execute_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    tool_executor: DeskmateToolExecutor,
    tool_event_sink: ToolEventSink | None = None,
    tool_action_log: ToolActionLog | None = None,
    tool_task: _ToolTaskAccumulator | None = None,
    conversation_id: str = "default",
    timeout_s: float | None = 8.0,
    seen_results: dict[str, str] | None = None,
    user_text: str = "",
) -> list[Message]:
    out: list[Message] = []
    seen = seen_results if seen_results is not None else {}
    for idx, call in enumerate(tool_calls):
        call_id = str(call.get("id") or f"tool-call-{idx}")
        function = call.get("function")
        if not isinstance(function, dict):
            out.append(
                Message(
                    role="tool",
                    tool_call_id=call_id,
                    content="Tool error: missing function payload.",
                )
            )
            continue
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        parsed_arguments = _parse_tool_arguments_for_log(arguments)
        safe_arguments = sanitize_tool_arguments(parsed_arguments)
        signature = _tool_call_signature(name, arguments)
        if signature in seen:
            result = seen[signature]
            failed = result.startswith("Tool error:")
            now_ms = int(time.time() * 1000)
            summary = summarize_tool_action(
                tool_name=name,
                arguments=safe_arguments,
                result=result,
                status="duplicate",
            )
            record = ToolActionRecord(
                conversation_id=conversation_id,
                tool_call_id=call_id,
                task_id=tool_task.task_id if tool_task is not None else None,
                tool_name=name,
                arguments=safe_arguments,
                result=result,
                status="duplicate",
                started_at_ms=now_ms,
                completed_at_ms=now_ms,
                summary=summary,
            )
            await _append_tool_action(
                tool_action_log,
                record,
            )
            await _update_tool_task(tool_action_log, tool_task, record)
            await _emit_tool_event(
                tool_event_sink,
                session_id=_tool_session_id(conversation_id),
                raw_event="tool.duplicate",
                phase=SessionPhase.FAILED if failed else SessionPhase.COMPLETED,
                summary=f"Skipped duplicate {name or 'tool'}",
                tool_name=name,
                tool_id=call_id,
                tool_result=result,
                failed=failed,
                **_tool_summary_event_fields(record),
                **_tool_task_event_fields(tool_task),
            )
            out.append(Message(role="tool", tool_call_id=call_id, content=result))
            continue
        policy_error = _tool_policy_error(name, arguments, user_text)
        started_record = ToolActionRecord(
            conversation_id=conversation_id,
            tool_call_id=call_id,
            task_id=tool_task.task_id if tool_task is not None else None,
            tool_name=name,
            arguments=safe_arguments,
            result="",
            status="completed",
            started_at_ms=0,
            completed_at_ms=0,
            summary=summarize_tool_action(
                tool_name=name,
                arguments=safe_arguments,
                result="",
                status="completed",
            ),
        )
        await _emit_tool_event(
            tool_event_sink,
            session_id=_tool_session_id(conversation_id),
            raw_event="tool.started",
            phase=SessionPhase.RUNNING_TOOL,
            summary=f"Running {name or 'tool'}",
            tool_name=name,
            tool_id=call_id,
            **_tool_summary_event_fields(started_record, include_summary=False),
            **_tool_task_event_fields(tool_task, status="running"),
        )
        started_at_ms = int(time.time() * 1000)
        if policy_error is not None:
            result = policy_error
        else:
            try:
                execution = tool_executor.execute(name, arguments)
                if timeout_s is not None and timeout_s > 0:
                    result = await asyncio.wait_for(execution, timeout=timeout_s)
                else:
                    result = await execution
            except TimeoutError:
                _LOGGER.warning(
                    "llm_composer.tool_timeout",
                    tool_name=name,
                    tool_call_id=call_id,
                    seconds=timeout_s,
                )
                result = f"Tool error: {name or 'tool'} timed out."
            except Exception as exc:  # noqa: BLE001 — tool failures become model-visible results
                _LOGGER.warning(
                    "llm_composer.tool_error",
                    tool_name=name,
                    tool_call_id=call_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                result = f"Tool error: {exc}"
        seen[signature] = result
        failed = result.startswith("Tool error:")
        completed_at_ms = int(time.time() * 1000)
        status = "failed" if failed else "completed"
        summary = summarize_tool_action(
            tool_name=name,
            arguments=safe_arguments,
            result=result,
            status=status,
        )
        record = ToolActionRecord(
            conversation_id=conversation_id,
            tool_call_id=call_id,
            task_id=tool_task.task_id if tool_task is not None else None,
            tool_name=name,
            arguments=safe_arguments,
            result=result,
            status=status,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            summary=summary,
        )
        await _append_tool_action(
            tool_action_log,
            record,
        )
        await _update_tool_task(tool_action_log, tool_task, record)
        await _emit_tool_event(
            tool_event_sink,
            session_id=_tool_session_id(conversation_id),
            raw_event="tool.failed" if failed else "tool.completed",
            phase=SessionPhase.FAILED if failed else SessionPhase.COMPLETED,
            summary=result,
            tool_name=name,
            tool_id=call_id,
            tool_result=result,
            failed=failed,
            **_tool_summary_event_fields(record),
            **_tool_task_event_fields(tool_task),
        )
        out.append(Message(role="tool", tool_call_id=call_id, content=result))
    return out


async def _append_tool_action(
    tool_action_log: ToolActionLog | None,
    record: ToolActionRecord,
) -> None:
    if tool_action_log is None:
        return
    try:
        await tool_action_log.append(record)
    except Exception as exc:  # noqa: BLE001 — audit logging must not break chat
        _LOGGER.warning(
            "llm_composer.tool_action_log_error",
            tool_name=record.tool_name,
            tool_call_id=record.tool_call_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def _persist_tool_task(
    tool_action_log: ToolActionLog | None,
    task: _ToolTaskAccumulator | None,
    *,
    status: ToolTaskStatus,
) -> None:
    if tool_action_log is None or task is None:
        return
    try:
        await tool_action_log.upsert_task(task.record(status=status))
    except Exception as exc:  # noqa: BLE001 — task audit must not break chat
        _LOGGER.warning(
            "llm_composer.tool_task_log_error",
            task_id=task.task_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def _update_tool_task(
    tool_action_log: ToolActionLog | None,
    task: _ToolTaskAccumulator | None,
    record: ToolActionRecord,
) -> None:
    if task is None:
        return
    task.note(record)
    await _persist_tool_task(tool_action_log, task, status=task.status())


def _parse_tool_arguments_for_log(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return arguments
    return arguments


def _tool_summary_event_fields(
    record: ToolActionRecord,
    *,
    include_summary: bool = True,
) -> dict[str, str]:
    summary = record.summary or {}
    fields = {
        "tool_action": str(summary.get("action") or record.tool_name or ""),
        "tool_target": str(summary.get("target") or ""),
        "tool_outcome": str(summary.get("outcome") or ""),
        "tool_needs_user": "true" if bool(summary.get("needs_user")) else "false",
    }
    if include_summary:
        fields["tool_summary"] = format_tool_action_summary(record)
    return fields


def _tool_task_event_fields(
    task: _ToolTaskAccumulator | None,
    *,
    status: ToolTaskStatus | None = None,
) -> dict[str, str]:
    if task is None:
        return {}
    task_status = status or task.status()
    return {
        "tool_task_id": task.task_id,
        "tool_task_status": task_status,
        "tool_task_summary": task.summary(),
    }


def _tool_policy_error(
    name: str,
    arguments: Any,
    user_text: str,
) -> str | None:
    """Return a model-visible error when a tool call violates local policy."""
    del arguments
    lowered = user_text.lower()
    if name == "deskmate_remember_fact" and not _has_explicit_memory_write_intent(
        lowered
    ):
        return (
            "Tool error: deskmate_remember_fact requires an explicit user request "
            "to remember this. Use deskmate_suggest_memory for ordinary "
            "conversation-derived preferences."
        )
    if name == "deskmate_forget_memory" and not _has_explicit_memory_forget_intent(
        lowered
    ):
        return (
            "Tool error: deskmate_forget_memory requires an explicit user request "
            "to forget or remove a stored memory."
        )
    if name == "deskmate_cancel_reminder" and not _has_explicit_reminder_cancel_intent(
        lowered
    ):
        return (
            "Tool error: deskmate_cancel_reminder requires an explicit user "
            "request to cancel a reminder."
        )
    if name == "deskmate_create_task" and not _has_explicit_task_write_intent(
        lowered
    ):
        return (
            "Tool error: deskmate_create_task requires an explicit user request "
            "to add or track a task. Use deskmate_suggest_task for ordinary "
            "conversation-derived todos."
        )
    if name in {
        "deskmate_update_task",
        "deskmate_update_task_steps",
    } and not _has_explicit_task_update_intent(lowered):
        return (
            f"Tool error: {name} requires an explicit user request "
            "to update, complete, cancel, or revise a tracked task."
        )
    return None


def _has_explicit_memory_write_intent(lowered_user_text: str) -> bool:
    return any(
        marker in lowered_user_text
        for marker in (
            "remember",
            "keep in mind",
            "save this",
            "store this",
            "记住",
            "记一下",
            "帮我记",
            "保存",
        )
    )


def _has_explicit_memory_forget_intent(lowered_user_text: str) -> bool:
    return any(
        marker in lowered_user_text
        for marker in (
            "forget",
            "delete memory",
            "remove memory",
            "clear memory",
            "stop remembering",
            "忘记",
            "删掉记忆",
            "删除记忆",
            "清除记忆",
            "别记",
            "不要记",
        )
    )


def _has_explicit_reminder_cancel_intent(lowered_user_text: str) -> bool:
    return any(
        marker in lowered_user_text
        for marker in (
            "cancel reminder",
            "cancel my reminder",
            "cancel the reminder",
            "delete reminder",
            "remove reminder",
            "clear reminder",
            "cancel timer",
            "delete timer",
            "remove timer",
            "取消提醒",
            "删除提醒",
            "删掉提醒",
            "取消计时器",
            "删除计时器",
        )
    )


def _has_explicit_task_write_intent(lowered_user_text: str) -> bool:
    return any(
        marker in lowered_user_text
        for marker in (
            "add task",
            "add todo",
            "create task",
            "create todo",
            "track this",
            "track task",
            "todo:",
            "put this on my todo",
            "remember to",
            "添加任务",
            "新增任务",
            "加个任务",
            "加到待办",
            "新增待办",
            "记录任务",
        )
    )


def _has_explicit_task_update_intent(lowered_user_text: str) -> bool:
    return any(
        marker in lowered_user_text
        for marker in (
            "complete task",
            "mark task",
            "update task",
            "update steps",
            "update checklist",
            "set task steps",
            "set checklist",
            "cancel task",
            "finish task",
            "done with task",
            "revise task",
            "checklist",
            "todo steps",
            "完成任务",
            "更新任务",
            "更新步骤",
            "更新清单",
            "设置步骤",
            "设置清单",
            "取消任务",
            "标记任务",
            "任务完成",
        )
    )


def _tool_call_signature(name: str, arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = arguments
    else:
        parsed = arguments
    try:
        normalized = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    except TypeError:
        normalized = str(parsed)
    return f"{name}\0{normalized}"


async def _emit_tool_event(
    sink: ToolEventSink | None,
    *,
    session_id: str,
    raw_event: str,
    phase: SessionPhase,
    summary: str,
    tool_name: str,
    tool_id: str,
    tool_result: str = "",
    tool_action: str = "",
    tool_target: str = "",
    tool_outcome: str = "",
    tool_needs_user: str = "",
    tool_summary: str = "",
    tool_task_id: str = "",
    tool_task_status: str = "",
    tool_task_summary: str = "",
    failed: bool = False,
) -> None:
    if sink is None:
        return
    now_ms = int(time.time() * 1000)
    common = {
        "session_id": session_id,
        "source": "deskmate",
        "ts_ms": now_ms,
        "title": "Deskmate tools",
        "summary": summary,
        "raw_event": raw_event,
        "tool_name": tool_name,
        "tool_id": tool_id,
        "tool_result": tool_result,
        "tool_action": tool_action,
        "tool_target": tool_target,
        "tool_outcome": tool_outcome,
        "tool_needs_user": tool_needs_user,
        "tool_summary": tool_summary,
        "tool_task_id": tool_task_id,
        "tool_task_status": tool_task_status,
        "tool_task_summary": tool_task_summary,
    }
    try:
        result: Awaitable[None] | None
        if phase in {SessionPhase.COMPLETED, SessionPhase.FAILED}:
            result = sink(SessionCompleted(**common, failed=failed))
        else:
            result = sink(SessionActivityUpdated(**common, phase=phase))
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 — observability must not break chat
        _LOGGER.warning(
            "llm_composer.tool_event_sink_error",
            tool_name=tool_name,
            tool_call_id=tool_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )


def _tool_session_id(conversation_id: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", conversation_id.strip() or "default")
    safe = safe.strip("-")[:64] or "default"
    return f"deskmate-tools-{safe}"


def _tool_task_id(conversation_id: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", conversation_id.strip() or "default")
    safe = safe.strip("-")[:48] or "default"
    return f"deskmate-tool-task-{safe}-{int(time.time() * 1000)}"


def _extract_delta_content(chunk: dict[str, Any]) -> str | None:
    """Pull the incremental content out of a single SSE chunk.

    OpenAI / OpenRouter / vLLM all use the
    ``choices[0].delta.content`` shape. Ollama's ``/v1`` proxy
    sometimes echoes ``choices[0].message.content`` for the final
    chunk; handle both so the same composer works against any of
    them.
    """
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    delta = first.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content")
        if isinstance(content, str):
            return content
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def _accumulate_stream_tool_calls(
    chunk: dict[str, Any],
    out: dict[int, dict[str, Any]],
) -> None:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return
    first = choices[0]
    if not isinstance(first, dict):
        return
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return
    tool_calls = delta.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    for fallback_index, partial in enumerate(tool_calls):
        if not isinstance(partial, dict):
            continue
        raw_index = partial.get("index", fallback_index)
        index = raw_index if isinstance(raw_index, int) else fallback_index
        current = out.setdefault(
            index,
            {"type": "function", "function": {"name": "", "arguments": ""}},
        )
        if partial.get("id"):
            current["id"] = str(partial["id"])
        if partial.get("type"):
            current["type"] = str(partial["type"])
        function = partial.get("function")
        if isinstance(function, dict):
            current_function = current.setdefault("function", {})
            name = function.get("name")
            if isinstance(name, str) and name:
                current_function["name"] = name
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                current_function["arguments"] = (
                    str(current_function.get("arguments") or "") + arguments
                )


def _stream_tool_calls_to_wire(
    accumulated: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for index in sorted(accumulated):
        call = accumulated[index]
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        calls.append(
            {
                "id": str(call.get("id") or f"tool-call-{index}"),
                "type": str(call.get("type") or "function"),
                "function": {
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                },
            }
        )
    return calls


def _resolve_tiered_model(skill_mode: SkillMode) -> str:
    """V10 L3-B3: pick the model env override for ``skill_mode``,
    falling back to ``DESKMATE_LLM_MODEL`` when no tier-specific
    override is configured. Always returns a non-empty string so
    callers can plug it directly into the chat-completions request.
    """
    base = os.environ.get("DESKMATE_LLM_MODEL", "gpt-4o-mini").strip()
    if skill_mode == "proactive":
        override = os.environ.get(
            "DESKMATE_LLM_MODEL_PROACTIVE", ""
        ).strip()
    else:
        override = os.environ.get(
            "DESKMATE_LLM_MODEL_REACTIVE", ""
        ).strip()
    return override or base


def _resolve_tool_timeout() -> float | None:
    raw = os.environ.get("DESKMATE_LLM_TOOL_TIMEOUT_S", "").strip()
    if not raw:
        return 8.0
    if raw.lower() in {"0", "off", "false", "none", "disabled"}:
        return None
    try:
        value = float(raw)
    except ValueError:
        _LOGGER.warning("llm_composer.invalid_tool_timeout", value=raw)
        return 8.0
    return value if value > 0 else None


def _resolve_tool_round_limit() -> int:
    raw = os.environ.get("DESKMATE_LLM_TOOL_ROUND_LIMIT", "").strip()
    if not raw:
        return 3
    try:
        value = int(raw)
    except ValueError:
        _LOGGER.warning("llm_composer.invalid_tool_round_limit", value=raw)
        return 3
    return max(1, min(value, 5))


def make_default_composer(
    *,
    skill_registry: SkillRegistry | None = None,
    skill_mode: SkillMode = "reactive",
    approval_store: Any | None = None,
    pending_computer_actions: PendingComputerActionStore | None = None,
    computer_control_clock: Callable[[], int] | None = None,
    reminder_store: Any | None = None,
    reminder_control_clock: Callable[[], int] | None = None,
    reminder_id_factory: Callable[[], str] | None = None,
    chat_memory: ChatMemory | None = None,
    tool_action_log: ToolActionLog | None = None,
    task_store: DeskmateTaskStore | None = None,
    profile_store: ProfileStore | None = None,
    conversation_id: str = "default",
    tool_event_sink: ToolEventSink | None = None,
) -> ReplyComposer:
    """Pick the LLM composer if ``DESKMATE_LLM_API_KEY`` is set,
    otherwise fall back to the canned composer.

    Recognized env vars:

    - ``DESKMATE_LLM_API_KEY`` — required for the LLM path.
    - ``DESKMATE_LLM_BASE_URL`` — default ``https://api.openai.com/v1``.
    - ``DESKMATE_LLM_MODEL`` — default ``gpt-4o-mini`` (used as the
      tiered fallback).
    - ``DESKMATE_LLM_MODEL_REACTIVE`` — V10 L3-B3 override picked
      when ``skill_mode == "reactive"``.
    - ``DESKMATE_LLM_MODEL_PROACTIVE`` — V10 L3-B3 override picked
      when ``skill_mode == "proactive"``. Lets a deployment send the
      cheap idle-nudge path through ``gpt-4o-mini`` while keeping
      the user-driven chat path on a larger reactive model.

    When ``skill_registry`` is passed the LLM path wires it through
    to :func:`openai_compat_composer`. The canned path ignores it —
    the canned composer doesn't have a prompt to inject into.

    V10 L2-#8A · ``skill_mode`` is forwarded to
    :func:`openai_compat_composer` so a proactive caller (future
    LLM-backed nudge composer) can pin ``"proactive"`` and the
    registry will hide write-capable skills automatically.
    """
    api_key = os.environ.get("DESKMATE_LLM_API_KEY", "").strip()
    if not api_key:
        return task_control_composer(
            task_store=task_store,
            tool_action_log=tool_action_log,
            conversation_id=conversation_id,
            fallback=memory_control_composer(
                profile_store=profile_store,
                chat_memory=chat_memory,
                conversation_id=conversation_id,
                clock=reminder_control_clock,
                fallback=reminder_control_composer(
                    reminder_store=reminder_store,
                    clock=reminder_control_clock,
                    id_factory=reminder_id_factory,
                    fallback=computer_control_composer(
                        approval_store=approval_store,
                        pending_actions=pending_computer_actions,
                        clock=computer_control_clock,
                        fallback=memory_suggestion_composer(
                            approval_store=approval_store,
                            profile_store=profile_store,
                            conversation_id=conversation_id,
                            clock=reminder_control_clock,
                            fallback=canned_reply_composer(),
                        ),
                    ),
                ),
            ),
        )

    base_url = os.environ.get(
        "DESKMATE_LLM_BASE_URL", "https://api.openai.com/v1"
    ).strip()
    model = _resolve_tiered_model(skill_mode)
    tool_timeout_s = _resolve_tool_timeout()
    tool_round_limit = _resolve_tool_round_limit()

    _LOGGER.info(
        "llm_composer.activated",
        base_url=base_url,
        model=model,
        skill_mode=skill_mode,
        tool_timeout_s=tool_timeout_s,
        tool_round_limit=tool_round_limit,
    )
    return task_control_composer(
        task_store=task_store,
        tool_action_log=tool_action_log,
        conversation_id=conversation_id,
        fallback=memory_control_composer(
            profile_store=profile_store,
            chat_memory=chat_memory,
            conversation_id=conversation_id,
            clock=reminder_control_clock,
            fallback=reminder_control_composer(
                reminder_store=reminder_store,
                clock=reminder_control_clock,
                id_factory=reminder_id_factory,
                fallback=computer_control_composer(
                    approval_store=approval_store,
                    pending_actions=pending_computer_actions,
                    clock=computer_control_clock,
                    fallback=memory_suggestion_composer(
                        approval_store=approval_store,
                        profile_store=profile_store,
                        conversation_id=conversation_id,
                        clock=reminder_control_clock,
                        fallback=openai_compat_composer(
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            fallback=canned_reply_composer(),
                            skill_registry=skill_registry,
                            skill_mode=skill_mode,
                            chat_memory=chat_memory,
                            task_store=task_store,
                            tool_action_log=tool_action_log,
                            profile_store=profile_store,
                            conversation_id=conversation_id,
                            tool_event_sink=tool_event_sink,
                            tool_timeout_s=tool_timeout_s,
                            tool_round_limit=tool_round_limit,
                            tool_executor=DeskmateToolExecutor(
                                reminder_store=reminder_store,
                                reminder_clock=reminder_control_clock,
                                reminder_id_factory=reminder_id_factory,
                                approval_store=approval_store,
                                pending_computer_actions=pending_computer_actions,
                                computer_control_clock=computer_control_clock,
                                profile_store=profile_store,
                                chat_memory=chat_memory,
                                task_store=task_store,
                                tool_action_log=tool_action_log,
                                conversation_id=conversation_id,
                            ),
                        ),
                    ),
                ),
            ),
        )
    )


def make_default_streaming_composer(
    *,
    skill_registry: SkillRegistry | None = None,
    skill_mode: SkillMode = "reactive",
    approval_store: Any | None = None,
    pending_computer_actions: PendingComputerActionStore | None = None,
    computer_control_clock: Callable[[], int] | None = None,
    reminder_store: Any | None = None,
    reminder_control_clock: Callable[[], int] | None = None,
    reminder_id_factory: Callable[[], str] | None = None,
    chat_memory: ChatMemory | None = None,
    tool_action_log: ToolActionLog | None = None,
    task_store: DeskmateTaskStore | None = None,
    profile_store: ProfileStore | None = None,
    conversation_id: str = "default",
    tool_event_sink: ToolEventSink | None = None,
) -> StreamingReplyComposer | None:
    """V10 L3-B1: pick the streaming LLM composer when the API key
    is configured, otherwise return ``None`` so the dispatcher
    transparently falls back to the (canned or non-streaming) sync
    composer.

    Same env vars as :func:`make_default_composer`. ``DESKMATE_LLM_STREAMING``
    set to ``"0"`` / ``"false"`` / ``"off"`` (case-insensitive) opts
    a key-holder out of streaming and back to the round-trip path.
    """
    api_key = os.environ.get("DESKMATE_LLM_API_KEY", "").strip()
    if not api_key:
        return None

    streaming_flag = os.environ.get("DESKMATE_LLM_STREAMING", "1").strip().lower()
    if streaming_flag in {"0", "false", "off", "no"}:
        return None

    base_url = os.environ.get(
        "DESKMATE_LLM_BASE_URL", "https://api.openai.com/v1"
    ).strip()
    model = _resolve_tiered_model(skill_mode)
    tool_timeout_s = _resolve_tool_timeout()
    tool_round_limit = _resolve_tool_round_limit()

    _LOGGER.info(
        "llm_composer.streaming_activated",
        base_url=base_url,
        model=model,
        skill_mode=skill_mode,
        tool_timeout_s=tool_timeout_s,
        tool_round_limit=tool_round_limit,
    )
    return task_control_streaming_composer(
        task_store=task_store,
        tool_action_log=tool_action_log,
        conversation_id=conversation_id,
        fallback=memory_control_streaming_composer(
            profile_store=profile_store,
            chat_memory=chat_memory,
            conversation_id=conversation_id,
            clock=reminder_control_clock,
            fallback=reminder_control_streaming_composer(
                reminder_store=reminder_store,
                clock=reminder_control_clock,
                id_factory=reminder_id_factory,
                fallback=computer_control_streaming_composer(
                    approval_store=approval_store,
                    pending_actions=pending_computer_actions,
                    clock=computer_control_clock,
                    fallback=memory_suggestion_streaming_composer(
                        approval_store=approval_store,
                        profile_store=profile_store,
                        conversation_id=conversation_id,
                        clock=reminder_control_clock,
                        fallback=openai_compat_streaming_composer(
                            base_url=base_url,
                            api_key=api_key,
                            model=model,
                            fallback=canned_reply_composer(),
                            skill_registry=skill_registry,
                            skill_mode=skill_mode,
                            chat_memory=chat_memory,
                            task_store=task_store,
                            tool_action_log=tool_action_log,
                            profile_store=profile_store,
                            conversation_id=conversation_id,
                            tool_event_sink=tool_event_sink,
                            tool_timeout_s=tool_timeout_s,
                            tool_round_limit=tool_round_limit,
                            tool_executor=DeskmateToolExecutor(
                                reminder_store=reminder_store,
                                reminder_clock=reminder_control_clock,
                                reminder_id_factory=reminder_id_factory,
                                approval_store=approval_store,
                                pending_computer_actions=pending_computer_actions,
                                computer_control_clock=computer_control_clock,
                                profile_store=profile_store,
                                chat_memory=chat_memory,
                                task_store=task_store,
                                tool_action_log=tool_action_log,
                                conversation_id=conversation_id,
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


async def default_llm_prewarm(
    *,
    timeout_s: float = 5.0,
) -> None:
    """V10 L3-B1: warm the TLS / HTTP/2 / model loader path the
    moment the agent boots, so the first real user turn does not
    pay the handshake tax.

    No-op when ``DESKMATE_LLM_API_KEY`` is missing. Always fail
    soft: any failure logs a warning and returns. Total time bounded
    by ``timeout_s`` so a stuck endpoint never blocks ``agent.ready``.

    The probe is a real ``max_tokens=1`` chat completion (not just a
    HEAD): TLS + auth + model-route caches all need at least one
    successful round trip to pre-fault their slabs.
    """
    api_key = os.environ.get("DESKMATE_LLM_API_KEY", "").strip()
    if not api_key:
        return
    base_url = os.environ.get(
        "DESKMATE_LLM_BASE_URL", "https://api.openai.com/v1"
    ).strip()
    model = os.environ.get("DESKMATE_LLM_MODEL", "gpt-4o-mini").strip()
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "ping"},
                        {"role": "user", "content": "ping"},
                    ],
                    "max_tokens": 1,
                },
            )
            resp.raise_for_status()
        _LOGGER.info(
            "llm_prewarm.ok",
            base_url=base_url,
            model=model,
            seconds=time.perf_counter() - started,
        )
    except Exception as exc:  # noqa: BLE001 — prewarm is best effort
        _LOGGER.warning(
            "llm_prewarm.failed",
            base_url=base_url,
            model=model,
            error=str(exc),
            error_type=type(exc).__name__,
            seconds=time.perf_counter() - started,
        )


__all__ = [
    "default_llm_prewarm",
    "make_default_composer",
    "make_default_streaming_composer",
    "openai_compat_composer",
    "openai_compat_streaming_composer",
]
