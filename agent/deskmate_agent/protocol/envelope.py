"""Bridge envelope (V10 L1 / L3-D).

Single shape for every IPC message exchanged over the UDS bridge.
Forward-compatible by design: unknown fields inside `payload` are preserved,
and an unsupported `type` yields a structured warning rather than a crash.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SPEC_VERSION = 1


class EnvelopeType(StrEnum):
    """Top-level envelope verbs. Must stay aligned with `shared/protocol.md`."""

    # Swift → Python
    PERCEPTION = "perception"
    USER_MESSAGE = "user.message"
    USER_CLICK_PET = "user.click_pet"
    INTERACTION = "interaction"

    # Python → Swift
    INTENT = "intent"

    # Bidirectional / lifecycle
    PING = "ping"
    PONG = "pong"
    STATE_SNAPSHOT_REQUEST = "state.snapshot.request"
    STATE_SNAPSHOT = "state.snapshot"
    AGENT_READY = "agent.ready"
    AGENT_PAUSE = "agent.pause"

    # V10 §3.1 row 6 + row 8: Swift-side hard budget metrics —
    # wake-to-first-frame latency + frame drop ratio. The Swift shell
    # pushes one of these at a configurable cadence so the agent can
    # log / persist the running budgets.
    PERF_METRICS = "perf.metrics"


def new_trace_id() -> str:
    """Generate a fresh 32-char lowercase hex trace_id (UUID4)."""
    return uuid.uuid4().hex


class BridgeEnvelope(BaseModel):
    """Single envelope shape for all IPC messages.

    - `payload` is an open dict so unknown keys survive round-trip.
    - `spec_version` gates future breaking changes.
    - `trace_id` is always present; callers may override for correlation.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    spec_version: int = SPEC_VERSION
    type: EnvelopeType
    trace_id: str = Field(default_factory=new_trace_id)
    ts_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(
        cls,
        type_: EnvelopeType,
        payload: dict[str, Any] | None = None,
        *,
        trace_id: str | None = None,
        ts_ms: int | None = None,
    ) -> BridgeEnvelope:
        """Convenience constructor so callers don't repeat defaults."""
        return cls(
            type=type_,
            trace_id=trace_id or new_trace_id(),
            ts_ms=ts_ms,
            payload=payload or {},
        )

    def to_wire_dict(self) -> dict[str, Any]:
        """Serialize to the exact wire dict (JSON-safe, snake_case)."""
        return self.model_dump(mode="json")


__all__ = ["SPEC_VERSION", "BridgeEnvelope", "EnvelopeType", "new_trace_id"]
