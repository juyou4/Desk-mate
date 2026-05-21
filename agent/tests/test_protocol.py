"""Phase 0 smoke tests for the V10 protocol layer.

Each test pins a V10 acceptance criterion, not an implementation detail.
"""

from __future__ import annotations

import json

import pytest

from deskmate_agent.protocol import (
    ActionSource,
    ActionTarget,
    BridgeEnvelope,
    BubbleKind,
    BubbleSpec,
    CharacterPackManifest,
    CompanionIntent,
    DomainState,
    EnvelopeType,
    IntentKind,
    InteractionAction,
    InteractionKind,
    IslandSurfaceKind,
    IslandSurfaceState,
    NestBehaviorPolicy,
    PetAnchor,
    PetAnchorKind,
    PetPresentationState,
    Priority,
    StateFrames,
    UserFocus,
    new_trace_id,
)

# ---------------------------------------------------------------------------
# Envelope (L1 / L3-D, trace_id propagation)
# ---------------------------------------------------------------------------


def test_envelope_roundtrip_preserves_trace_id() -> None:
    env = BridgeEnvelope.of(EnvelopeType.USER_MESSAGE, {"text": "hi"})
    restored = BridgeEnvelope.model_validate_json(env.model_dump_json())
    assert restored.trace_id == env.trace_id
    assert restored.type is EnvelopeType.USER_MESSAGE
    assert restored.payload == {"text": "hi"}


def test_envelope_forward_compatible_unknown_fields() -> None:
    raw = {
        "spec_version": 1,
        "type": "user.message",
        "trace_id": new_trace_id(),
        "payload": {"text": "hi", "future_payload_key": [1, 2, 3]},
        "future_top_level_field": {"hello": "world"},
    }
    env = BridgeEnvelope.model_validate(raw)

    assert env.payload["future_payload_key"] == [1, 2, 3]
    assert env.future_top_level_field == {"hello": "world"}

    wire = env.to_wire_dict()
    assert wire["payload"]["future_payload_key"] == [1, 2, 3]


def test_envelope_to_wire_dict_is_snake_case() -> None:
    env = BridgeEnvelope.of(EnvelopeType.PING)
    wire = env.to_wire_dict()
    assert set(wire.keys()) >= {"spec_version", "type", "trace_id", "payload"}
    assert wire["type"] == "ping"


def test_new_trace_id_is_unique_hex32() -> None:
    a, b = new_trace_id(), new_trace_id()
    assert a != b
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)


# ---------------------------------------------------------------------------
# InteractionAction (L1-F / I8, typed actions only)
# ---------------------------------------------------------------------------


def test_interaction_action_is_typed() -> None:
    act = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.PERMISSION_RESOLVE,
        payload={"allow": True},
    )
    data = json.loads(act.model_dump_json())
    assert data["kind"] == "permission.resolve"
    assert data["source"] == "island"


def test_interaction_action_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        InteractionAction.model_validate(
            {
                "source": "island",
                "target": "session",
                "kind": "totally.invented.kind",
                "payload": {},
            }
        )


def test_interaction_action_preserves_unknown_payload_keys() -> None:
    act = InteractionAction.model_validate(
        {
            "source": "pet",
            "target": "bubble",
            "kind": "pet.interact",
            "payload": {"gesture": "pat", "future_hint": 7},
        }
    )
    assert act.payload["future_hint"] == 7


def test_demo_trigger_action_is_typed() -> None:
    act = InteractionAction(
        source=ActionSource.MENU_BAR,
        target=ActionTarget.SYSTEM,
        kind=InteractionKind.DEMO_TRIGGER,
        payload={"scenario": "codex_session"},
    )
    data = json.loads(act.model_dump_json())
    assert data["kind"] == "demo.trigger"
    assert data["target"] == "system"


# ---------------------------------------------------------------------------
# CompanionIntent (L1-C)
# ---------------------------------------------------------------------------


def test_companion_intent_round_trip() -> None:
    intent = CompanionIntent(
        kind=IntentKind.PRESENT_ISLAND,
        payload={"surface": "notification_card", "session_id": "abc"},
    )
    restored = CompanionIntent.model_validate_json(intent.model_dump_json())
    assert restored.kind is IntentKind.PRESENT_ISLAND
    assert restored.payload["surface"] == "notification_card"


