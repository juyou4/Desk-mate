"""Envelope codec: newline-delimited compact JSON (V10 L3-D4).

Encoding:
- UTF-8, ``ensure_ascii=False`` so Chinese + emoji traverse bytes cleanly.
- No whitespace separators (``(",", ":")``).
- Each envelope is terminated by exactly one ``\n``.

Decoding:
- :class:`LineBuffer` accumulates raw bytes across ``StreamReader.read`` calls
  and emits completed lines only. This is the only piece that needs to cope
  with partial reads.
"""

from __future__ import annotations

import json

from ..protocol.envelope import BridgeEnvelope

ENCODING = "utf-8"
_SEPARATORS = (",", ":")


def encode_envelope(envelope: BridgeEnvelope) -> bytes:
    """Serialise one envelope as a single NL-terminated UTF-8 line."""
    data = envelope.model_dump(mode="json")
    return (json.dumps(data, separators=_SEPARATORS, ensure_ascii=False) + "\n").encode(
        ENCODING
    )


class DecoderError(ValueError):
    """Raised when a single line does not decode to a valid envelope."""


def decode_envelope(line: bytes | str) -> BridgeEnvelope:
    raw = line.decode(ENCODING) if isinstance(line, (bytes, bytearray)) else line
    raw = raw.strip()
    if not raw:
        raise DecoderError("empty line")
    try:
        return BridgeEnvelope.model_validate_json(raw)
    except Exception as exc:  # pydantic / json errors become DecoderError
        raise DecoderError(f"invalid envelope: {exc}") from exc


class LineBuffer:
    """Accumulates socket bytes and yields fully received lines."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Append ``data`` and return any newly completed (non-empty) lines."""
        self._buf.extend(data)
        lines: list[bytes] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._buf[:idx])
            del self._buf[: idx + 1]
            if line.strip():
                lines.append(line)
        return lines

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


__all__ = ["DecoderError", "ENCODING", "LineBuffer", "decode_envelope", "encode_envelope"]
