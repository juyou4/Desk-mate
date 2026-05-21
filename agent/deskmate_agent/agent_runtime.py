"""Passive runtime discovery for IDEs and local agent CLIs.

V1 deliberately avoids installing hooks or touching tool configuration.
It observes local processes and turns them into lightweight runtime
sessions so the island/session list can show that Codex, Claude Code,
Cursor, Windsurf, VSCode, etc. are alive even before a richer hook event
arrives.
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .logging_setup import get_logger
from .protocol.state import Priority
from .sessions import SessionInfo, SessionPhase, SessionState, SessionStore

_LOG = get_logger("deskmate_agent.agent_runtime")


class AgentRuntimeKind(StrEnum):
    GUI_IDE = "gui_ide"
    CLI_AGENT = "cli_agent"
    HOOK_SESSION = "hook_session"


class AgentRuntimeSource(StrEnum):
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    VSCODE = "vscode"
    XCODE = "xcode"
    JETBRAINS = "jetbrains"
    TERMINAL = "terminal"
    OPENCODE = "opencode"
    UNKNOWN = "unknown"


class AgentRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: AgentRuntimeSource = AgentRuntimeSource.UNKNOWN
    kind: AgentRuntimeKind
    process_id: int | None = None
    parent_pid: int | None = None
    display_name: str = ""
    command: str = ""
    cwd: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None
    session_id: str | None = None
    phase: SessionPhase = SessionPhase.RUNNING
    priority: Priority = Priority.P2
    last_seen_ms: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_session_id(self) -> str:
        if self.session_id:
            return self.session_id
        prefix = self.source.value
        if self.process_id is not None:
            return f"runtime-{prefix}-{self.process_id}"
        return f"runtime-{prefix}-{self.kind.value}"


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    comm: str
    args: str


class AgentRuntimeStore:
    def __init__(self, *, ttl_ms: int = 10_000) -> None:
        self._by_key: dict[str, AgentRuntimeStatus] = {}
        self._ttl_ms = max(1, ttl_ms)

    def upsert_many(self, statuses: list[AgentRuntimeStatus]) -> bool:
        changed = False
        seen_keys: set[str] = set()
        for status in statuses:
            key = self._key(status)
            seen_keys.add(key)
            if self._by_key.get(key) != status:
                self._by_key[key] = status
                changed = True
        return changed

    def expire(self, now_ms: int) -> list[AgentRuntimeStatus]:
        expired: list[AgentRuntimeStatus] = []
        cutoff = now_ms - self._ttl_ms
        for key, status in list(self._by_key.items()):
            if status.last_seen_ms < cutoff:
                expired.append(status)
                del self._by_key[key]
        return expired

    def list(self) -> list[AgentRuntimeStatus]:
        items = list(self._by_key.values())
        items.sort(
            key=lambda s: (
                _phase_rank(s.phase),
                _priority_rank(s.priority),
                -s.last_seen_ms,
                s.display_name,
            )
        )
        return items

    @staticmethod
    def _key(status: AgentRuntimeStatus) -> str:
        return status.effective_session_id


PsProvider = Callable[[], str]
Clock = Callable[[], int]


def _default_clock() -> int:
    return int(time.time() * 1000)


def _default_ps_provider() -> str:
    return subprocess.check_output(  # noqa: S603
        ["/bin/ps", "-axo", "pid=,ppid=,comm=,args="],
        text=True,
        stderr=subprocess.DEVNULL,
    )


class AgentRuntimeScanner:
    def __init__(
        self,
        store: AgentRuntimeStore,
        session_store: SessionStore,
        *,
        ps_provider: PsProvider = _default_ps_provider,
        clock: Clock = _default_clock,
        poll_interval_s: float = 2.0,
    ) -> None:
        self._store = store
        self._sessions = session_store
        self._ps_provider = ps_provider
        self._clock = clock
        self._poll = poll_interval_s
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="agent-runtime-scanner")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def scan_once(self) -> bool:
        try:
            rows = parse_ps_output(self._ps_provider())
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("agent_runtime.ps_failed", error=str(exc))
            return False
        now_ms = self._clock()
        statuses = discover_runtime_statuses(rows, now_ms=now_ms)
        changed = self._store.upsert_many(statuses)
        expired = self._store.expire(now_ms)
        if statuses:
            for status in statuses:
                self._upsert_session(status)
        for status in expired:
            self._remove_session(status)
        return changed or bool(expired)

    async def _run(self) -> None:
        while not self._stopping:
            await self.scan_once()
            await asyncio.sleep(self._poll)

    def _upsert_session(self, status: AgentRuntimeStatus) -> None:
        sid = status.effective_session_id
        existing = self._sessions.get(sid)
        if existing is not None and _is_hook_session(existing):
            return
        self._sessions.upsert(
            SessionInfo(
                session_id=sid,
                title=status.display_name or status.source.value,
                summary=_summary_for(status),
                state=SessionState.ACTIVE,
                priority=status.priority,
                created_at_ms=existing.created_at_ms if existing else status.last_seen_ms,
                updated_at_ms=status.last_seen_ms,
                phase=status.phase,
                cwd=status.cwd,
                source=status.source.value,
                kind=status.kind.value,
                process_id=status.process_id,
                extras={
                    "runtime_source": status.source.value,
                    "runtime_kind": status.kind.value,
                    "command": status.command,
                },
            )
        )

    def _remove_session(self, status: AgentRuntimeStatus) -> None:
        sid = status.effective_session_id
        existing = self._sessions.get(sid)
        if existing is not None and not _is_hook_session(existing):
            self._sessions.remove(sid)


def parse_ps_output(text: str) -> list[ProcessRow]:
    rows: list[ProcessRow] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        args = parts[3] if len(parts) > 3 else comm
        rows.append(ProcessRow(pid=pid, ppid=ppid, comm=comm, args=args))
    return rows


def discover_runtime_statuses(
    rows: list[ProcessRow], *, now_ms: int
) -> list[AgentRuntimeStatus]:
    out: list[AgentRuntimeStatus] = []
    for row in rows:
        status = _status_from_process(row, now_ms=now_ms)
        if status is not None:
            out.append(status)
    return out


def _status_from_process(row: ProcessRow, *, now_ms: int) -> AgentRuntimeStatus | None:
    executable = Path(row.comm).name.lower()
    args = row.args.lower()

    cli = _classify_cli(executable, args)
    if cli is not None:
        source, display = cli
        return AgentRuntimeStatus(
            source=source,
            kind=AgentRuntimeKind.CLI_AGENT,
            process_id=row.pid,
            parent_pid=row.ppid,
            display_name=display,
            command=row.args,
            phase=SessionPhase.RUNNING,
            priority=Priority.P1,
            last_seen_ms=now_ms,
            raw={"comm": row.comm},
        )

    gui = _classify_gui(executable, args)
    if gui is not None:
        source, display, bundle_id = gui
        return AgentRuntimeStatus(
            source=source,
            kind=AgentRuntimeKind.GUI_IDE,
            process_id=row.pid,
            parent_pid=row.ppid,
            display_name=display,
            command=row.args,
            bundle_id=bundle_id,
            phase=SessionPhase.RUNNING,
            priority=Priority.P3,
            last_seen_ms=now_ms,
            raw={"comm": row.comm},
        )
    return None


def _classify_cli(
    executable: str, args: str
) -> tuple[AgentRuntimeSource, str] | None:
    if executable in {"claude", "claude-code"} or "claude-code" in args:
        return AgentRuntimeSource.CLAUDE_CODE, "Claude Code CLI"
    if executable == "codex" or " openai/codex" in args:
        return AgentRuntimeSource.CODEX, "Codex CLI"
    if executable == "opencode" or "opencode" in args:
        return AgentRuntimeSource.OPENCODE, "OpenCode CLI"
    return None


def _classify_gui(
    executable: str, args: str
) -> tuple[AgentRuntimeSource, str, str | None] | None:
    haystack = f"{executable} {args}"
    if "cursor.app" in haystack or executable in {"cursor"}:
        return AgentRuntimeSource.CURSOR, "Cursor", "com.todesktop.230313mzl4w4u92"
    if "windsurf.app" in haystack or "windsurf" in haystack:
        return AgentRuntimeSource.WINDSURF, "Windsurf", "com.exafunction.windsurf"
    if "visual studio code.app" in haystack or (
        executable in {"code", "electron"} and "vscode" in haystack
    ):
        return AgentRuntimeSource.VSCODE, "VSCode", "com.microsoft.VSCode"
    if "xcode.app" in haystack or executable == "xcode":
        return AgentRuntimeSource.XCODE, "Xcode", "com.apple.dt.Xcode"
    if "jetbrains" in haystack or any(
        name in haystack
        for name in (
            "intellij",
            "pycharm",
            "webstorm",
            "goland",
            "rubymine",
            "clion",
        )
    ):
        return AgentRuntimeSource.JETBRAINS, "JetBrains IDE", None
    return None


def _summary_for(status: AgentRuntimeStatus) -> str:
    if status.kind is AgentRuntimeKind.CLI_AGENT:
        return "Detected running local agent process."
    return "Detected running IDE process."


def _is_hook_session(session: SessionInfo) -> bool:
    if getattr(session, "kind", None) == AgentRuntimeKind.HOOK_SESSION.value:
        return True
    extras = session.extras or {}
    return "hook_source" in extras


def _phase_rank(phase: SessionPhase) -> int:
    return {
        SessionPhase.WAITING_FOR_APPROVAL: 0,
        SessionPhase.WAITING_FOR_ANSWER: 1,
        SessionPhase.FAILED: 2,
        SessionPhase.RUNNING_TOOL: 3,
        SessionPhase.EDITING: 4,
        SessionPhase.TESTING: 5,
        SessionPhase.THINKING: 6,
        SessionPhase.RUNNING: 7,
        SessionPhase.COMPLETED: 8,
    }.get(phase, 9)


def _priority_rank(priority: Priority) -> int:
    return {
        Priority.P0: 0,
        Priority.P1: 1,
        Priority.P2: 2,
        Priority.P3: 3,
    }.get(priority, 9)


__all__ = [
    "AgentRuntimeKind",
    "AgentRuntimeScanner",
    "AgentRuntimeSource",
    "AgentRuntimeStatus",
    "AgentRuntimeStore",
    "ProcessRow",
    "discover_runtime_statuses",
    "parse_ps_output",
]
