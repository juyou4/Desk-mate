"""Dispatcher tests — user/proactive chain separation (V10 L2-#6)."""

from __future__ import annotations

import pytest

from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
from deskmate_agent.decision.base import DecisionEngine, DecisionOutcome, EngineKind
from deskmate_agent.dispatcher import Dispatcher, ReplyComposer
from deskmate_agent.perception_deduper import PerceptionDeduper
from deskmate_agent.proactive import CooldownTracker, ProactiveEngine, RuleFilter
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.protocol.state import UserFocus


class _AlwaysOnEngine(DecisionEngine):
    kind = EngineKind.THRESHOLD

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        return DecisionOutcome(
            should_respond=True,
            reason="always",
            engine=self.kind,
        )


class _NeverEngine(DecisionEngine):
    kind = EngineKind.THRESHOLD

    async def evaluate(self, ctx: ProactiveContext) -> DecisionOutcome:
        return DecisionOutcome(
            should_respond=False,
            reason="never",
            engine=self.kind,
        )


def _build_dispatcher(
    decision: DecisionEngine,
    *,
    cooldown: CooldownTracker | None = None,
    composer: ReplyComposer | None = None,
    nudge_selector=None,
    perception_observers=None,
    perception_deduper: PerceptionDeduper | None = None,
    streaming_composer=None,
    stream_flush_interval_ms: int = 50,
) -> tuple[Dispatcher, list[CompanionIntent]]:
    captured: list[CompanionIntent] = []

    async def sink(intent: CompanionIntent) -> None:
        captured.append(intent)

    cd = cooldown or CooldownTracker(min_cooldown_s=0, daily_quota=100)
    proactive = ProactiveEngine(
        decision_engine=decision,
        cooldown=cd,
        rule_filter=RuleFilter(cd, min_idle_seconds=60),
    )
    return (
        Dispatcher(
            proactive=proactive,
            intent_sink=sink,
            reply_composer=composer,
            nudge_selector=nudge_selector,
            perception_observers=perception_observers,
            perception_deduper=perception_deduper,
            streaming_reply_composer=streaming_composer,
            stream_flush_interval_ms=stream_flush_interval_ms,
        ),
        captured,
    )


def _ctx(
    *,
    idle_ms: int = 600_000,
    focus: UserFocus = UserFocus.CASUAL,
) -> ProactiveContext:
    return ProactiveContext(
        perception=PerceptionSnapshot(idle_ms=idle_ms, focus=focus),
    )