# ---------------------------------------------------------------------------
# DomainState / SurfaceState (L1-A / L1-B, I1 / I5)
# ---------------------------------------------------------------------------


def test_domain_state_defaults() -> None:
    ds = DomainState()
    assert ds.spec_version == 1
    assert ds.current_priority is Priority.P3
    assert ds.user_focus is UserFocus.CASUAL
    assert ds.pending_approvals == []


def test_island_surface_kind_matches_l1_e() -> None:
    # V10 L1-E mandates exactly five IslandSurface kinds.
    kinds = {k.value for k in IslandSurfaceKind}
    assert kinds == {
        "compact",
        "notification_card",
        "session_list",
        "live_activity",
        "empty",
    }


def test_island_surface_state_defaults_to_compact() -> None:
    s = IslandSurfaceState()
    assert s.kind is IslandSurfaceKind.COMPACT
    assert s.session_id is None


def test_pet_presentation_state_defaults_are_desktop_pixel() -> None:
    pps = PetPresentationState()
    assert pps.anchor_kind is PetAnchorKind.DESKTOP
    assert pps.velocity.dx == 0
    assert pps.velocity.dy == 0
    assert pps.avatar_style == "pixel"
    assert pps.is_interactive is True


def test_pet_anchor_and_nest_policy_are_forward_compatible() -> None:
    anchor = PetAnchor.model_validate(
        {"kind": "nest", "target_nest": "notch", "future": "kept"}
    )
    policy = NestBehaviorPolicy.model_validate(
        {"can_enter_nest": False, "should_leave_nest": True}
    )

    assert anchor.kind is PetAnchorKind.NEST
    assert anchor.target_nest == "notch"
    assert anchor.future == "kept"
    assert policy.can_enter_nest is False
    assert policy.should_leave_nest is True
    assert policy.target_nest is None


def test_bubble_spec_round_trip() -> None:
    b = BubbleSpec(id="b1", text="hi", kind=BubbleKind.APPROVAL_HINT)
    restored = BubbleSpec.model_validate_json(b.model_dump_json())
    assert restored.kind is BubbleKind.APPROVAL_HINT
    assert restored.ttl_ms == 8000  # default


# ---------------------------------------------------------------------------
# CharacterPackManifest (L1-D / I4)
# ---------------------------------------------------------------------------


def _manifest_with_states(states: dict[str, list[str]]) -> CharacterPackManifest:
    return CharacterPackManifest(
        id="pixie",
        display_name="Pixie",
        states={name: StateFrames(fps=4, frames=frames) for name, frames in states.items()},
    )


def test_manifest_detects_missing_required_states() -> None:
    manifest = _manifest_with_states(
        {
            "idle": ["idle/001.png"],
            "working": ["working/001.png"],
        }
    )
    assert manifest.missing_required_states() == ["thinking", "alert"]


def test_manifest_resolve_state_honors_fallbacks() -> None:
    manifest = _manifest_with_states(
        {
            "idle": ["idle/001.png"],
            "walking": ["walking/001.png"],
        }
    )
    # walking_left is in default fallbacks → walking, which exists.
    assert manifest.resolve_state("walking_left") == "walking"
    # Unknown name with no fallback returns None.
    assert manifest.resolve_state("nonexistent") is None


def test_manifest_resolve_state_detects_cycles() -> None:
    manifest = _manifest_with_states({"idle": ["idle/001.png"]})
    manifest.fallbacks = {"a": "b", "b": "a"}
    # Cycle must terminate returning None, not hang.
    assert manifest.resolve_state("a") is None


def test_manifest_forward_compatible_unknown_sections() -> None:
    raw = {
        "spec_version": 1,
        "id": "pixie",
        "display_name": "Pixie",
        "states": {"idle": {"fps": 4, "frames": ["idle/001.png"]}},
        "future_section": {"hello": "world"},
    }
    manifest = CharacterPackManifest.model_validate(raw)
    assert manifest.future_section == {"hello": "world"}


def test_manifest_state_frames_reject_empty_frames() -> None:
    with pytest.raises(ValueError):
        StateFrames(fps=4, frames=[])
