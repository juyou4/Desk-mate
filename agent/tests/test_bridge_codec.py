"""Codec tests (V10 L3-D4)."""

from __future__ import annotations

import json

import pytest

from deskmate_agent.bridge.codec import (
    DecoderError,
    LineBuffer,
    decode_envelope,
    encode_envelope,
)
from deskmate_agent.protocol.envelope import BridgeEnvelope, EnvelopeType


def test_encode_produces_compact_newline_terminated_utf8() -> None:
    env = BridgeEnvelope.of(EnvelopeType.USER_MESSAGE, {"text": "你好"})
    line = encode_envelope(env)
    assert line.endswith(b"\n")
    # Compact JSON has no whitespace around separators.
    text = line.decode("utf-8").rstrip("\n")
    assert ", " not in text
    assert '": ' not in text
    # UTF-8 encoded non-ASCII characters survive round-trip.
    assert "你好" in text


def test_round_trip_preserves_envelope() -> None:
    original = BridgeEnvelope.of(
        EnvelopeType.INTERACTION,
        {"source": "island", "kind": "permission.resolve"},
        trace_id="deadbeef" * 4,
    )
    decoded = decode_envelope(encode_envelope(original))
    assert decoded.type == original.type
    assert decoded.trace_id == original.trace_id
    assert decoded.payload == original.payload


def test_decode_rejects_empty_line() -> None:
    with pytest.raises(DecoderError):
        decode_envelope("")


def test_decode_rejects_invalid_json() -> None:
    with pytest.raises(DecoderError):
        decode_envelope("{not json")


def test_decode_rejects_unknown_envelope_type() -> None:
    raw = json.dumps(
        {
            "spec_version": 1,
            "type": "totally.invented",
            "trace_id": "abc",
            "payload": {},
        }
    )
    with pytest.raises(DecoderError):
        decode_envelope(raw)


class TestLineBuffer:
    def test_complete_line_emitted(self) -> None:
        buf = LineBuffer()
        assert buf.feed(b"hello\n") == [b"hello"]

    def test_partial_line_is_held(self) -> None:
        buf = LineBuffer()
        assert buf.feed(b"hel") == []
        assert buf.pending_bytes == 3
        assert buf.feed(b"lo\n") == [b"hello"]
        assert buf.pending_bytes == 0

    def test_multiple_lines_in_one_feed(self) -> None:
        buf = LineBuffer()
        assert buf.feed(b"a\nb\nc") == [b"a", b"b"]
        assert buf.pending_bytes == 1
        assert buf.feed(b"\n") == [b"c"]

    def test_empty_lines_are_skipped(self) -> None:
        buf = LineBuffer()
        assert buf.feed(b"\n\nfoo\n\n") == [b"foo"]
