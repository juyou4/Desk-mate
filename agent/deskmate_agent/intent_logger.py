"""JSON-lines intent logger (V10 Phase 14-iv).

Wraps an ``IntentSink`` so every intent that crosses the bridge also
gets appended to a newline-delimited JSON file. The CLI's
``deskmate tail-status`` subcommand tails that file so contributors +
integration tests get a cheap way to watch the pet's behaviour
without parking a second bridge client on the socket.

Design notes:

- **Fail-soft.** Any I/O error during the log write is swallowed
  after a single WARN — the bridge keeps running. We never let the
  logger break the user-visible chain.
- **Bounded by byte count.** ``max_bytes`` triggers a rotation
  once the primary file crosses the threshold; the previous contents
  are renamed to ``<name>.1`` (overwriting the older backup).
  Two-file rotation keeps the disk usage predictable without
  requiring an external log rotator.
- **Non-blocking append.** File writes happen on the event loop's
  default executor so the sink call stays fast; ordering is preserved
  because we serialise the write path through a lock.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logging_setup import get_logger
from .protocol.intents import CompanionIntent

_LOG = get_logger("deskmate_agent.intent_logger")

IntentSink = Callable[[CompanionIntent], Awaitable[None]]


def _time_ms() -> int:
    import time as _time

    return int(_time.time() * 1000)


@dataclass
class IntentLogger:
    """Wrap an ``IntentSink`` and mirror each intent into ``path``."""

    path: Path
    inner: IntentSink
    max_bytes: int = 1_048_576  # 1 MiB primary file before rotate
    clock_ms: Callable[[], int] = field(default=_time_ms)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def __call__(self, intent: CompanionIntent) -> None:
        # Deliver to the real sink first so our logging never sits on
        # top of the critical path. If the inner sink raises, we
        # still want to record the attempt so the log gives a true
        # picture of what the dispatcher tried to do.
        inner_exc: Exception | None = None
        try:
            await self.inner(intent)
        except Exception as exc:  # noqa: BLE001 — propagate after log
            inner_exc = exc
        try:
            await self._write_line(intent, inner_error=inner_exc)
        except Exception as exc:  # noqa: BLE001 — log-layer fail-soft
            _LOG.warning(
                "intent_logger.write_failed",
                path=str(self.path),
                error=str(exc),
                error_type=type(exc).__name__,
            )
        if inner_exc is not None:
            raise inner_exc

    async def _write_line(
        self,
        intent: CompanionIntent,
        *,
        inner_error: Exception | None,
    ) -> None:
        payload: dict[str, Any] = {
            "ts_ms": self.clock_ms(),
            "kind": intent.kind.value,
            "payload": dict(intent.payload),
        }
        if inner_error is not None:
            payload["inner_error"] = {
                "type": type(inner_error).__name__,
                "message": str(inner_error),
            }
        line = json.dumps(payload, ensure_ascii=False) + "\n"

        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(
                None, self._append_and_maybe_rotate, line
            )

    def _append_and_maybe_rotate(self, line: str) -> None:
        """Blocking write; callers run this via ``run_in_executor``."""
        data = line.encode("utf-8")
        # Rotate BEFORE the append when we'd blow past the cap. This
        # keeps the rotated backup intact and makes the cap an
        # honest upper bound.
        try:
            cur_size = self.path.stat().st_size
        except FileNotFoundError:
            cur_size = 0
        if cur_size + len(data) > self.max_bytes and cur_size > 0:
            self._rotate_locked()
        with self.path.open("ab") as fh:
            fh.write(data)

    def _rotate_locked(self) -> None:
        backup = self.path.with_suffix(self.path.suffix + ".1")
        try:
            if backup.exists():
                backup.unlink()
            os.replace(self.path, backup)
        except OSError as exc:
            _LOG.warning(
                "intent_logger.rotate_failed",
                path=str(self.path),
                error=str(exc),
            )


__all__ = ["IntentLogger"]
