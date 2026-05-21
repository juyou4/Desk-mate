"""Claude Code transcript watcher.

V1 scans Claude's local JSONL transcript directory and feeds appended rows
through ``claude_jsonl`` into the shared AgentEvent reducer. It is intentionally
read-only and best-effort: missing directories, partial lines, and malformed
rows are ignored without affecting hooks or process scanning.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from .agent_events import AgentEvent
from .claude_jsonl import ClaudeJsonlCursor, read_incremental_claude_jsonl
from .logging_setup import get_logger

_LOG = get_logger("deskmate_agent.claude_transcripts")


ClaudeEventHandler = Callable[[AgentEvent], Awaitable[None]]


def default_claude_transcript_roots() -> tuple[Path, ...]:
    return (Path.home() / ".claude" / "projects",)


class ClaudeTranscriptWatcher:
    def __init__(
        self,
        handler: ClaudeEventHandler,
        *,
        roots: tuple[Path, ...] | None = None,
        poll_interval_s: float = 2.0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._handler = handler
        self._roots = roots or default_claude_transcript_roots()
        self._poll = poll_interval_s
        self._clock = clock
        self._cursors: dict[Path, ClaudeJsonlCursor] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="claude-transcript-watcher")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def scan_once(self) -> int:
        count = 0
        for path in self._jsonl_paths():
            cursor = self._cursor_for(path)
            try:
                events = read_incremental_claude_jsonl(
                    cursor,
                    session_id=_session_id_for(path),
                    title=_title_for(path),
                    cwd=_cwd_for(path),
                    clock=self._clock,
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("claude_transcripts.read_failed", path=str(path), error=str(exc))
                continue
            for event in events:
                await self._handler(event)
                count += 1
        return count

    async def _run(self) -> None:
        while not self._stopping:
            await self.scan_once()
            await asyncio.sleep(self._poll)

    def _jsonl_paths(self) -> list[Path]:
        paths: list[Path] = []
        for root in self._roots:
            try:
                paths.extend(p for p in root.rglob("*.jsonl") if p.is_file())
            except OSError:
                continue
        paths.sort(key=lambda p: str(p))
        return paths

    def _cursor_for(self, path: Path) -> ClaudeJsonlCursor:
        cursor = self._cursors.get(path)
        if cursor is None:
            cursor = ClaudeJsonlCursor(path=path, offset=_initial_offset(path))
            self._cursors[path] = cursor
        return cursor


def _initial_offset(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _session_id_for(path: Path) -> str:
    return f"claude-jsonl-{path.stem}"


def _title_for(path: Path) -> str:
    parent = path.parent.name.replace("-", "/").strip()
    return f"Claude · {parent}" if parent else "Claude Code"


def _cwd_for(path: Path) -> str | None:
    name = path.parent.name
    if not name:
        return None
    candidate = "/" + name.strip("-").replace("-", "/")
    return candidate if len(candidate) > 1 else None


__all__ = [
    "ClaudeTranscriptWatcher",
    "default_claude_transcript_roots",
]
