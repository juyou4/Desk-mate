"""Envelope batcher with a bounded time window (V10 L3-D2).

Enqueueing envelopes starts (or extends) a 50ms timer. When the timer fires
the whole accumulated batch is handed to the sink in one call, so the
underlying socket sees *one* write instead of N.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

from ..protocol.envelope import BridgeEnvelope

SinkFn = Callable[[list[BridgeEnvelope]], Awaitable[None]]

DEFAULT_WINDOW_S: float = 0.05


class EnvelopeBatcher:
    """Coalesces enqueued envelopes within a tiny time window."""

    def __init__(self, sink: SinkFn, *, window_s: float = DEFAULT_WINDOW_S) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        self._sink = sink
        self._window_s = window_s
        self._pending: list[BridgeEnvelope] = []
        self._timer: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def enqueue(self, env: BridgeEnvelope) -> None:
        async with self._lock:
            self._pending.append(env)
            if self._timer is None or self._timer.done():
                self._timer = asyncio.create_task(self._flush_after_window())

    async def flush(self) -> None:
        """Drain the pending batch immediately."""
        async with self._lock:
            await self._cancel_timer_locked()
            await self._flush_now_locked()

    async def close(self) -> None:
        await self.flush()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _flush_after_window(self) -> None:
        try:
            await asyncio.sleep(self._window_s)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._flush_now_locked()

    async def _flush_now_locked(self) -> None:
        if not self._pending:
            return
        batch, self._pending = self._pending, []
        await self._sink(batch)

    async def _cancel_timer_locked(self) -> None:
        timer = self._timer
        if timer is not None and not timer.done():
            timer.cancel()
            # Swallow cancellation / sink errors: they are the timer's own
            # problem; flush() is about draining pending work.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await timer
        self._timer = None


__all__ = ["DEFAULT_WINDOW_S", "EnvelopeBatcher", "SinkFn"]
