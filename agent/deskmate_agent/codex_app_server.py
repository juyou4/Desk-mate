"""Codex.app app-server bridge.

Codex desktop exposes a stdio JSON-RPC server via its bundled ``codex``
executable. This module keeps that transport separate from Deskmate's Swift
bridge and converts Codex thread/turn notifications into shared AgentEvents.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .agent_events import (
    AgentEvent,
    PermissionRequested,
    QuestionAsked,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
)
from .logging_setup import get_logger
from .sessions import SessionPhase

_LOG = get_logger("deskmate_agent.codex_app_server")


class CodexThreadStatusType(StrEnum):
    NOT_LOADED = "notLoaded"
    IDLE = "idle"
    SYSTEM_ERROR = "systemError"
    ACTIVE = "active"
    UNKNOWN = "unknown"


class CodexTurnStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    IN_PROGRESS = "inProgress"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CodexThreadStatus:
    type: CodexThreadStatusType
    active_flags: tuple[str, ...] = ()

    @property
    def is_waiting_on_approval(self) -> bool:
        return "waitingOnApproval" in self.active_flags

    @property
    def is_waiting_on_user_input(self) -> bool:
        return "waitingOnUserInput" in self.active_flags


@dataclass(frozen=True)
class CodexTurn:
    id: str
    status: CodexTurnStatus = CodexTurnStatus.UNKNOWN


@dataclass(frozen=True)
class CodexThread:
    id: str
    cwd: str = ""
    name: str | None = None
    preview: str = ""
    created_at: int = 0
    updated_at: int = 0
    ephemeral: bool = False
    status: CodexThreadStatus = field(
        default_factory=lambda: CodexThreadStatus(CodexThreadStatusType.UNKNOWN)
    )


@dataclass(frozen=True)
class CodexNotification:
    method: str
    thread: CodexThread | None = None
    thread_id: str | None = None
    status: CodexThreadStatus | None = None
    turn: CodexTurn | None = None
    raw: dict[str, Any] = field(default_factory=dict)


AgentEventHandler = Callable[[AgentEvent], Awaitable[None]]
NotificationHandler = Callable[[CodexNotification], Awaitable[None]]
Clock = Callable[[], int]


def default_codex_app_server_path() -> Path | None:
    override = os.environ.get("DESKMATE_CODEX_APP_SERVER_PATH")
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    candidates = [
        Path("/Applications/Codex.app/Contents/Resources/codex"),
        Path.home() / "Applications/Codex.app/Contents/Resources/codex",
    ]
    for path in candidates:
        if path.exists() and os.access(path, os.X_OK):
            return path
    found = shutil.which("codex")
    return Path(found) if found else None


class CodexAppServerClient:
    def __init__(
        self,
        codex_path: Path,
        *,
        notification_handler: NotificationHandler | None = None,
        request_timeout_s: float = 5.0,
    ) -> None:
        self._path = codex_path
        self._notification_handler = notification_handler
        self._timeout = request_timeout_s
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1

    async def start(self) -> None:
        if self._process is not None:
            return
        self._process = await asyncio.create_subprocess_exec(
            str(self._path),
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader_task = asyncio.create_task(self._read_loop(), name="codex-app-server")
        await self.send_request(
            "initialize",
            {"clientInfo": {"name": "Deskmate", "version": "0.1.0"}},
        )

    async def stop(self) -> None:
        task = self._reader_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reader_task = None
        proc = self._process
        self._process = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError("codex app-server disconnected"))
        self._pending.clear()
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()

    async def list_loaded_threads(self) -> list[CodexThread]:
        result = await self.send_request("thread/loaded/list", {})
        raw_threads = result.get("threads", [])
        if not isinstance(raw_threads, list):
            return []
        return [
            thread
            for raw in raw_threads
            if isinstance(raw, dict)
            if (thread := _parse_thread(raw)) is not None
        ]

    async def send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        proc = self._process
        if proc is None or proc.stdin is None:
            raise RuntimeError("codex app-server is not connected")
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = fut
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        proc.stdin.write(line)
        await proc.stdin.drain()
        return await asyncio.wait_for(fut, timeout=self._timeout)

    async def _read_loop(self) -> None:
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if "id" in msg:
                self._handle_response(msg)
            elif isinstance(msg.get("method"), str):
                await self._handle_notification(msg)

    def _handle_response(self, msg: dict[str, Any]) -> None:
        request_id = msg.get("id")
        if not isinstance(request_id, int):
            return
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return
        if isinstance(msg.get("error"), dict):
            error = msg["error"]
            fut.set_exception(RuntimeError(str(error.get("message", "Codex RPC error"))))
            return
        result = msg.get("result")
        fut.set_result(result if isinstance(result, dict) else {})

    async def _handle_notification(self, msg: dict[str, Any]) -> None:
        if self._notification_handler is None:
            return
        notification = parse_codex_notification(msg)
        if notification is not None:
            await self._notification_handler(notification)


class CodexAppServerCoordinator:
    def __init__(
        self,
        *,
        event_handler: AgentEventHandler,
        codex_path_provider: Callable[[], Path | None] = default_codex_app_server_path,
        client_factory: Callable[[Path, NotificationHandler], CodexAppServerClient]
        | None = None,
        clock: Clock = lambda: int(time.time() * 1000),
    ) -> None:
        self._event_handler = event_handler
        self._path_provider = codex_path_provider
        self._client_factory = client_factory or (
            lambda path, handler: CodexAppServerClient(path, notification_handler=handler)
        )
        self._clock = clock
        self._client: CodexAppServerClient | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            return
        path = self._path_provider()
        if path is None:
            _LOG.info("codex_app_server.not_found")
            return
        self._task = asyncio.create_task(self._run(path), name="codex-app-server-coordinator")

    async def stop(self) -> None:
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        if self._client is not None:
            await self._client.stop()
        self._client = None

    async def _run(self, path: Path) -> None:
        client = self._client_factory(path, self.handle_notification)
        self._client = client
        try:
            await client.start()
            for thread in await client.list_loaded_threads():
                if not thread.ephemeral:
                    await self._event_handler(event_from_codex_thread_started(thread, self._clock()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("codex_app_server.start_failed", error=str(exc), path=str(path))

    async def handle_notification(self, notification: CodexNotification) -> None:
        event = agent_event_from_codex_notification(notification, ts_ms=self._clock())
        if event is not None:
            await self._event_handler(event)


def parse_codex_notification(msg: dict[str, Any]) -> CodexNotification | None:
    method = msg.get("method")
    if not isinstance(method, str):
        return None
    params = msg.get("params")
    params = params if isinstance(params, dict) else {}
    if method == "thread/started":
        raw_thread = params.get("thread")
        thread = _parse_thread(raw_thread) if isinstance(raw_thread, dict) else None
        return CodexNotification(method=method, thread=thread, raw=msg)
    if method == "thread/status/changed":
        return CodexNotification(
            method=method,
            thread_id=_optional_string(params, "threadId", "thread_id"),
            status=_parse_status(params.get("status")),
            raw=msg,
        )
    if method == "thread/closed":
        return CodexNotification(
            method=method,
            thread_id=_optional_string(params, "threadId", "thread_id"),
            raw=msg,
        )
    if method in {"turn/started", "turn/completed"}:
        raw_turn = params.get("turn")
        return CodexNotification(
            method=method,
            thread_id=_optional_string(params, "threadId", "thread_id"),
            turn=_parse_turn(raw_turn) if isinstance(raw_turn, dict) else None,
            raw=msg,
        )
    return CodexNotification(method=method, raw=msg)


def agent_event_from_codex_notification(
    notification: CodexNotification, *, ts_ms: int
) -> AgentEvent | None:
    if notification.method == "thread/started" and notification.thread is not None:
        if notification.thread.ephemeral:
            return None
        return event_from_codex_thread_started(notification.thread, ts_ms)

    if notification.method == "thread/status/changed":
        thread_id = notification.thread_id
        status = notification.status
        if not thread_id or status is None:
            return None
        if status.type is CodexThreadStatusType.ACTIVE:
            if status.is_waiting_on_approval:
                return PermissionRequested(
                    session_id=thread_id,
                    source="codex",
                    ts_ms=ts_ms,
                    title="Codex session",
                    summary="Codex is waiting for approval.",
                    approval_id=f"{thread_id}-approval",
                    prompt="Codex is waiting for approval.",
                    raw_event=notification.method,
                )
            if status.is_waiting_on_user_input:
                return QuestionAsked(
                    session_id=thread_id,
                    source="codex",
                    ts_ms=ts_ms,
                    title="Codex session",
                    summary="Codex is waiting for input.",
                    prompt="Codex is waiting for input.",
                    raw_event=notification.method,
                )
            return SessionActivityUpdated(
                session_id=thread_id,
                source="codex",
                ts_ms=ts_ms,
                title="Codex session",
                summary="Codex is working.",
                phase=SessionPhase.RUNNING,
                raw_event=notification.method,
            )
        if status.type is CodexThreadStatusType.IDLE:
            return SessionActivityUpdated(
                session_id=thread_id,
                source="codex",
                ts_ms=ts_ms,
                title="Codex session",
                summary="Codex is idle.",
                phase=SessionPhase.COMPLETED,
                raw_event=notification.method,
            )
        if status.type is CodexThreadStatusType.SYSTEM_ERROR:
            return SessionCompleted(
                session_id=thread_id,
                source="codex",
                ts_ms=ts_ms,
                title="Codex session",
                summary="Codex thread hit a system error.",
                failed=True,
                raw_event=notification.method,
            )
        return None

    if notification.method == "thread/closed" and notification.thread_id:
        return SessionCompleted(
            session_id=notification.thread_id,
            source="codex",
            ts_ms=ts_ms,
            title="Codex session",
            summary="Codex thread closed.",
            raw_event=notification.method,
        )

    if notification.method == "turn/started" and notification.thread_id:
        return SessionActivityUpdated(
            session_id=notification.thread_id,
            source="codex",
            ts_ms=ts_ms,
            title="Codex session",
            summary="Codex is working.",
            phase=SessionPhase.RUNNING,
            raw_event=notification.method,
        )

    if notification.method == "turn/completed" and notification.thread_id:
        status = notification.turn.status if notification.turn is not None else CodexTurnStatus.UNKNOWN
        failed = status is CodexTurnStatus.FAILED
        summary = {
            CodexTurnStatus.COMPLETED: "Codex turn completed.",
            CodexTurnStatus.INTERRUPTED: "Codex turn interrupted.",
            CodexTurnStatus.FAILED: "Codex turn failed.",
            CodexTurnStatus.IN_PROGRESS: "Codex turn is still in progress.",
            CodexTurnStatus.UNKNOWN: "Codex turn completed.",
        }[status]
        return SessionCompleted(
            session_id=notification.thread_id,
            source="codex",
            ts_ms=ts_ms,
            title="Codex session",
            summary=summary,
            failed=failed,
            raw_event=notification.method,
        )

    return None


def event_from_codex_thread_started(thread: CodexThread, ts_ms: int) -> SessionStarted:
    title = thread.name or _title_from_cwd(thread.cwd) or "Codex session"
    summary = thread.preview or "Codex thread loaded."
    phase = _phase_from_status(thread.status)
    return SessionStarted(
        session_id=thread.id,
        source="codex",
        ts_ms=ts_ms,
        title=f"Codex · {title}" if not title.startswith("Codex") else title,
        summary=summary,
        cwd=thread.cwd or None,
        jump_url=f"codex://threads/{thread.id}",
        raw_event="thread/started",
        phase=phase,
    )


def _parse_thread(raw: dict[str, Any]) -> CodexThread | None:
    thread_id = raw.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return CodexThread(
        id=thread_id,
        cwd=str(raw.get("cwd") or ""),
        name=str(raw["name"]) if raw.get("name") is not None else None,
        preview=str(raw.get("preview") or ""),
        created_at=_int_value(raw.get("createdAt") or raw.get("created_at")),
        updated_at=_int_value(raw.get("updatedAt") or raw.get("updated_at")),
        ephemeral=bool(raw.get("ephemeral", False)),
        status=_parse_status(raw.get("status")),
    )


def _parse_status(raw: object) -> CodexThreadStatus:
    if not isinstance(raw, dict):
        return CodexThreadStatus(CodexThreadStatusType.UNKNOWN)
    raw_type = str(raw.get("type") or "")
    try:
        status_type = CodexThreadStatusType(raw_type)
    except ValueError:
        status_type = CodexThreadStatusType.UNKNOWN
    flags = raw.get("activeFlags") or raw.get("active_flags") or []
    if not isinstance(flags, list):
        flags = []
    return CodexThreadStatus(
        type=status_type,
        active_flags=tuple(str(flag) for flag in flags),
    )


def _parse_turn(raw: dict[str, Any]) -> CodexTurn:
    raw_status = str(raw.get("status") or "")
    try:
        status = CodexTurnStatus(raw_status)
    except ValueError:
        status = CodexTurnStatus.UNKNOWN
    return CodexTurn(id=str(raw.get("id") or ""), status=status)


def _phase_from_status(status: CodexThreadStatus) -> SessionPhase:
    if status.type is CodexThreadStatusType.ACTIVE:
        if status.is_waiting_on_approval:
            return SessionPhase.WAITING_FOR_APPROVAL
        if status.is_waiting_on_user_input:
            return SessionPhase.WAITING_FOR_ANSWER
        return SessionPhase.RUNNING
    if status.type is CodexThreadStatusType.IDLE:
        return SessionPhase.COMPLETED
    if status.type is CodexThreadStatusType.SYSTEM_ERROR:
        return SessionPhase.FAILED
    return SessionPhase.RUNNING


def _optional_string(raw: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _int_value(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _title_from_cwd(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).name or cwd


__all__ = [
    "CodexAppServerClient",
    "CodexAppServerCoordinator",
    "CodexNotification",
    "CodexThread",
    "CodexThreadStatus",
    "CodexThreadStatusType",
    "CodexTurn",
    "CodexTurnStatus",
    "agent_event_from_codex_notification",
    "default_codex_app_server_path",
    "event_from_codex_thread_started",
    "parse_codex_notification",
]
