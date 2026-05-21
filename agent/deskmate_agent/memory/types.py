"""Shared types for the memory layer."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    """OpenAI-compatible chat message with tool-call support.

    ``tool_calls`` is kept as a list of dicts (rather than a typed model) so
    we can round-trip whatever the upstream SDK emits without reshaping it.
    """

    model_config = ConfigDict(extra="allow")

    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    ts_ms: int = Field(default_factory=lambda: int(time.time() * 1000))


__all__ = ["Message", "Role"]
