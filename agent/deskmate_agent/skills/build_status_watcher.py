"""File-poll watcher for the build-status skill (V10 Phase 14-i).

Dedicated module so :class:`BuildStatusSkill` stays pure and
re-usable (it only knows about :class:`CompanionIntent` emission).
The watcher is the only piece that touches the filesystem.

Why poll a JSON file instead of an RPC channel:

- The existing bridge socket is a single-consumer pipe to the Swift
  shell; plumbing a second producer would be a large refactor.
- A file is trivially integrable from any tool (Makefile, npm
  script, pytest plugin, CI runner, …) in one line: ``echo '{...}'
  > ~/.deskmate/build-status.json``.
- The watcher eats the file after reading, so there's no stale
  state to manage on disk.

The cadence (``poll_interval_s``) trades latency for CPU cost. The
default 0.3 s is snappy enough that the pill lights up before the
user's editor has finished animating its own "building" indicator.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

from ..logging_setup import get_logger
from .build_status import BuildStatusSkill

_LOGGER = get_logger(__name__)


class BuildStatusWatcher:
    """Poll ``path`` and route JSON lines into ``skill``."""

    def __init__(
        self,
        skill: BuildStatusSkill,
        *,
        path: Path | None = None,
        poll_interval_s: float = 0.3,
    ) -> None:
        self._skill = skill
        self._path = path or Path.home() / ".deskmate" / "build-status.json"
        self._poll_s = poll_interval_s
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_s)
                await self._drain_once()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001 — fail-soft
                _LOGGER.warning(
                    "build_status_watcher.tick_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                )

    async def _drain_once(self) -> None:
        """Read + consume the file if present. Exposed for tests."""
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text()
            # Consume immediately so a slow handler can't double-fire
            # the same update.
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning(
                "build_status_watcher.read_error", error=str(exc)
            )
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _LOGGER.warning(
                "build_status_watcher.decode_error",
                error=str(exc),
                payload=raw[:200],
            )
            return

        await self._dispatch(data)

    async def _dispatch(self, data: dict) -> None:
        state = str(data.get("state", "")).strip()
        task = data.get("task")
        message = data.get("message")
        branch = data.get("branch")
        # Normalize branch: only strings, only non-empty after strip.
        branch = branch.strip() or None if isinstance(branch, str) else None
        if state == "dismiss":
            await self._skill.on_external_dismiss()
            return
        if not isinstance(task, str) or not task:
            _LOGGER.warning(
                "build_status_watcher.missing_task", state=state
            )
            return
        if state == "started":
            await self._skill.on_build_start(task, branch=branch)
        elif state == "progress":
            try:
                progress = float(data.get("progress", 0))
            except (TypeError, ValueError):
                progress = 0.0
            await self._skill.on_build_progress(
                task, progress, message=message, branch=branch
            )
        elif state == "done":
            await self._skill.on_build_done(
                task, success=True, message=message, branch=branch
            )
        elif state == "failed":
            await self._skill.on_build_done(
                task, success=False, message=message, branch=branch
            )
        else:
            _LOGGER.warning(
                "build_status_watcher.unknown_state", state=state
            )


__all__ = ["BuildStatusWatcher"]
