"""Dispatcher: separates the reactive and proactive chains (V10 L2-#6).

The two chains have deliberately different policies:

- :meth:`Dispatcher.on_user_message` — reactive chain. Fires immediately,
  never gated by cooldown / quota / focus. The Orchestrator later composes
  prompts + tools; Phase 1a only ships the routing layer.
- :meth:`Dispatcher.on_perception_tick` — proactive chain. Fully mediated by
  :class:`ProactiveEngine` (rule pre-filter + decision engine + cooldown).

Routing emits :class:`CompanionIntent` values through an ``IntentSink`` —
never raw UI messages. V10 L1-C forbids Python from constructing view-level
instructions like ``pet.speak``; only intents cross the boundary.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from .context import PerceptionSnapshot, ProactiveContext
from .logging_setup import get_logger
from .perception_deduper import PerceptionDeduper
from .proactive.engine import ProactiveEngine, ProactiveResult
from .proactive.nudges import NudgeSelector
from .protocol.intents import CompanionIntent, IntentKind
from .protocol.state import BubbleKind, BubbleSpec, Priority

_LOGGER = get_logger(__name__)

IntentSink = Callable[[CompanionIntent], Awaitable[None]]

# V10 Phase 12-i: pluggable composer for the reactive chat reply. A
# return value of ``None`` means "no content to render" — the
# dispatcher falls back to the short placeholder bubble so the user
# still gets visible feedback. Skill-level adapters (LLM, rules,
# canned) all plug in through this single seam.
ReplyComposer = Callable[[str], Awaitable[str | None]]

# V10 L3-B1 streaming composer. Returns an async iterator that
# yields ``str`` tokens (or partial chunks of any size). The
# dispatcher accumulates them into the bubble's ``text`` field via
# UPDATE_PET_BUBBLE intents, throttled to ~50ms windows so a fast
# token stream doesn't blow past V10 L3-A9's bubble update budget.
StreamingReplyComposer = Callable[[str], AsyncIterator[str]]

# V10 L3-A9 / L3-B1: minimum interval between UPDATE_PET_BUBBLE
# emissions for the same in-flight reply bubble. Tokens that arrive
# within the window get coalesced into the next emission.
_STREAM_FLUSH_INTERVAL_MS = 50

# V10 Phase 13-i: side-channel observer invoked once per
# :meth:`Dispatcher.on_perception_tick`. Skills that react to OS
# signals (foreground app, idle duration, …) plug in here without
# touching the proactive chain. They run **before** the proactive
# decision so any island / bubble intents they emit land first on
# the wire.
PerceptionObserver = Callable[[PerceptionSnapshot], Awaitable[None]]


@dataclass
class DispatchStats:
    user_messages: int = 0
    perception_ticks: int = 0
    proactive_triggers: int = 0
    proactive_blocked: int = 0
    # V10 Phase 9 · §4 step 3: perception ticks the deduper rejected.
    perception_ticks_dropped: int = 0


class Dispatcher:
    """Thin router over the two chains + an intent sink.

    Optionally accepts a :data:`ReplyComposer` for the reactive chain
    (Phase 12-i). When present, ``on_user_message`` awaits the
    composer before emitting the chat bubble so the user sees real
    content instead of the ``"…"`` placeholder. When absent (pure
    routing harness, tests, early bring-up) the dispatcher degrades
    to the Phase 11d-x ack-bubble behaviour.
    """

    def __init__(
        self,
        proactive: ProactiveEngine,
        intent_sink: IntentSink,
        reply_composer: ReplyComposer | None = None,
        perception_observers: list[PerceptionObserver] | None = None,
        nudge_selector: NudgeSelector | None = None,
        perception_deduper: PerceptionDeduper | None = None,
        streaming_reply_composer: StreamingReplyComposer | None = None,
        stream_flush_interval_ms: int = _STREAM_FLUSH_INTERVAL_MS,
    ) -> None:
        self._proactive = proactive
        self._sink = intent_sink
        self._reply_composer = reply_composer
        self._streaming_composer = streaming_reply_composer
        self._stream_flush_interval_ms = max(0, int(stream_flush_interval_ms))
        self._perception_observers = list(perception_observers or [])
        self._nudge_selector = nudge_selector
        # V10 Phase 9 · §4 step 3: optional deduper that drops
        # near-duplicate ticks. Defaults to ``None`` so existing tests
        # keep their tick-by-tick behaviour; production wires a real
        # deduper from :class:`DegradationController`.
        self._perception_deduper = perception_deduper
        self.stats = DispatchStats()
        # V10 L2-#1: latest perception snapshot, captured at the head
        # of every ``on_perception_tick`` so other components (e.g.
        # :class:`IslandNotificationPublisher`) can read "who's
        # frontmost right now?" without subscribing to the perception
        # observer chain themselves.
        self._last_perception: PerceptionSnapshot | None = None

    @property
    def last_perception(self) -> PerceptionSnapshot | None:
        """Return the most recent :class:`PerceptionSnapshot` seen by
        the proactive chain, or ``None`` before the first tick."""
        return self._last_perception

    # ------------------------------------------------------------------
    # Reactive chain — user-initiated
    # ------------------------------------------------------------------

    async def on_user_message(self, text: str, *, trace_id: str | None = None) -> None:
        """Immediate chain. Bypasses cooldown / quota / focus checks.

        Phase 12-iii "typewriter" effect:

        1. Kick off the ``thinking`` animation.
        2. Emit a placeholder ``"…"`` bubble **immediately** so the
           user sees instant feedback regardless of composer latency.
        3. If a streaming composer is plugged in, await its first
           token, swap the placeholder for a real reply bubble
           carrying that token, and stream subsequent tokens into
           the same bubble id via ``UPDATE_PET_BUBBLE`` intents.
           Coalesce updates into ~50 ms windows so a fast LLM
           doesn't blow past V10 L3-A9's bubble update budget.
        4. Otherwise, if a non-streaming composer is plugged in,
           await it and atomically swap the placeholder for the
           full reply.
        5. With no composer the placeholder is the final state.
        """
        self.stats.user_messages += 1

        # 1. Thinking animation — flip pet art before the bubble.
        await self._sink(
            CompanionIntent(
                kind=IntentKind.SET_PET_ANIMATION,
                payload={"state": "thinking"},
            )
        )

        # 2. Placeholder bubble. A generous TTL covers slow LLMs;
        # a successful composer will DISMISS explicitly below.
        ack_id = "user-msg-ack"
        ack = BubbleSpec(
            id=ack_id,
            kind=BubbleKind.CHAT,
            text="…",
            ttl_ms=30_000,
            priority=Priority.P2,
            source_event_id=(trace_id or None),
        )
        await self._sink(
            CompanionIntent(
                kind=IntentKind.SHOW_PET_BUBBLE,
                payload={"bubble": ack.model_dump(mode="json")},
            )
        )

        # 3. Streaming path wins when both are configured: the
        # canned-fallback case in :func:`make_default_composer`
        # already wires the streaming composer's fallback to the
        # canned non-streaming one.
        if self._streaming_composer is not None:
            await self._stream_reply(text, ack_id=ack_id, trace_id=trace_id)
            return

        # 4. No streaming composer → original full-await path.
        if self._reply_composer is None:
            return

        reply = await self._reply_composer(text)
        if not reply:
            return

        # Atomic swap: dismiss placeholder THEN show the real reply.
        # Separate ``bubble_id`` lets Swift's ``LivePendingBubbleQueue``
        # identify the outgoing entry — same-id replacement was added
        # in V10 L3-B1 specifically for streaming, but the legacy
        # full-reply path keeps using DISMISS+SHOW for clarity.
        await self._sink(
            CompanionIntent(
                kind=IntentKind.DISMISS_PET_BUBBLE,
                payload={"bubble_id": ack_id},
            )
        )
        reply_bubble = BubbleSpec(
            id="user-msg-reply",
            kind=BubbleKind.CHAT,
            text=reply,
            ttl_ms=8_000,
            priority=Priority.P2,
            source_event_id=(trace_id or None),
        )
        await self._sink(
            CompanionIntent(
                kind=IntentKind.SHOW_PET_BUBBLE,
                payload={"bubble": reply_bubble.model_dump(mode="json")},
            )
        )

    async def _stream_reply(
        self,
        text: str,
        *,
        ack_id: str,
        trace_id: str | None,
    ) -> None:
        """Drive the streaming reply chain (V10 L3-B1).

        Strategy:

        - Wait for the first token before dismissing the ack bubble
          so a composer that fails before any token arrives leaves
          the placeholder in place (its TTL retires it later).
        - Coalesce subsequent tokens into ~50 ms windows.
        - Always emit a final ``UPDATE_PET_BUBBLE`` with the full
          accumulated text so the bubble settles on the complete
          reply even if the last window's batch was small.
        """
        assert self._streaming_composer is not None  # narrowed for mypy
        reply_id = "user-msg-reply"

        accumulated: list[str] = []
        first_token: str | None = None
        try:
            stream = self._streaming_composer(text)
            if inspect.isawaitable(stream):
                # Composer returned an awaitable that resolves to an
                # iterator (some skill adapters do this for warm-up
                # plumbing). Await once before iterating.
                stream = await stream  # type: ignore[assignment]
            iterator = stream.__aiter__()
            while True:
                try:
                    chunk = await iterator.__anext__()
                except StopAsyncIteration:
                    break
                if chunk is None:
                    continue
                text_chunk = str(chunk)
                if not text_chunk:
                    continue
                if first_token is None:
                    first_token = text_chunk
                    accumulated.append(text_chunk)
                    # Swap placeholder → real reply bubble carrying
                    # the first token. From here on we patch in
                    # place via UPDATE_PET_BUBBLE.
                    await self._sink(
                        CompanionIntent(
                            kind=IntentKind.DISMISS_PET_BUBBLE,
                            payload={"bubble_id": ack_id},
                        )
                    )
                    reply_bubble = BubbleSpec(
                        id=reply_id,
                        kind=BubbleKind.CHAT,
                        text="".join(accumulated),
                        ttl_ms=8_000,
                        priority=Priority.P2,
                        source_event_id=(trace_id or None),
                    )
                    await self._sink(
                        CompanionIntent(
                            kind=IntentKind.SHOW_PET_BUBBLE,
                            payload={"bubble": reply_bubble.model_dump(mode="json")},
                        )
                    )
                    last_flush_ms = int(time.time() * 1000)
                    continue

                accumulated.append(text_chunk)
                now_ms = int(time.time() * 1000)
                if now_ms - last_flush_ms >= self._stream_flush_interval_ms:
                    await self._emit_bubble_patch(
                        bubble_id=reply_id,
                        text="".join(accumulated),
                    )
                    last_flush_ms = now_ms
        except Exception as exc:  # noqa: BLE001 — fail-soft, log and bail
            _LOGGER.warning(
                "dispatcher.streaming_composer_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )

        if first_token is None:
            # No tokens at all — leave the placeholder, its TTL will
            # retire it. (The streaming composer's fallback path
            # should have already emitted a non-empty stream when
            # falling back to the canned composer.)
            return

        final_text = "".join(accumulated)
        # Always emit the final state, even if it equals the last
        # patched text — this is the "settled reply" Swift relies on
        # to know the stream is done. Cheap; the queue's update is
        # a no-op when the entry has aged out.
        await self._emit_bubble_patch(
            bubble_id=reply_id,
            text=final_text,
        )

    async def _emit_bubble_patch(
        self,
        *,
        bubble_id: str,
        text: str,
    ) -> None:
        await self._sink(
            CompanionIntent(
                kind=IntentKind.UPDATE_PET_BUBBLE,
                payload={
                    "bubble_id": bubble_id,
                    "text": text,
                },
            )
        )

    async def on_user_click_pet(self) -> None:
        self.stats.user_messages += 1
        # Phase 11d-x: emit a fully-formed BubbleSpec wrapped in the
        # canonical ``{"bubble": {...}}`` intent payload so Swift's
        # :class:`CompanionIntentDispatcher.bindBubbleQueue` can decode
        # and enqueue it (the previous flat-payload shape tripped the
        # decode path and never rendered).
        bubble = BubbleSpec(
            id="user-click-pet-greeting",
            kind=BubbleKind.CHAT,
            text="👋",
            ttl_ms=4_000,
            priority=Priority.P2,
        )
        await self._sink(
            CompanionIntent(
                kind=IntentKind.SHOW_PET_BUBBLE,
                payload={"bubble": bubble.model_dump(mode="json")},
            )
        )

    # ------------------------------------------------------------------
    # Proactive chain — perception-initiated
    # ------------------------------------------------------------------

    async def on_perception_tick(self, ctx: ProactiveContext) -> ProactiveResult:
        # V10 L2-#1: stash the latest snapshot before any gating so
        # any code that wants to know "is the user already in the
        # active session's window?" gets a fresh answer even on a
        # dedup-dropped tick.
        self._last_perception = ctx.perception

        # V10 Phase 9 · §4 step 3: let the deduper coalesce ticks
        # that share a fingerprint with the last accepted one and
        # arrive within the (degradation-widened) gap. Dropped ticks
        # skip observers + the proactive engine entirely.
        if self._perception_deduper is not None:
            now_ms = int(time.time() * 1000)
            if not self._perception_deduper.should_accept(
                ctx.perception, now_ms=now_ms
            ):
                self.stats.perception_ticks_dropped += 1
                return ProactiveResult(
                    should_trigger=False,
                    reason="deduped",
                )

        # Phase 13-i: let side-channel observers react to the latest
        # perception before the proactive engine runs. Observer
        # exceptions never block the main tick — they get logged and
        # the tick continues, since losing a proactive turn because of
        # an unrelated skill bug would be overkill.
        for observer in self._perception_observers:
            try:
                await observer(ctx.perception)
            except Exception as exc:  # noqa: BLE001 — fail-soft per observer
                _LOGGER.warning(
                    "perception_observer.error",
                    observer=getattr(observer, "__name__", repr(observer)),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

        self.stats.perception_ticks += 1
        result = await self._proactive.maybe_trigger(ctx)
        if result.should_trigger:
            self.stats.proactive_triggers += 1
            self._proactive.record_trigger()
            await self._sink(
                CompanionIntent(
                    kind=IntentKind.SET_AVATAR_MOOD,
                    payload={"mood": "proactive"},
                )
            )
            # Phase 16-i: actually speak. The nudge selector is
            # optional so existing test harnesses (which care about
            # the mood transition, not the bubble) keep seeing a
            # single intent; production wires a concrete selector.
            if self._nudge_selector is not None:
                try:
                    text = await self._nudge_selector.select(ctx)
                except Exception as exc:  # noqa: BLE001 — fail-soft
                    _LOGGER.warning(
                        "nudge_selector.error",
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    text = None
                if text:
                    bubble = BubbleSpec(
                        id=f"proactive-nudge-{ctx.perception.ts_ms}",
                        kind=BubbleKind.CHAT,
                        text=text,
                        ttl_ms=10_000,
                        priority=Priority.P2,
                    )
                    await self._sink(
                        CompanionIntent(
                            kind=IntentKind.SHOW_PET_BUBBLE,
                            payload={
                                "bubble": bubble.model_dump(mode="json")
                            },
                        )
                    )
        else:
            self.stats.proactive_blocked += 1
        return result


__all__ = [
    "Dispatcher",
    "DispatchStats",
    "IntentSink",
    "ReplyComposer",
    "StreamingReplyComposer",
]
