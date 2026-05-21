"""Short-term memory: bounded in-memory deque of recent messages."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .types import Message

DEFAULT_MAX_MESSAGES: int = 20


class ShortTermMemory:
    """Rolling window of the last ``max_messages`` messages.

    All access is synchronous and thread-unsafe by design — the orchestrator
    owns this and drives it from a single asyncio task.
    """

    def __init__(self, max_messages: int = DEFAULT_MAX_MESSAGES) -> None:
        if max_messages <= 0:
            raise ValueError("max_messages must be > 0")
        self._buf: deque[Message] = deque(maxlen=max_messages)
        self._max = max_messages

    @property
    def max_messages(self) -> int:
        return self._max

    def append(self, msg: Message) -> None:
        self._buf.append(msg)

    def extend(self, messages: Iterable[Message]) -> None:
        for m in messages:
            self._buf.append(m)

    def recent(self, limit: int | None = None) -> list[Message]:
        if limit is None or limit >= len(self._buf):
            return list(self._buf)
        return list(self._buf)[-limit:]

    def clear(self) -> None:
        self._buf.clear()

    def __len__(self) -> int:
        return len(self._buf)


__all__ = ["DEFAULT_MAX_MESSAGES", "ShortTermMemory"]
