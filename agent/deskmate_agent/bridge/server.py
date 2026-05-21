"""UDS bridge server (V10 L3-D2 / L3-D3 / L3-D4).

Single-client asyncio server that speaks newline-delimited compact JSON.
Business traffic suppresses the heartbeat; only real silence triggers a ping.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..logging_setup import get_logger
from ..protocol.envelope import BridgeEnvelope, EnvelopeType
from .batcher import DEFAULT_WINDOW_S, EnvelopeBatcher
from .codec import DecoderError, LineBuffer, decode_envelope, encode_envelope

_LOG = get_logger("deskmate_agent.bridge.server")

EnvelopeHandler = Callable[[BridgeEnvelope], Awaitable[None]]
ConnectionHook = Callable[[], Awaitable[None]]

DEFAULT_HEARTBEAT_S: int = 30


class BridgeServer:
    """One-client UDS server that emits + receives :class:`BridgeEnvelope`.

    - Any inbound / outbound envelope resets the heartbeat countdown; pings
      are *only* sent after a full silent interval (V10 L3-D3).
    - Outbound writes are coalesced by :class:`EnvelopeBatcher` into a
      single write per window (V10 L3-D2).
    - Malformed inbound lines are logged and dropped — the server does not
      crash on bad JSON (forward-compat contract).
    """

    def __init__(
        self,
        socket_path: str | Path,
        *,
        on_envelope: EnvelopeHandler,
        on_connect: ConnectionHook | None = None,
        on_disconnect: ConnectionHook | None = None,
        heartbeat_interval_s: float = DEFAULT_HEARTBEAT_S,
        batch_window_s: float = DEFAULT_WINDOW_S,
    ) -> None:
        self.socket_path = Path(socket_path)
        self._on_env = on_envelope
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._heartbeat_interval_s = heartbeat_interval_s
        self._batch_window_s = batch_window_s

        self._server: asyncio.AbstractServer | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._batcher: EnvelopeBatcher | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_outbound_mono: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self.socket_path)
        )
        with contextlib.suppress(OSError):
            os.chmod(self.socket_path, 0o600)

    async def serve_forever(self) -> None:
        if self._server is None:
            raise RuntimeError("BridgeServer.start() not called")
        async with self._server:
            await self._server.serve_forever()

    async def stop(self, *, teardown_timeout_s: float = 1.0) -> None:
        """Tear down the bridge. Every step is bounded by ``teardown_timeout_s``
        and falls through to :meth:`Transport.abort` / best-effort close so a
        misbehaving client or pending write never hangs shutdown (this is what
        used to trip Phase 1d e2e tests)."""
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._heartbeat_task, timeout=teardown_timeout_s)
            self._heartbeat_task = None

        if self._batcher is not None:
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    self._batcher.close(), timeout=teardown_timeout_s
                )
            self._batcher = None

        if self._writer is not None:
            writer = self._writer
            self._writer = None
            try:
                writer.close()
                await asyncio.wait_for(
                    writer.wait_closed(), timeout=teardown_timeout_s
                )
            except (TimeoutError, Exception):
                # Force the transport down if a peer refuses to close.
                transport = getattr(writer, "transport", None)
                if transport is not None and hasattr(transport, "abort"):
                    with contextlib.suppress(Exception):
                        transport.abort()

        if self._server is not None:
            server = self._server
            self._server = None
            server.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(
                    server.wait_closed(), timeout=teardown_timeout_s
                )

        try:
            if self.socket_path.exists():
                self.socket_path.unlink()
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send(self, envelope: BridgeEnvelope) -> None:
        if self._batcher is None:
            raise RuntimeError("no client connected")
        await self._batcher.enqueue(envelope)
        self._touch_outbound()

    async def flush(self) -> None:
        if self._batcher is not None:
            await self._batcher.flush()

    # ------------------------------------------------------------------
    # Client handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if self._writer is not None:
            # Drop any stale previous connection cleanly so reconnects aren't
            # blocked — Swift will retry with exponential backoff.
            with contextlib.suppress(Exception):
                self._writer.close()
        self._writer = writer
        self._batcher = EnvelopeBatcher(self._write_batch, window_s=self._batch_window_s)
        self._touch_outbound()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        if self._on_connect is not None:
            try:
                await self._on_connect()
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("bridge.on_connect_failed", error=str(exc))

        buffer = LineBuffer()
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for raw_line in buffer.feed(data):
                    try:
                        env = decode_envelope(raw_line)
                    except DecoderError as exc:
                        _LOG.warning("bridge.decode_failed", error=str(exc))
                        continue
                    if env.type in (EnvelopeType.PING, EnvelopeType.PONG):
                        # Answer pings automatically. Business handlers don't
                        # need to see heartbeat traffic.
                        if env.type is EnvelopeType.PING:
                            try:
                                await self.send(BridgeEnvelope.of(EnvelopeType.PONG))
                                # V10 §3.1 row 9: IPC p99 round trip
                                # must stay under 10 ms — that's
                                # tighter than the 50 ms batch
                                # window. Heartbeat traffic is
                                # lifecycle, not a batch candidate,
                                # so flush the PONG immediately.
                                await self.flush()
                            except Exception as exc:  # noqa: BLE001
                                _LOG.warning("bridge.pong_send_failed", error=str(exc))
                        continue
                    try:
                        await self._on_env(env)
                    except Exception as exc:  # noqa: BLE001
                        _LOG.warning(
                            "bridge.handler_failed", type=env.type.value, error=str(exc)
                        )
        finally:
            if self._heartbeat_task is not None and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            if self._batcher is not None:
                await self._batcher.close()
                self._batcher = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            if self._writer is writer:
                self._writer = None
            if self._on_disconnect is not None:
                try:
                    await self._on_disconnect()
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("bridge.on_disconnect_failed", error=str(exc))

    async def _write_batch(self, batch: list[BridgeEnvelope]) -> None:
        if self._writer is None or self._writer.is_closing():
            return
        payload = b"".join(encode_envelope(e) for e in batch)
        try:
            self._writer.write(payload)
            await self._writer.drain()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("bridge.write_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_s)
                if self._writer is None or self._writer.is_closing():
                    return
                elapsed = loop.time() - self._last_outbound_mono
                if elapsed + 1e-3 < self._heartbeat_interval_s:
                    # Business traffic covered us this interval.
                    continue
                try:
                    await self.send(BridgeEnvelope.of(EnvelopeType.PING))
                except Exception as exc:  # noqa: BLE001
                    _LOG.warning("bridge.heartbeat_send_failed", error=str(exc))
        except asyncio.CancelledError:
            return

    def _touch_outbound(self) -> None:
        try:
            self._last_outbound_mono = asyncio.get_running_loop().time()
        except RuntimeError:
            self._last_outbound_mono = 0.0


__all__ = ["BridgeServer", "ConnectionHook", "DEFAULT_HEARTBEAT_S", "EnvelopeHandler"]
