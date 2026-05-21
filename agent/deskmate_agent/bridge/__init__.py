"""UDS bridge layer (V10 L3-D).

Exposes the asyncio Unix-domain-socket server that Swift connects to, plus
the envelope codec and the 50ms batcher it uses under the hood.
"""

from __future__ import annotations

from .batcher import EnvelopeBatcher
from .codec import DecoderError, LineBuffer, decode_envelope, encode_envelope
from .paths import default_socket_path
from .server import BridgeServer, EnvelopeHandler

__all__ = [
    "BridgeServer",
    "DecoderError",
    "EnvelopeBatcher",
    "EnvelopeHandler",
    "LineBuffer",
    "decode_envelope",
    "default_socket_path",
    "encode_envelope",
]
