"""Hook event v1 ingestion and file-queue watcher.

The first external-agent bridge intentionally uses a directory queue
instead of the Swift IPC socket. Hooks can write JSON into
``~/.deskmate/hook-events`` from any process, while the resident Python
agent remains the only client attached to the Swift bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .agent_events import AgentEventReducer, event_from_hook
from .approvals import ApprovalStore
from .hook_normalizers import normalize_source_hook
from .logging_setup import get_logger
from .sessions import SessionPhase, SessionStore

_LOG = get_logger("deskmate_agent.hooks")


def default_hook_events_dir() -> Path:
    override = os.environ.get("DESKMATE_HOOK_EVENTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".deskmate" / "hook-events"


def _now_ms() -> int:
    return int(time.time() * 1000)


class HookEvent(BaseModel):
    """Normalized hook payload.

    Unknown source fields are retained in ``raw`` and extra top-level
    fields are allowed so a future hook version can arrive without
    breaking the MVP watcher.
    """

    model_config = ConfigDict(extra="allow")

    source: str
    event: str
    session_id: str
    title: str = ""
    summary: str = ""
    cwd: str | None = None
    jump_url: str | None = None
    phase: SessionPhase = SessionPhase.RUNNING
    approval_id: str | None = None
    prompt: str | None = None
    ts_ms: int = Field(default_factory=_now_ms)
    raw: dict[str, Any] = Field(default_factory=dict)


def normalize_hook_event(raw: dict[str, Any], *, source: str) -> HookEvent:
    """Best-effort normalizer for external agent hook payloads."""

    fields = normalize_source_hook(raw, source=source)
    return HookEvent(
        source=fields.source,
        event=fields.event,
        session_id=fields.session_id,
        title=fields.title,
        summary=fields.summary,
        cwd=fields.cwd,
        jump_url=fields.jump_url,
        phase=fields.phase,
        approval_id=fields.approval_id,
        prompt=fields.prompt,
        ts_ms=fields.ts_ms,
        raw=fields.raw,
    )


def write_hook_event(event: HookEvent, *, queue_dir: Path | None = None) -> Path:
    """Atomically write ``event`` into the hook queue and return its path."""

    root = queue_dir or default_hook_events_dir()
    root.mkdir(parents=True, exist_ok=True)
    name = f"{event.ts_ms}-{uuid.uuid4().hex}.json"
    path = root / name
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(event.model_dump_json(), encoding="utf-8")
    tmp.replace(path)
    return path


class HookEventConsumer:
    """Map normalized hook events through the shared AgentEvent reducer."""

    def __init__(
        self,
        *,
        session_store: SessionStore,
        approval_store: ApprovalStore,
    ) -> None:
        self._reducer = AgentEventReducer(
            session_store=session_store,
            approval_store=approval_store,
        )

    def handle(self, event: HookEvent) -> None:
        self._reducer.apply(event_from_hook(event))


HookEventHandler = Callable[[HookEvent], Awaitable[None]]


class HookEventWatcher:
    """Poll a directory queue and consume hook event files."""

    def __init__(
        self,
        queue_dir: Path,
        handler: HookEventHandler,
        *,
        poll_interval_s: float = 0.3,
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
        self._task = asyncio.create_task(self._run(), name="hook-event-watcher")

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
        for path in self._event_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                event = HookEvent.model_validate(data)
                await self._handler(event)
                path.unlink(missing_ok=True)
                count += 1
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("hooks.consume_failed", path=str(path), error=str(exc))
                path.unlink(missing_ok=True)
        return count

    async def _run(self) -> None:
        while not self._stopping:
            await self.drain_once()
            await asyncio.sleep(self._poll)

    def _event_files(self) -> list[Path]:
        try:
            return sorted(
                p
                for p in self._dir.glob("*.json")
                if not p.name.endswith(".tmp")
            )
        except OSError:
            return []


__all__ = [
    "HookEvent",
    "HookEventConsumer",
    "HookEventWatcher",
    "default_hook_events_dir",
    "normalize_hook_event",
    "write_hook_event",
]