# ---------------------------------------------------------------------------
# Reactive chain — never gated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_message_bypasses_cooldown_and_emits_placeholder() -> None:
    """No composer → animation + placeholder bubble, that's it."""
    cooldown = CooldownTracker(min_cooldown_s=10_000, daily_quota=1)
    cooldown.record_trigger()  # pretend we already spoke very recently
    dispatcher, captured = _build_dispatcher(_AlwaysOnEngine(), cooldown=cooldown)

    await dispatcher.on_user_message("hello", trace_id="tr-1")

    assert dispatcher.stats.user_messages == 1
    assert dispatcher.stats.proactive_blocked == 0
    # Phase 12-iii: exactly two intents without a composer.
    assert [c.kind for c in captured] == [
        IntentKind.SET_PET_ANIMATION,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    assert captured[0].payload == {"state": "thinking"}

    ack = captured[1].payload["bubble"]
    assert ack["id"] == "user-msg-ack"
    assert ack["text"] == "…"
    # Placeholder TTL is generous so slow composers aren't cut off.
    assert ack["ttl_ms"] == 30_000
    assert ack["kind"] == "chat"
    assert ack["source_event_id"] == "tr-1"


@pytest.mark.asyncio
async def test_user_message_typewriter_with_composer_swaps_placeholder() -> None:
    """Composer reply → placeholder dismissed, reply bubble shown."""

    async def compose(text: str) -> str | None:
        return f"you said: {text}"

    dispatcher, captured = _build_dispatcher(_NeverEngine(), composer=compose)
    await dispatcher.on_user_message("hi", trace_id="tr-9")

    # Phase 12-iii typewriter sequence:
    # animation → placeholder → dismiss placeholder → reply
    assert [c.kind for c in captured] == [
        IntentKind.SET_PET_ANIMATION,
        IntentKind.SHOW_PET_BUBBLE,
        IntentKind.DISMISS_PET_BUBBLE,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    # Placeholder goes out first (for instant visual feedback).
    ack = captured[1].payload["bubble"]
    assert ack["id"] == "user-msg-ack"
    assert ack["text"] == "…"
    # Dismiss targets the same placeholder id.
    assert captured[2].payload == {"bubble_id": "user-msg-ack"}
    # Real reply replaces it.
    reply = captured[3].payload["bubble"]
    assert reply["id"] == "user-msg-reply"
    assert reply["text"] == "you said: hi"
    assert reply["ttl_ms"] == 8_000
    assert reply["source_event_id"] == "tr-9"


@pytest.mark.asyncio
async def test_user_message_typewriter_composer_returns_none_keeps_placeholder() -> None:
    """Composer returning None must leave the placeholder in place —
    no dismiss, no reply intent, just animation + placeholder."""

    async def compose(text: str) -> str | None:
        return None

    dispatcher, captured = _build_dispatcher(_NeverEngine(), composer=compose)
    await dispatcher.on_user_message("hi")

    assert [c.kind for c in captured] == [
        IntentKind.SET_PET_ANIMATION,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    ack = captured[1].payload["bubble"]
    assert ack["id"] == "user-msg-ack"
    assert ack["text"] == "…"


@pytest.mark.asyncio
async def test_user_click_pet_emits_wrapped_bubble_intent() -> None:
    dispatcher, captured = _build_dispatcher(_NeverEngine())
    await dispatcher.on_user_click_pet()
    assert captured[0].kind is IntentKind.SHOW_PET_BUBBLE
    # Canonical shape Swift's CompanionIntentDispatcher decodes: the
    # BubbleSpec must live under ``payload["bubble"]`` (not flattened).
    bubble = captured[0].payload["bubble"]
    assert bubble["id"] == "user-click-pet-greeting"
    assert bubble["text"] == "👋"
    assert bubble["kind"] == "chat"
    assert bubble["ttl_ms"] == 4_000


# ---------------------------------------------------------------------------
# Proactive chain — gated by rules + decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proactive_fires_when_rules_pass_and_engine_says_yes() -> None:
    dispatcher, captured = _build_dispatcher(_AlwaysOnEngine())
    result = await dispatcher.on_perception_tick(_ctx())

    assert result.should_trigger is True
    assert dispatcher.stats.proactive_triggers == 1
    assert captured[0].kind is IntentKind.SET_AVATAR_MOOD


@pytest.mark.asyncio
async def test_proactive_trigger_emits_nudge_bubble_when_selector_plugged() -> None:
    from deskmate_agent.proactive import NudgeSelector

    selector = NudgeSelector(messages=("Hydrate 💧",))
    dispatcher, captured = _build_dispatcher(
        _AlwaysOnEngine(), nudge_selector=selector
    )
    await dispatcher.on_perception_tick(_ctx())

    # Phase 16-i sequence: SET_AVATAR_MOOD first, then a bubble
    # carrying the nudge text with a P2 priority.
    assert [c.kind for c in captured] == [
        IntentKind.SET_AVATAR_MOOD,
        IntentKind.SHOW_PET_BUBBLE,
    ]
    bubble = captured[1].payload["bubble"]
    assert bubble["text"] == "Hydrate 💧"
    assert bubble["priority"] == "P2"
    assert bubble["ttl_ms"] == 10_000
    assert bubble["id"].startswith("proactive-nudge-")


@pytest.mark.asyncio
async def test_proactive_without_selector_keeps_mood_only_shape() -> None:
    dispatcher, captured = _build_dispatcher(_AlwaysOnEngine())
    await dispatcher.on_perception_tick(_ctx())
    # Zero nudge selector → no SHOW_PET_BUBBLE; existing callers
    # that relied on ``captured[0].kind == SET_AVATAR_MOOD`` still
    # see exactly one intent.
    assert [c.kind for c in captured] == [IntentKind.SET_AVATAR_MOOD]


@pytest.mark.asyncio
async def test_proactive_selector_failure_does_not_break_mood_intent() -> None:
    from deskmate_agent.proactive import NudgeSelector

    async def crashing_composer(ctx):
        raise RuntimeError("boom")

    selector = NudgeSelector(
        messages=(), composer=crashing_composer
    )
    dispatcher, captured = _build_dispatcher(
        _AlwaysOnEngine(), nudge_selector=selector
    )
    await dispatcher.on_perception_tick(_ctx())
    # Selector raised + empty rotation → no SHOW_PET_BUBBLE, but
    # SET_AVATAR_MOOD still fires so the pet still signals activity.
    assert [c.kind for c in captured] == [IntentKind.SET_AVATAR_MOOD]


@pytest.mark.asyncio
async def test_proactive_blocked_by_focused_user() -> None:
    dispatcher, captured = _build_dispatcher(_AlwaysOnEngine())
    result = await dispatcher.on_perception_tick(_ctx(focus=UserFocus.FOCUSED))

    assert result.should_trigger is False
    assert result.reason.startswith("rule:user_focused")
    assert dispatcher.stats.proactive_triggers == 0
    assert dispatcher.stats.proactive_blocked == 1
    assert captured == []


# ---------------------------------------------------------------------------
# V10 Phase 9 · §4 Step 3: perception deduper wiring
# ---------------------------------------------------------------------------


def _ctx_with(*, bundle: str, idle_ms: int = 600_000) -> ProactiveContext:
    """Helper for deduper tests — fingerprint diff is driven by ``bundle``."""
    return ProactiveContext(
        perception=PerceptionSnapshot(
            idle_ms=idle_ms,
            focus=UserFocus.CASUAL,
            app_bundle_id=bundle,
            window_title="w",
        ),
    )


@pytest.mark.asyncio
async def test_dedup_drops_identical_tick_within_gap() -> None:
    """An impossibly long gap means *every* same-fingerprint tick
    should be dropped after the first — verifies the deduper hooks
    the proactive chain *and* the observer chain."""
    observed: list[PerceptionSnapshot] = []

    async def observer(snap: PerceptionSnapshot) -> None:
        observed.append(snap)

    dispatcher, captured = _build_dispatcher(
        _AlwaysOnEngine(),
        perception_observers=[observer],
        perception_deduper=PerceptionDeduper(base_gap_ms=10**9),
    )

    first = await dispatcher.on_perception_tick(_ctx_with(bundle="a"))
    second = await dispatcher.on_perception_tick(_ctx_with(bundle="a"))

    # First tick fires the proactive chain + observer.
    assert first.should_trigger is True
    # Second tick is rejected before observers / proactive run.
    assert second.should_trigger is False
    assert second.reason == "deduped"
    assert len(observed) == 1
    assert dispatcher.stats.perception_ticks == 1
    assert dispatcher.stats.perception_ticks_dropped == 1


@pytest.mark.asyncio
async def test_dedup_passes_when_fingerprint_changes() -> None:
    """A new bundle id yields a fresh fingerprint — must always pass
    even within the dedup gap."""
    dispatcher, _ = _build_dispatcher(
        _AlwaysOnEngine(),
        perception_deduper=PerceptionDeduper(base_gap_ms=10**9),
    )

    a = await dispatcher.on_perception_tick(_ctx_with(bundle="a"))
    b = await dispatcher.on_perception_tick(_ctx_with(bundle="b"))

    # Both tick fingerprints differ → both fire.
    assert a.should_trigger is True
    assert b.should_trigger is True
    assert dispatcher.stats.perception_ticks == 2
    assert dispatcher.stats.perception_ticks_dropped == 0


@pytest.mark.asyncio
async def test_dedup_dropped_tick_still_updates_last_perception() -> None:
    """L2-#1 invariant: callers reading ``dispatcher.last_perception``
    must always see the freshest snapshot, even when the deduper
    rejected the tick. Otherwise downstream lookups (frontmost
    suppression, island notifications) would lag a tick."""
    dispatcher, _ = _build_dispatcher(
        _AlwaysOnEngine(),
        perception_deduper=PerceptionDeduper(base_gap_ms=10**9),
    )
    await dispatcher.on_perception_tick(
        _ctx_with(bundle="a", idle_ms=1_000)
    )
    # Same fingerprint, different idle_ms — dropped, but the snapshot
    # stored on the dispatcher should reflect the *new* idle value.
    await dispatcher.on_perception_tick(
        _ctx_with(bundle="a", idle_ms=2_000)
    )

    snap = dispatcher.last_perception
    assert snap is not None
    assert snap.idle_ms == 2_000
    assert dispatcher.stats.perception_ticks_dropped == 1


@pytest.mark.asyncio
async def test_proactive_charges_cooldown_once_fired() -> None:
    cooldown = CooldownTracker(min_cooldown_s=60, daily_quota=10)
    dispatcher, _ = _build_dispatcher(_AlwaysOnEngine(), cooldown=cooldown)

    # First tick fires → charges cooldown.
    await dispatcher.on_perception_tick(_ctx())
    assert cooldown.within_cooldown() is True

    # Second tick inside cooldown is blocked.
    result = await dispatcher.on_perception_tick(_ctx())
    assert result.should_trigger is False
    assert result.reason.startswith("rule:cooldown")


# ---------------------------------------------------------------------------
# V10 L3-B1: streaming reactive chain
# ---------------------------------------------------------------------------


def _make_token_stream(tokens: list[str]):
    """Build a no-arg-friendly streaming composer that yields tokens
    in order and then exits cleanly."""

    async def compose(text: str):
        for tok in tokens:
            yield tok

    return compose


@pytest.mark.asyncio
async def test_streaming_composer_emits_first_token_via_swap_then_patches() -> None:
    """Streaming path:

    1. SET_PET_ANIMATION thinking
    2. SHOW_PET_BUBBLE ack
    3. DISMISS_PET_BUBBLE ack (after first token)
    4. SHOW_PET_BUBBLE reply with first token
    5. UPDATE_PET_BUBBLE for each subsequent token (or coalesced batch)
    6. Final UPDATE_PET_BUBBLE carries the fully accumulated text.
    """

    composer = _make_token_stream(["He", "llo", " world"])
    dispatcher, captured = _build_dispatcher(
        _NeverEngine(),
        streaming_composer=composer,
        # 0ms flush so every token gets its own UPDATE_PET_BUBBLE,
        # making the order easy to assert.
        stream_flush_interval_ms=0,
    )

    await dispatcher.on_user_message("hi")

    kinds = [c.kind for c in captured]
    # Must start with the standard ack handshake.
    assert kinds[0] is IntentKind.SET_PET_ANIMATION
    assert kinds[1] is IntentKind.SHOW_PET_BUBBLE
    assert captured[1].payload["bubble"]["id"] == "user-msg-ack"

    # First token must DISMISS the ack and SHOW the reply bubble.
    assert kinds[2] is IntentKind.DISMISS_PET_BUBBLE
    assert captured[2].payload["bubble_id"] == "user-msg-ack"
    assert kinds[3] is IntentKind.SHOW_PET_BUBBLE
    reply_first = captured[3].payload["bubble"]
    assert reply_first["id"] == "user-msg-reply"
    assert reply_first["text"] == "He"

    # Every subsequent token (and the final settle) is an UPDATE_PET_BUBBLE.
    update_payloads = [
        c.payload for c in captured if c.kind is IntentKind.UPDATE_PET_BUBBLE
    ]
    update_texts = [p["text"] for p in update_payloads]
    # Texts must be monotonically lengthening (never shrink) and end with
    # the fully accumulated reply.
    assert all(p["bubble_id"] == "user-msg-reply" for p in update_payloads)
    assert update_texts == sorted(update_texts, key=len), update_texts
    assert update_texts[-1] == "Hello world"


@pytest.mark.asyncio
async def test_streaming_composer_yielding_nothing_keeps_placeholder() -> None:
    """A streaming composer that completes without yielding a single
    token must leave the placeholder bubble in place (its TTL retires
    it) and never emit a DISMISS / UPDATE for the reply bubble."""

    composer = _make_token_stream([])
    dispatcher, captured = _build_dispatcher(
        _NeverEngine(),
        streaming_composer=composer,
    )

    await dispatcher.on_user_message("hi")

    kinds = [c.kind for c in captured]
    assert kinds == [IntentKind.SET_PET_ANIMATION, IntentKind.SHOW_PET_BUBBLE]
    assert all(k is not IntentKind.DISMISS_PET_BUBBLE for k in kinds)
    assert all(k is not IntentKind.UPDATE_PET_BUBBLE for k in kinds)


@pytest.mark.asyncio
async def test_streaming_composer_failure_after_first_token_emits_partial_reply() -> None:
    """A composer that raises mid-stream must not crash the dispatcher;
    the partial reply already emitted stays visible and the final settle
    update lands."""

    async def compose(text: str):
        yield "Hel"
        raise RuntimeError("simulated network blip")

    dispatcher, captured = _build_dispatcher(
        _NeverEngine(),
        streaming_composer=compose,
        stream_flush_interval_ms=0,
    )

    await dispatcher.on_user_message("hi")

    update_texts = [
        c.payload["text"]
        for c in captured
        if c.kind is IntentKind.UPDATE_PET_BUBBLE
    ]
    # Must contain at least the final settle carrying the partial reply.
    assert update_texts, "expected at least one UPDATE_PET_BUBBLE"
    assert update_texts[-1] == "Hel"

    # Reply bubble was created with the first token.
    show_intents = [
        c for c in captured if c.kind is IntentKind.SHOW_PET_BUBBLE
    ]
    assert any(
        s.payload["bubble"]["id"] == "user-msg-reply"
        for s in show_intents
    )


@pytest.mark.asyncio
async def test_streaming_composer_takes_priority_over_non_streaming() -> None:
    """When both composers are wired, the streaming one wins. The
    non-streaming composer must never be called."""

    sync_calls = []

    async def sync_compose(text: str) -> str | None:
        sync_calls.append(text)
        return "should not appear"

    streaming = _make_token_stream(["streaming reply"])

    dispatcher, captured = _build_dispatcher(
        _NeverEngine(),
        composer=sync_compose,
        streaming_composer=streaming,
        stream_flush_interval_ms=0,
    )

    await dispatcher.on_user_message("hi")

    assert sync_calls == [], "non-streaming composer must not be invoked"
    show_payloads = [
        c.payload["bubble"]
        for c in captured
        if c.kind is IntentKind.SHOW_PET_BUBBLE
    ]
    reply = next(s for s in show_payloads if s["id"] == "user-msg-reply")
    assert reply["text"] == "streaming reply"
