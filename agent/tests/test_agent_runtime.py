"""Passive IDE / agent runtime discovery tests."""

from __future__ import annotations

import pytest

from deskmate_agent.agent_runtime import (
    AgentRuntimeKind,
    AgentRuntimeScanner,
    AgentRuntimeSource,
    AgentRuntimeStore,
    discover_runtime_statuses,
    parse_ps_output,
)
from deskmate_agent.sessions import SessionInfo, SessionStore


def test_parse_ps_output_and_discover_known_processes() -> None:
    rows = parse_ps_output(
        """
        101 1 /opt/homebrew/bin/claude claude --continue
        102 1 /opt/homebrew/bin/codex codex
        103 1 /Applications/Cursor.app/Contents/MacOS/Cursor Cursor
        104 1 /Applications/Windsurf.app/Contents/MacOS/Windsurf Windsurf
        105 1 /Applications/Visual Studio Code.app/Contents/MacOS/Electron Electron /Applications/Visual Studio Code.app
        """
    )

    statuses = discover_runtime_statuses(rows, now_ms=12_000)
    pairs = {(s.source, s.kind) for s in statuses}

    assert (AgentRuntimeSource.CLAUDE_CODE, AgentRuntimeKind.CLI_AGENT) in pairs
    assert (AgentRuntimeSource.CODEX, AgentRuntimeKind.CLI_AGENT) in pairs
    assert (AgentRuntimeSource.CURSOR, AgentRuntimeKind.GUI_IDE) in pairs
    assert (AgentRuntimeSource.WINDSURF, AgentRuntimeKind.GUI_IDE) in pairs
    assert (AgentRuntimeSource.VSCODE, AgentRuntimeKind.GUI_IDE) in pairs


@pytest.mark.asyncio
async def test_scanner_creates_and_expires_fallback_sessions() -> None:
    now = 1_000
    ps_text = "101 1 /opt/homebrew/bin/codex codex\n"
    store = AgentRuntimeStore(ttl_ms=10_000)
    sessions = SessionStore()
    scanner = AgentRuntimeScanner(
        store,
        sessions,
        ps_provider=lambda: ps_text,
        clock=lambda: now,
    )

    assert await scanner.scan_once()
    assert sessions.get("runtime-codex-101") is not None

    ps_text = ""
    now = 12_001
    assert await scanner.scan_once()
    assert sessions.get("runtime-codex-101") is None


@pytest.mark.asyncio
async def test_hook_session_is_not_overwritten_by_process_fallback() -> None:
    store = AgentRuntimeStore()
    sessions = SessionStore()
    sessions.upsert(
        SessionInfo(
            session_id="runtime-codex-101",
            title="Codex precise hook",
            source="codex",
            kind="hook_session",
            extras={"hook_source": "codex"},
        )
    )
    scanner = AgentRuntimeScanner(
        store,
        sessions,
        ps_provider=lambda: "101 1 /opt/homebrew/bin/codex codex\n",
        clock=lambda: 1_000,
    )

    await scanner.scan_once()

    got = sessions.get("runtime-codex-101")
    assert got is not None
    assert got.title == "Codex precise hook"
