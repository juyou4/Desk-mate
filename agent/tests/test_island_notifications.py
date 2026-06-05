"""IslandNotificationPublisher tests (V10 L2-#1)."""

from __future__ import annotations

import pytest

from deskmate_agent.context import PerceptionSnapshot
from deskmate_agent.island_notifications import (
    EXTRA_FRONTMOST_BUNDLE_ID,
    EXTRA_FRONTMOST_WINDOW_SUBSTRING,
    IslandNotificationPublisher,
    NotificationOutcome,
)
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.protocol.state import IslandSurfaceKind, Priority
from deskmate_agent.sessions import SessionInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _session(
    sid: str = "sess-1",
    *,
    bundle_id: str | None = None,
    window_substring: str | None = None,
) -> SessionInfo:
    extras: dict[str, object] = {}
    if bundle_id is not None:
        extras[EXTRA_FRONTMOST_BUNDLE_ID] = bundle_id
    if window_substring is not None:
        extras[EXTRA_FRONTMOST_WINDOW_SUBSTRING] = window_substring
    return SessionInfo(session_id=sid, title=sid, extras=extras)


def _perception(
    *, bundle_id: str | None = None, window_title: str | None = None
) -> PerceptionSnapshot:
    return PerceptionSnapshot(
        app_bundle_id=bundle_id, window_title=window_title
    )


def _make_publisher(
    *,
    sink_log: list[CompanionIntent],
    active: SessionInfo | None = None,
    perception: PerceptionSnapshot | None = None,
    suppress: bool = True,
) -> IslandNotificationPublisher:
    async def sink(intent: CompanionIntent) -> None:
        sink_log.append(intent)

    return IslandNotificationPublisher(
        sink,
        active_session_provider=lambda: active,
        perception_provider=lambda: perception,
        suppress_frontmost_notifications=suppress,
    )


# ---------------------------------------------------------------------------
# Wire shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_show_notification_emits_well_formed_intent() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    outcome = await publisher.show_notification(
        activity_id="reminder-42",
        session_id="sess-1",
        priority=Priority.P1,
        detail="Standup in 5 minutes",
    )
    assert outcome == NotificationOutcome(emitted=True)
    assert len(sink) == 1
    intent = sink[0]
    assert intent.kind is IntentKind.PRESENT_ISLAND
    assert intent.payload["surface"] == IslandSurfaceKind.NOTIFICATION_CARD.value
    assert intent.payload["activity_id"] == "reminder-42"
    assert intent.payload["session_id"] == "sess-1"
    assert intent.payload["priority"] == Priority.P1.value
    assert intent.payload["detail"] == "Standup in 5 minutes"


@pytest.mark.asyncio
async def test_show_notification_omits_optional_fields_when_unset() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    await publisher.show_notification(activity_id="bare")
    payload = sink[0].payload
    # When no explicit surface_id is provided, the activity_id is
    # used as a backwards-compatible fallback (R3 polish — see
    # show_notification docstring).
    assert payload == {
        "surface": IslandSurfaceKind.NOTIFICATION_CARD.value,
        "activity_id": "bare",
        "priority": Priority.P2.value,
        "surface_id": "bare",
    }


@pytest.mark.asyncio
async def test_show_notification_rejects_empty_activity_id() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    with pytest.raises(ValueError):
        await publisher.show_notification(activity_id="")


# ---------------------------------------------------------------------------
# R3 surface_id injection (island-polish-enhancements task 5.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provided_valid_surface_id_is_included_verbatim() -> None:
    """R3.1/R3.2: An explicit ``surface_id`` (e.g. ``approval:42`` or
    ``question:sess-1:7``) must land in the payload exactly as
    given — the dismiss path matches against this byte-for-byte."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    outcome = await publisher.show_notification(
        activity_id="hook-sess-1-waiting_for_answer",
        session_id="sess-1",
        priority=Priority.P1,
        surface_id="question:sess-1:7",
    )
    assert outcome.emitted is True
    payload = sink[0].payload
    assert payload["surface_id"] == "question:sess-1:7"
    # activity_id is still emitted alongside; surface_id wins as the
    # close-loop dismiss target.
    assert payload["activity_id"] == "hook-sess-1-waiting_for_answer"


@pytest.mark.asyncio
async def test_none_surface_id_falls_back_to_activity_id() -> None:
    """Backward-compat: callers that haven't migrated yet (and
    pass values like ``hook-sess-1-running``) keep working. The
    activity_id is reused as the surface_id when it itself fits
    the R3.6 grammar."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    outcome = await publisher.show_notification(
        activity_id="hook-sess-1-running",
        session_id="sess-1",
    )
    assert outcome.emitted is True
    assert sink[0].payload["surface_id"] == "hook-sess-1-running"


