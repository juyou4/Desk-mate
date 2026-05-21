"""Skill metadata registry (V10 Phase 10).

The agent ships a growing set of *skills* (chat composer, build-status
watcher, coding-session tracker, …). Loading every skill's prompt /
code at startup bloats RSS and warms irrelevant caches, but the LLM
orchestrator still needs to know *what* is available to answer /
route / hand off to.

The registry resolves that tension by splitting each skill into two
artefacts:

- :class:`SkillMetadata` — a lightweight record kept in memory for
  the whole process lifetime. Just enough to describe *what* the skill
  does and when it should trigger. Cheap to scan on every user turn.
- :class:`SkillBody` — the "heavy" bits (system prompt fragments,
  context hints, future tool definitions). Produced by a lazy
  ``body_loader`` on first use and cached until process exit.

The LLM composer (:mod:`deskmate_agent.skills.llm_chat`) consults
``registry.select(text)`` on every user turn; matched metadata ids
drive ``registry.load_body`` calls whose returned system prompt
fragments are concatenated into the request's ``system`` message.
Non-matching skills never pay their body cost.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from ..logging_setup import get_logger

_LOGGER = get_logger(__name__)


CostClass = str  # "free" | "llm_tokens" | "network" | "disk"
Capability = str  # "chat" | "perception_observer" | "cli_tool" | "watcher"

#: V10 L2-#8A. The agent runs in two distinct modes:
#:
#: - ``"reactive"`` — the user just typed something, the full skill
#:   catalog is fair game (including future write / mutating skills
#:   that take effect on the user's machine).
#: - ``"proactive"`` — the agent is speaking unprompted (idle nudge,
#:   reminder, periodic check-in). Only skills explicitly tagged
#:   ``proactive_safe=True`` get a chance to influence the prompt or
#:   run; everything else is filtered out so an unattended agent
#:   can't drive a write skill on its own initiative.
SkillMode = Literal["reactive", "proactive"]


@dataclass(frozen=True)
class SkillMetadata:
    """Always-resident record describing one skill.

    ``triggers`` drives :meth:`SkillRegistry.select` — currently a
    case-insensitive substring match, chosen deliberately over a
    regex / embedding path for MVP determinism and testability.

    ``proactive_safe`` (V10 L2-#8A) is conservative-by-default:
    *every* skill must explicitly opt into the proactive set. This
    is so a third-party pack that ships a write-capable skill without
    knowing about the proactive contract can never hurt a user — the
    agent silently treats it as reactive-only until the pack author
    flips the flag deliberately.
    """

    id: str
    title: str
    summary: str
    triggers: tuple[str, ...] = ()
    capabilities: tuple[Capability, ...] = ()
    cost_class: CostClass = "free"
    version: str = "0.1.0"
    proactive_safe: bool = False


@dataclass
class SkillBody:
    """On-demand body of a skill — loaded lazily, cached on hit.

    Kept deliberately minimal: the current consumer (LLM composer)
    only needs a system-prompt fragment + a few extra context hints.
    Tool definitions + code handles land here in a later phase, but
    their addition must remain backward-compatible (hence the
    optional fields below).
    """

    system_prompt: str | None = None
    context_hints: tuple[str, ...] = ()


BodyLoader = Callable[[], Awaitable[SkillBody]]


class SkillRegistry:
    """In-memory catalog of :class:`SkillMetadata` entries.

    Thread-safety: all mutations happen on the main asyncio loop
    during app setup — no locks needed. Readers (``select``,
    ``metadata_for``, ``load_body``) may run from multiple tasks and
    rely on dict atomicity for lookups plus an ``asyncio.Lock`` per
    body to serialize concurrent first-loads.
    """

    def __init__(self, *, body_timeout_s: float | None = 1.0) -> None:
        if body_timeout_s is not None and body_timeout_s <= 0:
            raise ValueError("body_timeout_s must be > 0")
        self._body_timeout_s = body_timeout_s
        self._metadata: dict[str, SkillMetadata] = {}
        self._loaders: dict[str, BodyLoader] = {}
        self._body_cache: dict[str, SkillBody] = {}
        self._body_locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def register(
        self,
        metadata: SkillMetadata,
        *,
        body_loader: BodyLoader | None = None,
    ) -> None:
        """Register (or replace) a skill's metadata + optional loader.

        Re-registering the same id is treated as an intentional
        override (tests + character packs do this) — we drop any
        cached body so the next ``load_body`` reruns the new loader.
        """
        if metadata.id in self._metadata and (
            self._metadata[metadata.id] != metadata
            or body_loader is not None
        ):
            self._body_cache.pop(metadata.id, None)
        self._metadata[metadata.id] = metadata
        if body_loader is not None:
            self._loaders[metadata.id] = body_loader
        else:
            # No loader = metadata-only skill (e.g. watcher with no
            # prompt injection). Remove any stale loader from a prior
            # registration so ``load_body`` returns ``None`` cleanly.
            self._loaders.pop(metadata.id, None)

    def unregister(self, skill_id: str) -> None:
        """Forget a skill id. Missing ids are silently ignored."""
        self._metadata.pop(skill_id, None)
        self._loaders.pop(skill_id, None)
        self._body_cache.pop(skill_id, None)
        self._body_locks.pop(skill_id, None)

    def clear_body_cache(self) -> None:
        """Drop every loaded body. Next ``load_body`` re-invokes the
        loader. Useful in tests + after a hot-reload."""
        self._body_cache.clear()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def all(self) -> list[SkillMetadata]:
        """Return every registered metadata entry in insertion order."""
        return list(self._metadata.values())

    def metadata_for(self, skill_id: str) -> SkillMetadata | None:
        return self._metadata.get(skill_id)

    def select(
        self,
        text: str,
        *,
        capabilities: tuple[str, ...] | None = None,
        mode: SkillMode = "reactive",
    ) -> list[SkillMetadata]:
        """Return every metadata whose triggers appear in ``text``.

        Matching is case-insensitive substring matching. Empty / all
        whitespace input returns ``[]`` (don't inject anything for a
        blank user message — save tokens).

        ``capabilities`` further narrows the result to skills that
        advertise every listed capability.

        ``mode`` (V10 L2-#8A) gates the result by safety class:

        - ``"reactive"`` (default): full catalog, no extra filter.
        - ``"proactive"``: only entries with
          ``proactive_safe=True`` are considered, so an agent
          speaking unprompted can never reach a write / mutating
          skill that hasn't explicitly opted in.

        The ``mode`` filter is applied *before* triggers and
        capabilities so a write-capable skill isn't even examined for
        a potential proactive turn.
        """
        if not text or not text.strip():
            return []
        lowered = text.lower()
        matches: list[SkillMetadata] = []
        for meta in self._metadata.values():
            if mode == "proactive" and not meta.proactive_safe:
                continue
            if not meta.triggers:
                continue
            if not any(
                trigger.lower() in lowered
                for trigger in meta.triggers
                if trigger
            ):
                continue
            if capabilities and not set(capabilities).issubset(
                set(meta.capabilities)
            ):
                continue
            matches.append(meta)
        return matches

    async def load_body(self, skill_id: str) -> SkillBody | None:
        """Return the cached body, loading it on first access.

        Returns ``None`` for a metadata-only skill (no loader
        registered) or when the loader raised — the error is logged
        but never re-raised, keeping the composer fail-soft.
        """
        if skill_id in self._body_cache:
            return self._body_cache[skill_id]
        loader = self._loaders.get(skill_id)
        if loader is None:
            return None
        lock = self._body_locks.setdefault(skill_id, asyncio.Lock())
        async with lock:
            if skill_id in self._body_cache:
                return self._body_cache[skill_id]
            return await self._load_body_uncached(skill_id, loader)

    async def _load_body_uncached(
        self, skill_id: str, loader: BodyLoader
    ) -> SkillBody | None:
        try:
            body = await (
                asyncio.wait_for(loader(), timeout=self._body_timeout_s)
                if self._body_timeout_s is not None
                else loader()
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft
            _LOGGER.warning(
                "skill_registry.body_loader_failed",
                skill_id=skill_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None
        self._body_cache[skill_id] = body
        return body


__all__ = [
    "BodyLoader",
    "Capability",
    "CostClass",
    "SkillBody",
    "SkillMetadata",
    "SkillMode",
    "SkillRegistry",
]
