"""Island module registration queue.

External tools should not connect to the Swift bridge directly: the
resident Python agent is the single bridge client. This queue lets an
agent register compact/live-activity rendering hints through a file
drop, then the resident agent forwards a typed ``register_module``
intent to Swift and replays the latest registrations on reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

from .logging_setup import get_logger
from .protocol.intents import IslandModuleSpec

_LOG = get_logger("deskmate_agent.module_registrations")


def default_module_registrations_dir() -> Path:
    override = os.environ.get("DESKMATE_MODULE_REGISTRATIONS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".deskmate" / "module-registrations"


def _now_ms() -> int:
    return int(time.time() * 1000)


def write_module_registration(
    spec: IslandModuleSpec, *, queue_dir: Path | None = None
) -> Path:
    """Atomically write ``spec`` into the module registration queue."""

    root = queue_dir or default_module_registrations_dir()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{_now_ms()}-{uuid.uuid4().hex}.json"
    path = root / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(spec.model_dump_json(exclude_none=True), encoding="utf-8")
    tmp.replace(path)
    return path


ModuleRegistrationHandler = Callable[[IslandModuleSpec], Awaitable[None]]


class ModuleRegistrationWatcher:
    """Poll a directory queue and consume island module registration files."""

    def __init__(
        self,
        queue_dir: Path,
        handler: ModuleRegistrationHandler,
        *,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._dir = queue_dir
        self._handler = handler
        self._poll = poll_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        self._stopping = False
        self._task = asyncio.create_task(
            self._run(), name="module-registration-watcher"
        )

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def drain_once(self) -> int:
        count = 0
        for path in self._registration_files():
            try:
                spec = IslandModuleSpec.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                await self._handler(spec)
                path.unlink(missing_ok=True)
                count += 1
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "module_registration.consume_failed",
                    path=str(path),
                    error=str(exc),
                )
                path.unlink(missing_ok=True)
        return count

    async def _run(self) -> None:
        while not self._stopping:
            await self.drain_once()
            await asyncio.sleep(self._poll)

    def _registration_files(self) -> list[Path]:
        try:
            return sorted(
                p
                for p in self._dir.glob("*.json")
                if not p.name.endswith(".tmp")
            )
        except OSError:
            return []


__all__ = [
    "ModuleRegistrationWatcher",
    "default_module_registrations_dir",
    "write_module_registration",
]
