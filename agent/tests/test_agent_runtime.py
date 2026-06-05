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


def test_classifier_recognises_extended_cli_lineup() -> None:
    """V10 polish: cover Aider, Gemini, Kimi, Qwen, Factory Droid,
    CodeBuddy, Qoder. Each line is a realistic ``ps`` row taken
    from the upstream installer doc / ``which`` output."""
    rows = parse_ps_output(
        """
        201 1 /opt/homebrew/bin/aider aider .
        202 1 /opt/homebrew/bin/python3 python3 -m aider --model gpt-4o
        203 1 /opt/homebrew/bin/gemini gemini --model gemini-2.0-flash --prompt hi
        204 1 /opt/homebrew/bin/kimi kimi
        205 1 /opt/homebrew/bin/qwen qwen-code chat
        206 1 /opt/homebrew/bin/droid droid run
        207 1 /opt/homebrew/bin/codebuddy codebuddy run
        208 1 /opt/homebrew/bin/qoder qoder
        """
    )
    sources = {s.source for s in discover_runtime_statuses(rows, now_ms=1)}
    assert AgentRuntimeSource.AIDER in sources
    assert AgentRuntimeSource.GEMINI in sources
    assert AgentRuntimeSource.KIMI in sources
    assert AgentRuntimeSource.QWEN in sources
    assert AgentRuntimeSource.FACTORY_DROID in sources
    assert AgentRuntimeSource.CODEBUDDY in sources
    assert AgentRuntimeSource.QODER in sources


def test_classifier_recognises_extended_gui_lineup() -> None:
    """Cover Zed, Trae, Sublime Text, Fleet, Nova, Neovim,
    GitHub Desktop. Bundle paths come from each app's actual
    ``Contents/MacOS`` binary on a stock install.

    ``ps -axo`` outputs spaces in paths un-escaped. Our parser
    splits on whitespace so the executable basename collapses
    to the first token, leaving the full path inside the args
    field — which is the route the args-needle matcher picks up.
    """
    rows = parse_ps_output(
        """
        301 1 /Applications/Zed.app/Contents/MacOS/zed zed
        302 1 /Applications/Trae.app/Contents/MacOS/Trae Trae /Applications/Trae.app
        303 1 /Applications/SublimeText.app/Contents/MacOS/sublime_text sublime_text
        304 1 /Applications/Fleet.app/Contents/MacOS/Fleet Fleet
        305 1 /Applications/Nova.app/Contents/MacOS/Nova Nova
        306 1 /opt/homebrew/bin/nvim nvim
        307 1 /Applications/GitHub.app/Contents/MacOS/GitHubDesktop GitHubDesktop /Applications/GitHub Desktop.app
        """
    )
    sources = {s.source for s in discover_runtime_statuses(rows, now_ms=1)}
    assert AgentRuntimeSource.ZED in sources
    assert AgentRuntimeSource.TRAE in sources
    assert AgentRuntimeSource.SUBLIME in sources
    assert AgentRuntimeSource.FLEET in sources
    assert AgentRuntimeSource.NOVA in sources
    assert AgentRuntimeSource.NEOVIM in sources
    assert AgentRuntimeSource.GITHUB_DESKTOP in sources


def test_npm_shimmed_clis_classify_as_their_agent() -> None:
    """node-running-the-bundle is the most common install path on
    macOS — Homebrew's ``claude`` symlinks resolve to a ``node`` exec
    with the agent's ``cli.js`` in argv. The interpreter
    ``require_all`` rules cover this."""
    rows = parse_ps_output(
        """
        401 1 /opt/homebrew/bin/node node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js
        402 1 /opt/homebrew/bin/node node /opt/homebrew/lib/node_modules/@openai/codex/dist/cli.js
        403 1 /opt/homebrew/bin/bun bun /Users/dev/.bun/install/global/node_modules/@sst/opencode/cli.mjs
        """
    )
    sources = {s.source for s in discover_runtime_statuses(rows, now_ms=1)}
    assert AgentRuntimeSource.CLAUDE_CODE in sources
    assert AgentRuntimeSource.CODEX in sources
    assert AgentRuntimeSource.OPENCODE in sources