@pytest.mark.asyncio
async def test_invalid_surface_id_too_long_is_rejected() -> None:
    """R3.6/R3.7: a 129-char surface_id violates the length cap and
    must drop the entire emission — not just strip the field."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    overlong = "a" * 129  # one past the 128-char ceiling
    outcome = await publisher.show_notification(
        activity_id="x",
        surface_id=overlong,
    )
    assert outcome == NotificationOutcome(
        emitted=False, suppressed_reason="invalid_surface_id"
    )
    assert sink == []


@pytest.mark.asyncio
async def test_invalid_surface_id_special_chars_is_rejected() -> None:
    """R3.6/R3.7: ``/``, spaces, or any character outside
    ``[A-Za-z0-9:_-]`` violate the grammar and the publisher must
    refuse to emit."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    outcome = await publisher.show_notification(
        activity_id="x",
        surface_id="approval:foo/bar baz",
    )
    assert outcome.emitted is False
    assert outcome.suppressed_reason == "invalid_surface_id"
    assert sink == []


@pytest.mark.asyncio
async def test_invalid_activity_id_fallback_is_dropped_silently() -> None:
    """When no explicit ``surface_id`` is provided and the
    ``activity_id`` itself doesn't fit the grammar (e.g. contains
    spaces or unicode), graceful degradation kicks in: the
    notification still emits — just without a ``surface_id`` key
    on the payload."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(sink_log=sink)
    outcome = await publisher.show_notification(
        activity_id="bare with spaces",
        session_id="sess-1",
    )
    assert outcome.emitted is True
    assert len(sink) == 1
    payload = sink[0].payload
    assert "surface_id" not in payload
    assert payload["activity_id"] == "bare with spaces"


# ---------------------------------------------------------------------------
# Suppression: bundle id only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppresses_when_bundle_id_matches_active_session() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is False
    assert outcome.suppressed_reason == "frontmost_matches_session"
    assert sink == []


@pytest.mark.asyncio
async def test_does_not_suppress_when_bundle_id_differs() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Safari"),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is True
    assert len(sink) == 1


# ---------------------------------------------------------------------------
# Suppression: window substring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppresses_when_window_substring_matches_case_insensitively() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session(
            "sess-1",
            bundle_id="com.apple.Terminal",
            window_substring="my-project",
        ),
        perception=_perception(
            bundle_id="com.apple.Terminal",
            window_title="bash · MY-PROJECT (main)",
        ),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is False


@pytest.mark.asyncio
async def test_does_not_suppress_when_window_substring_misses() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session(
            "sess-1",
            bundle_id="com.apple.Terminal",
            window_substring="my-project",
        ),
        perception=_perception(
            bundle_id="com.apple.Terminal",
            window_title="bash · other-repo",
        ),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is True


# ---------------------------------------------------------------------------
# Suppression: edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_without_window_claim_never_suppresses() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1"),  # no extras → no claim
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is True


@pytest.mark.asyncio
async def test_no_active_session_means_no_suppression() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=None,
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(activity_id="x")
    assert outcome.emitted is True


@pytest.mark.asyncio
async def test_no_perception_yet_means_no_suppression() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=None,  # before the first perception tick
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is True


@pytest.mark.asyncio
async def test_session_id_mismatch_never_suppresses() -> None:
    """A notification scoped to ``sess-2`` mustn't be suppressed by
    the user looking at ``sess-1``'s window — different conversation."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-2"
    )
    assert outcome.emitted is True


