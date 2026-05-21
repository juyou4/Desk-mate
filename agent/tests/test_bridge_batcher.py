"""Envelope batcher tests (V10 L3-D2)."""

from __future__ import annotations

import asyncio

import pytest

from deskmate_agent.bridge.batcher import EnvelopeBatcher
from deskmate_agent.protocol.envelope import BridgeEnvelope, EnvelopeType


class _SinkRecorder:
    def __init__(self) -> None:
        self.batches: list[list[BridgeEnvelope]] = []
        self.call_count = 0

    async def __call__(self, batch: list[BridgeEnvelope]) -> None:
        self.call_count += 1
        self.batches.append(list(batch))


@pytest.mark.asyncio
async def test_batch_coalesces_enqueues_within_window() -> None:
    sink = _SinkRecorder()
    batcher = EnvelopeBatcher(sink, window_s=0.05)

    for _ in range(5):
        await batcher.enqueue(BridgeEnvelope.of(EnvelopeType.PING))

    await asyncio.sleep(0.1)
    assert sink.call_count == 1
    assert len(sink.batches[0]) == 5
    assert batcher.pending_count == 0


@pytest.mark.asyncio
async def test_explicit_flush_drains_immediately() -> None:
    sink = _SinkRecorder()
    batcher = EnvelopeBatcher(sink, window_s=1.0)

    await batcher.enqueue(BridgeEnvelope.of(EnvelopeType.PING))
    assert batcher.pending_count == 1
    await batcher.flush()

    assert sink.call_count == 1
    assert batcher.pending_count == 0


@pytest.mark.asyncio
async def test_close_flushes_pending() -> None:
    sink = _SinkRecorder()
    batcher = EnvelopeBatcher(sink, window_s=10.0)

    await batcher.enqueue(BridgeEnvelope.of(EnvelopeType.PONG))
    await batcher.close()
    assert sink.call_count == 1


@pytest.mark.asyncio
async def test_consecutive_windows_start_fresh_timers() -> None:
    sink = _SinkRecorder()
    batcher = EnvelopeBatcher(sink, window_s=0.03)

    await batcher.enqueue(BridgeEnvelope.of(EnvelopeType.PING))
    await asyncio.sleep(0.08)
    assert sink.call_count == 1

    await batcher.enqueue(BridgeEnvelope.of(EnvelopeType.PONG))
    await asyncio.sleep(0.08)
    assert sink.call_count == 2


@pytest.mark.asyncio
async def test_flush_while_empty_is_noop() -> None:
    sink = _SinkRecorder()
    batcher = EnvelopeBatcher(sink, window_s=0.05)
    await batcher.flush()
    assert sink.call_count == 0


@pytest.mark.asyncio
async def test_window_must_be_positive() -> None:
    async def sink(_batch: list[BridgeEnvelope]) -> None:
        return None

    with pytest.raises(ValueError):
        EnvelopeBatcher(sink, window_s=0)