def test_plain_node_does_not_falsely_classify_as_agent() -> None:
    """``node app.js`` running an unrelated project must NOT classify
    as Claude / Codex / OpenCode — the interpreter rule requires both
    the executable and an arg needle to agree."""
    rows = parse_ps_output(
        """
        501 1 /opt/homebrew/bin/node node /Users/dev/myapp/server.js
        502 1 /opt/homebrew/bin/python3 python3 server.py
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=1)
    assert statuses == []


def test_electron_helpers_collapse_to_single_session() -> None:
    """Cursor / VSCode launch a renderer + GPU + crashpad helper for
    every window. We must surface ONE row per IDE, not one per
    helper. Lowest-pid (the main process) wins.

    ``ps`` emits spaces in paths un-escaped; the parser splits on
    whitespace so we encode the helper paths without the bundle
    name boundary using compact bundle names ("CursorHelper"
    instead of ``Cursor Helper.app``). What matters for the test
    is that the args still contain ``cursor.app`` (main process)
    or ``(renderer)`` / ``(gpu)`` (helpers), exercising the
    helper_needles filter.
    """
    rows = parse_ps_output(
        """
        601 1 /Applications/Cursor.app/Contents/MacOS/Cursor Cursor /Applications/Cursor.app
        602 601 /Applications/Cursor.app/Contents/Frameworks/CursorHelper.app/Contents/MacOS/CursorHelper CursorHelper (GPU)
        603 601 /Applications/Cursor.app/Contents/Frameworks/CursorHelper.app/Contents/MacOS/CursorHelper CursorHelper (Renderer)
        604 601 /Applications/Cursor.app/Contents/Frameworks/CursorHelper.app/Contents/MacOS/CursorHelper CursorHelper (Plugin)
        605 1 /Applications/VSCode.app/Contents/MacOS/Electron Electron /Applications/Visual Studio Code.app
        606 605 /Applications/VSCode.app/Contents/Frameworks/CodeHelper.app/Contents/MacOS/CodeHelper CodeHelper (Renderer)
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=1)
    by_source = {s.source: s for s in statuses}
    # Exactly one Cursor row, exactly one VSCode row, both
    # carrying the main-process pid.
    assert by_source[AgentRuntimeSource.CURSOR].process_id == 601
    assert by_source[AgentRuntimeSource.VSCODE].process_id == 605
    assert sum(1 for s in statuses if s.source is AgentRuntimeSource.CURSOR) == 1
    assert sum(1 for s in statuses if s.source is AgentRuntimeSource.VSCODE) == 1


def test_workspace_hint_extracted_from_folder_uri() -> None:
    """When the user opens a workspace via the Dock, VSCode-family
    apps relaunch with ``--folder-uri`` in argv. We pluck that
    into ``cwd`` so "Jump to session" can open the folder."""
    rows = parse_ps_output(
        """
        701 1 /Applications/Cursor.app/Contents/MacOS/Cursor Cursor --folder-uri file:///Users/dev/projects/deskmate
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=1)
    cursor = next(s for s in statuses if s.source is AgentRuntimeSource.CURSOR)
    assert cursor.cwd == "/Users/dev/projects/deskmate"


def test_workspace_hint_handles_url_encoded_path() -> None:
    """Folder names with spaces are percent-escaped in the URI;
    the extractor must round-trip them through ``unquote`` so the
    user gets the original path."""
    rows = parse_ps_output(
        """
        801 1 /Applications/Cursor.app/Contents/MacOS/Cursor Cursor --folder-uri=file:///Users/dev/My%20Project
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=1)
    cursor = next(s for s in statuses if s.source is AgentRuntimeSource.CURSOR)
    assert cursor.cwd == "/Users/dev/My Project"


def test_cli_agents_keep_separate_rows_per_pid() -> None:
    """Two terminals each running ``codex`` must surface as two
    distinct sessions. Dedupe only collapses GUI helpers, not
    independent CLI invocations."""
    rows = parse_ps_output(
        """
        901 1 /opt/homebrew/bin/codex codex
        902 1 /opt/homebrew/bin/codex codex
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=1)
    assert len(statuses) == 2
    pids = {s.process_id for s in statuses}
    assert pids == {901, 902}


# ---------------------------------------------------------------------------
# Task 15.4: Unit tests for agent_runtime unobserved timer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_unobserved_after_30s():
    """R11.1: codex process without events gets phase_source=unobserved after 30s."""
    store = AgentRuntimeStore()
    sessions = SessionStore()
    ps_output = "12345 1 /opt/homebrew/bin/codex codex\n"
    clock_ms = 1_000
    scanner = AgentRuntimeScanner(
        store,
        sessions,
        ps_provider=lambda: ps_output,
        clock=lambda: clock_ms,
    )

    # First scan — records first_observed_ms, phase_source should be None
    await scanner.scan_once()
    s = sessions.get("runtime-codex-12345")
    assert s is not None
    assert s.phase_source is None  # not yet 30s

    # Advance clock past 30s
    clock_ms = 31_000
    scanner._clock = lambda: clock_ms
    await scanner.scan_once()
    s = sessions.get("runtime-codex-12345")
    assert s is not None
    assert s.phase_source == "unobserved"
