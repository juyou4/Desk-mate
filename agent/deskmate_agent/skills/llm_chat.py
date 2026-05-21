"""LLM-backed chat skill (V10 Phase 12-ii).

A :data:`dispatcher.ReplyComposer` that posts to any OpenAI
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
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from typing import Any

import httpx

from ..dispatcher import ReplyComposer
from ..logging_setup import get_logger
from .canned_chat import canned_reply_composer
from .registry import SkillMode, SkillRegistry

_LOGGER = get_logger(__name__)

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
    history: list[dict[str, str]] = []

    def _trim() -> None:
        if len(history) > memory_window:
            del history[: len(history) - memory_window]

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

        # Tentatively record the user turn so window math stays
        # uniform across the success / failure branches below.
        history.append({"role": "user", "content": stripped})
        _trim()

        skill_injections = await _resolve_skill_injections(stripped)
        messages = [
            {"role": "system", "content": system_prompt},
            *skill_injections,
            *history,
        ]

        try:
            started = time.perf_counter()
            resp = await effective_client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            content = data["choices"][0]["message"]["content"]
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
        except Exception as exc:  # noqa: BLE001 — fail-soft is the point
            _LOGGER.warning(
                "llm_composer.error",
                model=model,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Retract the failed user turn so a subsequent call isn't
            # anchored to context the LLM never saw.
            if history and history[-1] is messages[-1]:
                history.pop()
            if fallback is not None:
                return await fallback(text)
            return None

        reply = (content or "").strip() or None
        if reply:
            history.append({"role": "assistant", "content": reply})
            _trim()
        return reply

    return compose


def make_default_composer(
    *,
    skill_registry: SkillRegistry | None = None,
    skill_mode: SkillMode = "reactive",
) -> ReplyComposer:
    """Pick the LLM composer if ``DESKMATE_LLM_API_KEY`` is set,
    otherwise fall back to the canned composer.

    Recognized env vars:

    - ``DESKMATE_LLM_API_KEY`` — required for the LLM path.
    - ``DESKMATE_LLM_BASE_URL`` — default ``https://api.openai.com/v1``.
    - ``DESKMATE_LLM_MODEL`` — default ``gpt-4o-mini``.

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
        return canned_reply_composer()

    base_url = os.environ.get(
        "DESKMATE_LLM_BASE_URL", "https://api.openai.com/v1"
    ).strip()
    model = os.environ.get("DESKMATE_LLM_MODEL", "gpt-4o-mini").strip()

    _LOGGER.info(
        "llm_composer.activated",
        base_url=base_url,
        model=model,
        skill_mode=skill_mode,
    )
    return openai_compat_composer(
        base_url=base_url,
        api_key=api_key,
        model=model,
        fallback=canned_reply_composer(),
        skill_registry=skill_registry,
        skill_mode=skill_mode,
    )


__all__ = ["openai_compat_composer", "make_default_composer"]
