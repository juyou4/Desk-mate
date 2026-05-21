"""BridgeServer end-to-end tests (V10 L3-D2/D3/D4).

Each test spins up a real UDS server, connects a local client, exercises the
protocol, then tears everything down. Heartbeat and batch windows are
shortened so the suite runs in well under a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from pathlib import Path

import pytest

from deskmate_agent.bridge import (
    BridgeServer,
    LineBuffer,
    decode_envelope,
    encode_envelope,
)
from deskmate_agent.protocol.envelope import BridgeEnvelope, EnvelopeType


@pytest.fixture
def short_socket_path() -> Path:
    """Return a socket path short enough to fit ``sockaddr_un.sun_path`` (104
    bytes on macOS). ``tmp_path`` paths can blow that limit, especially when
    the repo root contains multi-byte characters."""
    path = Path(tempfile.gettempdir()) / f"dm-{uuid.uuid4().hex[:8]}.sock"
    yield path
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()


class _Harness:
    """Drives a server + one client for the duration of a test."""

    def __init__(
        self,
        socket_path: Path,
        *,
        heartbeat_s: float = 10.0,
        batch_window_s: float = 0.01,
    ) -> None:
        self.socket_path = socket_path
        self.received: list[BridgeEnvelope] = []
        self.connect_count = 0
        self.disconnect_count = 0
        self.server = BridgeServer(
            socket_path,
            on_envelope=self._on_env,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
            heartbeat_interval_s=heartbeat_s,
            batch_window_s=batch_window_s,
        )
        self._serve_task: asyncio.Task[None] | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._client_buf = LineBuffer()

    async def __aenter__(self) -> _Harness:
        await self.server.start()
        self._serve_task = asyncio.create_task(self.server.serve_forever())
        self.reader, self.writer = await asyncio.open_unix_connection(
            str(self.socket_path)
        )
        # Give the server a moment to finish wiring on_connect.
        await asyncio.sleep(0.02)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        if self._serve_task is not None:
            self._serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._serve_task
        await self.server.stop()

    async def _on_env(self, env: BridgeEnvelope) -> None:
        self.received.append(env)

    async def _on_connect(self) -> None:
        self.connect_count += 1

    async def _on_disconnect(self) -> None:
        self.disconnect_count += 1

    async def send_from_client(self, env: BridgeEnvelope) -> None:
        assert self.writer is not None
        self.writer.write(encode_envelope(env))
        await self.writer.drain()

    async def collect_from_server(self, duration_s: float) -> list[BridgeEnvelope]:
        """Read whatever the server sends during ``duration_s`` seconds."""
        assert self.reader is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + duration_s
        envelopes: list[BridgeEnvelope] = []
        while loop.time() < deadline:
            remaining = max(deadline - loop.time(), 0.01)
            try:
                data = await asyncio.wait_for(self.reader.read(4096), timeout=remaining)
            except TimeoutError:
                continue
            if not data:
                break
            for line in self._client_buf.feed(data):
                envelopes.append(decode_envelope(line))
        return envelopes


@pytest.mark.asyncio
async def test_client_to_server_envelope_is_delivered(short_socket_path: Path) -> None:
    async with _Harness(short_socket_path) as h:
        await h.send_from_client(
            BridgeEnvelope.of(EnvelopeType.USER_MESSAGE, {"text": "hi"})
        )
        await asyncio.sleep(0.05)
        assert len(h.received) == 1
        assert h.received[0].type is EnvelopeType.USER_MESSAGE
        assert h.received[0].payload == {"text": "hi"}
        assert h.connect_count == 1


@pytest.mark.asyncio
async def test_server_to_client_uses_batched_window(short_socket_path: Path) -> None:
    async with _Harness(short_socket_path, batch_window_s=0.02) as h:
        for i in range(3):
            await h.server.send(
                BridgeEnvelope.of(EnvelopeType.INTENT, {"seq": i})
            )
        envs = await h.collect_from_server(0.15)
        intents = [e for e in envs if e.type is EnvelopeType.INTENT]
        assert [e.payload["seq"] for e in intents] == [0, 1, 2]


@pytest.mark.asyncio
async def test_ping_from_client_receives_pong(short_socket_path: Path) -> None:
    async with _Harness(short_socket_path) as h:
        await h.send_from_client(BridgeEnvelope.of(EnvelopeType.PING))
        envs = await h.collect_from_server(0.1)
        assert any(e.type is EnvelopeType.PONG for e in envs)
        # Pings are NOT delivered to the business handler.
        assert all(e.type is not EnvelopeType.PING for e in h.received)


@pytest.mark.asyncio
async def test_ping_pong_bypasses_batch_window(short_socket_path: Path) -> None:
    """V10 §3.1 row 9 — heartbeat traffic must not be deferred by
    the batch window. With a 200 ms batch window we'd see a
    full-window stall on every ping if PONG were enqueued like
    business traffic; the fast-path flush keeps the round trip
    well under the window."""
    async with _Harness(short_socket_path, batch_window_s=0.2) as h:
        loop = asyncio.get_running_loop()
        sent_at = loop.time()
        await h.send_from_client(BridgeEnvelope.of(EnvelopeType.PING))
        # Read just long enough that the unrelated batch window
        # would *not* yet have fired (≈100 ms), proving the PONG
        # came out of band.
        envs = await h.collect_from_server(0.1)
        elapsed_s = loop.time() - sent_at
        pongs = [e for e in envs if e.type is EnvelopeType.PONG]
        assert len(pongs) == 1, "expected exactly one PONG"
        assert elapsed_s < 0.2, (
            f"PONG arrived in {elapsed_s*1000:.1f}ms; should be "
            f"well under the 200ms batch window"
        )


@pytest.mark.asyncio
async def test_ping_pong_does_not_starve_batched_traffic(
    short_socket_path: Path,
) -> None:
    """A ping flush must not drop or break the next batch of
    business intents — they still coalesce normally behind the
    bypassed PONG."""
    async with _Harness(short_socket_path, batch_window_s=0.05) as h:
        # Force a ping/pong fast-path first.
        await h.send_from_client(BridgeEnvelope.of(EnvelopeType.PING))
        # Then fire two business envelopes that should still batch.
        for i in range(2):
            await h.server.send(
                BridgeEnvelope.of(EnvelopeType.INTENT, {"seq": i})
            )
        envs = await h.collect_from_server(0.2)
        seqs = [e.payload["seq"] for e in envs if e.type is EnvelopeType.INTENT]
        assert seqs == [0, 1], f"intents lost or reordered: {seqs}"


@pytest.mark.asyncio
async def test_heartbeat_pings_after_silence(short_socket_path: Path) -> None:
    async with _Harness(
        short_socket_path, heartbeat_s=0.05, batch_window_s=0.01
    ) as h:
        envs = await h.collect_from_server(0.25)
        pings = [e for e in envs if e.type is EnvelopeType.PING]
        assert len(pings) >= 2


@pytest.mark.asyncio
async def test_disconnect_hook_fires(short_socket_path: Path) -> None:
    h = _Harness(short_socket_path)
    async with h:
        await asyncio.sleep(0.02)
        assert h.writer is not None
        h.writer.close()
        with contextlib.suppress(Exception):
            await h.writer.wait_closed()
        h.writer = None
        # Give the server's finally-block a tick to run.
        await asyncio.sleep(0.05)
        assert h.disconnect_count == 1


@pytest.mark.asyncio
async def test_malformed_line_does_not_kill_connection(short_socket_path: Path) -> None:
    async with _Harness(short_socket_path) as h:
        assert h.writer is not None
        h.writer.write(b"{not json}\n")
        await h.writer.drain()

        # Subsequent valid envelope should still arrive.
        await h.send_from_client(
            BridgeEnvelope.of(EnvelopeType.USER_MESSAGE, {"text": "ok"})
        )
        await asyncio.sleep(0.05)
        assert any(e.type is EnvelopeType.USER_MESSAGE for e in h.received)
