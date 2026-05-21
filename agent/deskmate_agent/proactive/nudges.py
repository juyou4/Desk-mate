"""Proactive nudge selector (V10 Phase 16-i / 16-ii).

The proactive chain was wired end-to-end in earlier phases
(rule filter + decision engine + cooldown + avatar mood toggle),
but the pet never actually said anything out loud when it fired.
This module plugs that gap: the dispatcher calls
``NudgeSelector.select(ctx)`` after a successful proactive trigger
and emits the returned text as a ``SHOW_PET_BUBBLE`` intent.

Design notes:

- **Bucketed pools (16-ii).** Messages are grouped by tag; the
  selector picks a tag based on the current
  :class:`ProactiveContext`:
  ``long_day`` after 2+ hours of coding, ``long_idle`` after
  30+ minutes of inactivity, ``default`` otherwise. Each pool
  rotates independently so heavy coding days don't starve the
  generic rotation.
- **Composer fallback.** When the caller passes a
  :data:`NudgeComposer` (async), we ask it first — the LLM-backed
  composer from Phase 12 can be reused here for free. If the
  composer returns ``None`` (network error, empty reply, rate
  limited) we fall back to the bucketed rotation.
- **Deterministic rotation per tag.** Cycling through each pool by
  index keeps tests predictable; callers can reset all indexes via
  :meth:`rewind`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence

from ..context import ProactiveContext

NudgeTag = str

# Defaults picked so the tone escalates rather than scolds. Keep
# entries short so a 10-second peek at the notch delivers the whole
# message.
_DEFAULT_POOLS: Mapping[NudgeTag, tuple[str, ...]] = {
    "long_day": (
        "Big coding day — consider wrapping up soon 🛌",
        "Hours in — stretch, grab water 💧",
        "Long session detected. Rest your eyes 👀",
    ),
    "long_idle": (
        "Been a while — welcome back 👋",
        "Long pause. Everything OK?",
        "Still here? Jump back in whenever you're ready.",
    ),
    "default": (
        "Taking a break? Hydrate 💧",
        "Hey — still there?",
        "Stretch break — rest your eyes 👀",
        "Long pause. A short walk helps 🚶",
        "How's it going? 👋",
    ),
}

# Thresholds — kept constants so tests can reason about them.
LONG_DAY_CODING_MS = 2 * 60 * 60 * 1000   # 2 hours
LONG_IDLE_SECONDS = 30 * 60               # 30 minutes


NudgeComposer = Callable[[ProactiveContext], Awaitable[str | None]]


class NudgeSelector:
    """Pick a proactive nudge message for the current context."""

    def __init__(
        self,
        pools: Mapping[NudgeTag, Sequence[str]] | None = None,
        *,
        # Back-compat: some callers still hand in a flat list which we
        # treat as the default pool. Keyword-only so new callers can't
        # mix styles by accident.
        messages: Sequence[str] | None = None,
        composer: NudgeComposer | None = None,
        long_day_coding_ms: int = LONG_DAY_CODING_MS,
        long_idle_seconds: int = LONG_IDLE_SECONDS,
    ) -> None:
        if pools is None and messages is not None:
            pools = {"default": messages}
        raw = pools if pools is not None else _DEFAULT_POOLS
        self._pools: dict[NudgeTag, list[str]] = {
            tag: [m for m in items if m and m.strip()]
            for tag, items in raw.items()
        }
        self._pools.setdefault("default", [])
        self._rotation: dict[NudgeTag, int] = {t: 0 for t in self._pools}
        self._composer = composer
        self._long_day_coding_ms = max(0, int(long_day_coding_ms))
        self._long_idle_seconds = max(0, int(long_idle_seconds))

    async def select(self, ctx: ProactiveContext) -> str | None:
        """Return the next nudge text, or ``None`` if the pool is
        exhausted and no composer is configured."""
        if self._composer is not None:
            try:
                text = await self._composer(ctx)
            except Exception:  # noqa: BLE001 — fail-soft
                text = None
            if text is not None:
                stripped = text.strip()
                if stripped:
                    return stripped
                # Whitespace-only / empty composer output falls
                # through to the rotation below rather than returning
                # nothing.
        tag = self._tag_for(ctx)
        return self._next_from(tag)

    def tag_for(self, ctx: ProactiveContext) -> NudgeTag:
        """Exposed for tests / diagnostics; the dispatcher only needs
        :meth:`select`."""
        return self._tag_for(ctx)

    def rewind(self) -> None:
        """Reset every pool's rotation index (e.g. at start-of-day)."""
        self._rotation = {t: 0 for t in self._pools}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _tag_for(self, ctx: ProactiveContext) -> NudgeTag:
        # Order matters: "long_day" wins over "long_idle" because a
        # user who just did 3h of coding deserves the rest-oriented
        # copy even if they happen to have paused for a while.
        if (
            ctx.coding_today_ms >= self._long_day_coding_ms
            and self._pools.get("long_day")
        ):
            return "long_day"
        if (
            ctx.idle_seconds >= self._long_idle_seconds
            and self._pools.get("long_idle")
        ):
            return "long_idle"
        return "default"

    def _next_from(self, tag: NudgeTag) -> str | None:
        pool = self._pools.get(tag) or self._pools.get("default") or []
        if not pool:
            return None
        # If the preferred pool is empty, cascade to ``default``.
        effective_tag = tag if self._pools.get(tag) else "default"
        idx = self._rotation.get(effective_tag, 0)
        msg = pool[idx % len(pool)]
        self._rotation[effective_tag] = idx + 1
        return msg


__all__ = [
    "LONG_DAY_CODING_MS",
    "LONG_IDLE_SECONDS",
    "NudgeComposer",
    "NudgeSelector",
    "NudgeTag",
]
