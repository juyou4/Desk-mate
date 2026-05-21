"""Cross-language wire-regression tests (V10 Phase 11b).

Pins the exact JSON shape produced by the Swift ``EnvelopeSender``
helpers — ``send(action:)``, ``sendUserMessage``, ``sendUserClickPet``,
``sendPerception`` — and verifies Python decodes each one without
losing information.

Keep these byte strings in lock-step with
``DeskmateCore/EnvelopeSender.swift`` and the corresponding Swift
smoke tests. If a Swift change alters wire format, the Swift smoke
suite flags it *and* these tests flag it — together they form the
cross-language contract bouquet.

Key-order note: Swift ``JSONEncoder`` is unordered, so these fixtures
only need to be *one* valid byte-for-byte output. Decoding is
key-order-agnostic, which is what we test.
"""

from __future__ import annotations

from deskmate_agent.bridge.codec import decode_envelope
from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)
from deskmate_agent.protocol.envelope import EnvelopeType

# ---------------------------------------------------------------------------
# .interaction
# ---------------------------------------------------------------------------


def test_swift_send_action_decodes_into_interaction_action() -> None:
    """``client.send(action:)`` wraps an InteractionAction in the
    envelope payload *directly*; Python's InteractionAction.model_validate
    must round-trip it cleanly."""
    wire = (
        b'{"spec_version":1,"type":"interaction","trace_id":"wire-action",'
        b'"payload":{'
        b'"source":"pet","target":"bubble","kind":"permission.resolve",'
        b'"payload":{"approval_id":"ap-1","allow":true}'
        b'}}'
    )
    env = decode_envelope(wire)
    assert env.type is EnvelopeType.INTERACTION
    assert env.trace_id == "wire-action"

    action = InteractionAction.model_validate(env.payload)
    assert action.source is ActionSource.PET
    assert action.target is ActionTarget.BUBBLE
    assert action.kind is InteractionKind.PERMISSION_RESOLVE
    assert action.payload == {"approval_id": "ap-1", "allow": True}


# ---------------------------------------------------------------------------
# .user.message
# ---------------------------------------------------------------------------


def test_swift_send_user_message_decodes_with_text() -> None:
    wire = (
        b'{"spec_version":1,"type":"user.message","trace_id":"wire-msg",'
        b'"payload":{"text":"hello"}}'
    )
    env = decode_envelope(wire)
    assert env.type is EnvelopeType.USER_MESSAGE
    assert env.trace_id == "wire-msg"
    assert env.payload == {"text": "hello"}


# ---------------------------------------------------------------------------
# .user.click_pet
# ---------------------------------------------------------------------------


def test_swift_send_user_click_pet_decodes_with_empty_payload() -> None:
    wire = (
        b'{"spec_version":1,"type":"user.click_pet","trace_id":"wire-click",'
        b'"payload":{}}'
    )
    env = decode_envelope(wire)
    assert env.type is EnvelopeType.USER_CLICK_PET
    assert env.trace_id == "wire-click"
    assert env.payload == {}


# ---------------------------------------------------------------------------
# .perception
# ---------------------------------------------------------------------------


def test_swift_send_perception_keys_feed_context_reader() -> None:
    """V10 L3-D1: Swift's snake_case keys must feed Python's
    ``_context_from_perception`` without translation."""
    wire = (
        b'{"spec_version":1,"type":"perception","trace_id":"wire-perc",'
        b'"payload":{'
        b'"user_state":"active","focus":"focused",'
        b'"app":"com.apple.Terminal","title":"bash","idle_ms":1500'
        b'}}'
    )
    env = decode_envelope(wire)
    assert env.type is EnvelopeType.PERCEPTION
    assert env.trace_id == "wire-perc"

    # Exercise the actual reader the agent uses at runtime.
    from deskmate_agent.app import _context_from_perception

    ctx = _context_from_perception(env.payload)
    assert ctx.perception.user_state == "active"
    assert ctx.perception.focus.value == "focused"
    assert ctx.perception.app_bundle_id == "com.apple.Terminal"
    assert ctx.perception.window_title == "bash"
    assert ctx.perception.idle_ms == 1_500


# ---------------------------------------------------------------------------
# Byte-level sanity: the Python encoder agrees with the Swift wire
# ---------------------------------------------------------------------------


def test_python_encode_of_same_envelope_decodes_identically() -> None:
    """If Python encodes an equivalent envelope, the payload dicts must
    match the Swift wire fixture semantically. Guards against either
    side drifting key names / enum raw values silently."""
    from deskmate_agent.bridge.codec import encode_envelope
    from deskmate_agent.protocol.envelope import BridgeEnvelope

    swift_wire = (
        b'{"spec_version":1,"type":"interaction","trace_id":"x",'
        b'"payload":{"source":"pet","target":"bubble",'
        b'"kind":"permission.resolve",'
        b'"payload":{"approval_id":"ap-1","allow":true}}}'
    )
    swift_env = decode_envelope(swift_wire)

    python_env = BridgeEnvelope.of(
        EnvelopeType.INTERACTION,
        payload=InteractionAction(
            source=ActionSource.PET,
            target=ActionTarget.BUBBLE,
            kind=InteractionKind.PERMISSION_RESOLVE,
            payload={"approval_id": "ap-1", "allow": True},
        ).model_dump(mode="json"),
        trace_id="x",
    )
    python_decoded = decode_envelope(encode_envelope(python_env))

    assert swift_env.type is python_decoded.type
    assert swift_env.trace_id == python_decoded.trace_id
    assert swift_env.payload == python_decoded.payload