@pytest.mark.asyncio
async def test_unscoped_notification_can_still_be_suppressed_by_active_match() -> None:
    """A notification with no ``session_id`` (e.g. a generic system
    nudge) should still be suppressed when the user is actively
    inside the active session's window — they're already engaged."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(activity_id="x")
    assert outcome.emitted is False


# ---------------------------------------------------------------------------
# Suppression knob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_suppression_disabled_always_emits() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
        suppress=False,
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert outcome.emitted is True


@pytest.mark.asyncio
async def test_set_suppression_toggles_at_runtime() -> None:
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
        suppress=True,
    )
    # First call suppressed.
    out1 = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert out1.emitted is False

    publisher.set_suppression(False)
    out2 = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert out2.emitted is True

    publisher.set_suppression(True)
    out3 = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    assert out3.emitted is False


@pytest.mark.asyncio
async def test_force_bypasses_suppression() -> None:
    """A P0 approval that absolutely needs attention can pass
    ``force=True`` to skip the niceties."""
    sink: list[CompanionIntent] = []
    publisher = _make_publisher(
        sink_log=sink,
        active=_session("sess-1", bundle_id="com.apple.Terminal"),
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(
        activity_id="urgent",
        session_id="sess-1",
        priority=Priority.P0,
        force=True,
    )
    assert outcome.emitted is True
    assert sink[0].payload["priority"] == Priority.P0.value


# ---------------------------------------------------------------------------
# V10 L2-#3 restore-without-replay fuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restored_session_id_is_silenced_and_not_emitted() -> None:
    """A session id seeded via ``silenced_session_ids`` must skip the
    notification path even when nothing else (no frontmost match, no
    runtime suppression toggle) would otherwise suppress it."""
    sink: list[CompanionIntent] = []

    async def sink_fn(intent: CompanionIntent) -> None:
        sink.append(intent)

    publisher = IslandNotificationPublisher(
        sink_fn,
        active_session_provider=lambda: None,  # no active session
        perception_provider=lambda: None,
        suppress_frontmost_notifications=True,
        silenced_session_ids=["sess-restored-1", "sess-restored-2"],
    )

    assert publisher.is_silenced("sess-restored-1") is True
    assert publisher.is_silenced("sess-fresh") is False
    assert publisher.silenced_session_ids == frozenset(
        {"sess-restored-1", "sess-restored-2"}
    )

    outcome = await publisher.show_notification(
        activity_id="reminder", session_id="sess-restored-1"
    )
    assert outcome == NotificationOutcome(
        emitted=False, suppressed_reason="restored_session_silenced"
    )
    assert sink == []  # nothing made it onto the wire


@pytest.mark.asyncio
async def test_silenced_session_id_does_not_block_other_sessions() -> None:
    """Silencing one id must not bleed onto unrelated sessions."""
    sink: list[CompanionIntent] = []

    async def sink_fn(intent: CompanionIntent) -> None:
        sink.append(intent)

    publisher = IslandNotificationPublisher(
        sink_fn,
        active_session_provider=lambda: None,
        perception_provider=lambda: None,
        silenced_session_ids=["sess-restored"],
    )

    out_silenced = await publisher.show_notification(
        activity_id="x", session_id="sess-restored"
    )
    out_fresh = await publisher.show_notification(
        activity_id="x", session_id="sess-fresh"
    )
    out_anon = await publisher.show_notification(activity_id="x")

    assert out_silenced.emitted is False
    assert out_fresh.emitted is True
    assert out_anon.emitted is True  # no session_id → never silenced
    assert len(sink) == 2


@pytest.mark.asyncio
async def test_unsilence_restores_normal_emission() -> None:
    """Once an id is unsilenced (e.g. orchestrator wrote a fresh
    upsert) the next notification emits normally."""
    sink: list[CompanionIntent] = []

    async def sink_fn(intent: CompanionIntent) -> None:
        sink.append(intent)

    publisher = IslandNotificationPublisher(
        sink_fn,
        active_session_provider=lambda: None,
        perception_provider=lambda: None,
        silenced_session_ids=["sess-1"],
    )

    out1 = await publisher.show_notification(
        activity_id="a", session_id="sess-1"
    )
    assert out1.emitted is False

    assert publisher.unsilence("sess-1") is True
    assert publisher.unsilence("sess-1") is False  # idempotent

    out2 = await publisher.show_notification(
        activity_id="b", session_id="sess-1"
    )
    assert out2.emitted is True
    assert sink[0].payload["activity_id"] == "b"


@pytest.mark.asyncio
async def test_force_overrides_restore_silence() -> None:
    """Genuine P0 paths still need a way through. ``force=True``
    must bypass the silenced-session check just like it does for
    frontmost suppression."""
    sink: list[CompanionIntent] = []

    async def sink_fn(intent: CompanionIntent) -> None:
        sink.append(intent)

    publisher = IslandNotificationPublisher(
        sink_fn,
        active_session_provider=lambda: None,
        perception_provider=lambda: None,
        silenced_session_ids=["sess-1"],
    )

    outcome = await publisher.show_notification(
        activity_id="urgent",
        session_id="sess-1",
        priority=Priority.P0,
        force=True,
    )
    assert outcome.emitted is True
    # force does NOT auto-clear the silence — only an explicit
    # ``unsilence`` (via session_store activity) should do that.
    assert publisher.is_silenced("sess-1") is True


@pytest.mark.asyncio
async def test_silence_runtime_method_can_add_ids_after_construction() -> None:
    sink: list[CompanionIntent] = []

    async def sink_fn(intent: CompanionIntent) -> None:
        sink.append(intent)

    publisher = IslandNotificationPublisher(
        sink_fn,
        active_session_provider=lambda: None,
        perception_provider=lambda: None,
    )

    publisher.silence("sess-late")
    publisher.silence("")  # empty id ignored — never silenced
    assert publisher.is_silenced("sess-late") is True
    assert publisher.is_silenced("") is False

    out = await publisher.show_notification(
        activity_id="x", session_id="sess-late"
    )
    assert out.emitted is False


# ---------------------------------------------------------------------------
# Type robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_string_extras_dont_crash() -> None:
    """A typo in a session pack putting an int into
    ``frontmost_bundle_id`` shouldn't crash the publisher — the
    suppression rule simply treats it as 'no claim'."""
    sink: list[CompanionIntent] = []
    bad_session = SessionInfo(
        session_id="sess-1",
        extras={EXTRA_FRONTMOST_BUNDLE_ID: 42},  # type: ignore[dict-item]
    )
    publisher = _make_publisher(
        sink_log=sink,
        active=bad_session,
        perception=_perception(bundle_id="com.apple.Terminal"),
    )
    outcome = await publisher.show_notification(
        activity_id="x", session_id="sess-1"
    )
    # No suppression because the claim is invalid → emit.
    assert outcome.emitted is True


# ---------------------------------------------------------------------------
# Task 5.3: Property test for SurfaceId dismiss matching (Python side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_payload_uses_surface_id_format():
    """Property 3 (Python side): dismiss targets use the same format as present.

    This verifies the contract that ApprovalRouter emits dismiss with the same
    surface_id that was stored on the Approval.

    **Validates: Requirements R3.4, R3.5**
    """
    from deskmate_agent.approvals import Approval, ApprovalRouter, ApprovalStore
    from deskmate_agent.projector import DomainStateProjector
    from deskmate_agent.sessions import SessionStore

    store = ApprovalStore()
    store.add(
        Approval(
            approval_id="x1",
            prompt="?",
            created_at_ms=1,
            surface_id="approval:x1",
        )
    )
    emitted: list = []

    async def sink(i):
        emitted.append(i)

    ss = SessionStore()
    proj = DomainStateProjector(
        approval_store=store, session_store=ss, intent_sink=sink
    )
    router = ApprovalRouter(
        store, intent_sink=sink, session_store=ss, domain_projector=proj
    )
    from deskmate_agent.protocol.actions import (
        ActionSource,
        ActionTarget,
        InteractionAction,
        InteractionKind,
    )

    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SYSTEM,
        kind=InteractionKind.PERMISSION_RESOLVE,
        payload={"approval_id": "x1", "allow": True},
    )
    await router.handle(action)
    dismiss = [i for i in emitted if i.kind == IntentKind.DISMISS_ISLAND]
    assert len(dismiss) == 1
    assert dismiss[0].payload["id"] == "approval:x1"
