"""V10 L1-F — session interaction router."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)
from deskmate_agent.protocol.intents import CompanionIntent, IntentKind
from deskmate_agent.sessions import (
    SessionInfo,
    SessionInteractionRouter,
    SessionPhase,
    SessionState,
    SessionStore,
)
from deskmate_agent.sessions.router import (
    TerminalJumpTarget,
    _activation_candidates,
    _default_terminal_router,
    _workspace_candidates,
)


def _store_with(sid: str) -> SessionStore:
    store = SessionStore()
    store.upsert(SessionInfo(session_id=sid, created_at_ms=1_000, updated_at_ms=1_000))
    return store


def _jump(sid: str) -> InteractionAction:
    return InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.SESSION_JUMP,
        payload={"session_id": sid},
    )


def _answer(sid: str, answer: str) -> InteractionAction:
    return InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.QUESTION_ANSWER,
        payload={"session_id": sid, "answer": answer},
    )


async def test_session_jump_accepted_touches_existing_session() -> None:
    store = _store_with("s1")
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = await router.handle(_jump("s1"))

    assert result.handled
    assert result.effect == "session.jump.accepted"
    assert result.session_id == "s1"
    got = store.get("s1")
    assert got is not None
    assert got.state is SessionState.ACTIVE
    assert got.updated_at_ms == 7_777


async def test_session_jump_opens_allowed_url_scheme() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            jump_url="codex://session/s1",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.handled
    assert result.effect == "session.jump.opened"
    assert opened == ["codex://session/s1"]


async def test_session_jump_rejects_unallowed_url_and_uses_cwd(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            jump_url="https://example.com/not-allowed",
            cwd=str(tmp_path),
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == [str(tmp_path)]


async def test_session_jump_opens_workspace_with_source_app_before_plain_cwd(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="cursor",
            kind="gui_ide",
            cwd=str(tmp_path),
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    workspaces: list[tuple[str, str]] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
        workspace_opener=lambda app, path: workspaces.append((app, path)) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.workspace_opened"
    assert workspaces == [("Cursor", str(tmp_path))]
    assert opened == []


async def test_session_jump_workspace_route_falls_back_to_plain_cwd(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="windsurf",
            kind="gui_ide",
            cwd=str(tmp_path),
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    workspaces: list[tuple[str, str]] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
        workspace_opener=lambda app, path: workspaces.append((app, path)) and False,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert workspaces == [("Windsurf", str(tmp_path))]
    assert opened == [str(tmp_path)]


async def test_session_jump_url_beats_workspace_route(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="cursor",
            jump_url="codex://session/s1",
            cwd=str(tmp_path),
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    workspaces: list[tuple[str, str]] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
        workspace_opener=lambda app, path: workspaces.append((app, path)) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == ["codex://session/s1"]
    assert workspaces == []


async def test_session_jump_does_not_open_missing_local_path(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            cwd=str(tmp_path / "missing"),
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.accepted"
    assert opened == []


async def test_session_jump_activates_known_source_when_no_precise_target() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="cursor",
            kind="gui_ide",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    activated: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        activator=lambda app: activated.append(app) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.activated"
    assert activated == ["Cursor"]


async def test_session_jump_cli_agent_falls_back_to_terminal_activation() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="claude_code",
            kind="cli_agent",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    activated: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        activator=lambda app: activated.append(app) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.activated"
    assert activated == ["Terminal"]


async def test_session_jump_activation_tries_candidate_fallbacks() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="claude_code",
            kind="cli_agent",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    activated: list[str] = []

    def activate(app: str) -> bool:
        activated.append(app)
        return app == "Ghostty"

    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        activator=activate,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.activated"
    assert activated == ["Terminal", "iTerm", "Ghostty"]


async def test_session_jump_terminal_route_uses_precise_locator(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="codex",
            kind="cli_agent",
            cwd=str(tmp_path),
            extras={
                "terminal_app": "Ghostty",
                "tmux_target": "deskmate:1.2",
                "tmux_socket_path": "/tmp/tmux-deskmate",
            },
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    targets: list[TerminalJumpTarget] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        workspace_opener=lambda _app, _path: False,
        activator=lambda _app: False,
        terminal_router=lambda target: targets.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.terminal_routed"
    assert result.details is not None
    assert result.details["route"] == "terminal"
    assert "Ghostty" in result.details["detail"]
    assert len(targets) == 1
    assert targets[0].terminal_app == "Ghostty"
    assert targets[0].tmux_target == "deskmate:1.2"
    assert targets[0].tmux_socket_path == "/tmp/tmux-deskmate"
    assert targets[0].cwd == str(tmp_path)
    got = store.get("s1")
    assert got is not None
    assert got.state is SessionState.ACTIVE
    assert got.updated_at_ms == 7_777
    assert got.extras["last_jump_effect"] == "session.jump.terminal_routed"
    assert got.extras["last_jump_route"] == "terminal"
    assert "Ghostty" in got.extras["last_jump_detail"]
    assert "terminal:routed" in got.extras["last_jump_attempts"]
    assert got.extras["last_jump_at_ms"] == "7777"


async def test_session_jump_url_beats_terminal_route(tmp_path) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            jump_url="codex://session/s1",
            cwd=str(tmp_path),
            extras={"terminal_app": "Terminal", "terminal_tty": "/dev/ttys001"},
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    opened: list[str] = []
    targets: list[TerminalJumpTarget] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
        terminal_router=lambda target: targets.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == ["codex://session/s1"]
    assert targets == []


async def test_session_jump_terminal_route_failure_falls_back_to_workspace(
    tmp_path,
) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            source="cursor",
            kind="gui_ide",
            cwd=str(tmp_path),
            extras={"terminalApp": "iTerm2", "terminalSessionID": "w1/t1/s1"},
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    targets: list[TerminalJumpTarget] = []
    workspaces: list[tuple[str, str]] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        terminal_router=lambda target: targets.append(target) and False,
        workspace_opener=lambda app, path: workspaces.append((app, path)) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.workspace_opened"
    assert result.details is not None
    assert result.details["route"] == "workspace"
    assert len(targets) == 1
    assert targets[0].terminal_app == "iTerm2"
    assert targets[0].terminal_session_id == "w1/t1/s1"
    assert workspaces == [("Cursor", str(tmp_path))]
    got = store.get("s1")
    assert got is not None
    assert got.extras["last_jump_effect"] == "session.jump.workspace_opened"
    assert got.extras["last_jump_route"] == "workspace"
    assert got.extras["last_jump_detail"] == "Opened workspace in Cursor."
    assert "terminal:route_failed" in got.extras["last_jump_attempts"]
    assert "workspace:Cursor:opened" in got.extras["last_jump_attempts"]


async def test_session_jump_without_precise_terminal_locator_skips_terminal_router(
    tmp_path,
) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            cwd=str(tmp_path),
            extras={"terminal_app": "Terminal"},
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    targets: list[TerminalJumpTarget] = []
    opened: list[str] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda target: opened.append(target) is None,
        terminal_router=lambda target: targets.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == [str(tmp_path)]
    assert targets == []
    got = store.get("s1")
    assert got is not None
    assert got.extras["last_jump_route"] == "local_path"
    assert got.extras["last_jump_detail"] == "Opened local working directory."
    assert "terminal:missing_precise_locator" in got.extras["last_jump_attempts"]


async def test_session_jump_terminal_target_accepts_camel_case_cmux_fields() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            extras={
                "terminalApp": "cmux",
                "paneTitle": "codex",
                "cmuxWorkspaceId": "workspace-1",
                "cmuxSurfaceId": "surface-1",
            },
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    targets: list[TerminalJumpTarget] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        activator=lambda _app: False,
        terminal_router=lambda target: targets.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.terminal_routed"
    assert len(targets) == 1
    assert targets[0].terminal_app == "cmux"
    assert targets[0].pane_title == "codex"
    assert targets[0].cmux_workspace_id == "workspace-1"
    assert targets[0].cmux_surface_id == "surface-1"


def test_default_terminal_router_focuses_cmux_surface(monkeypatch, tmp_path) -> None:
    import deskmate_agent.sessions.router as router_module

    socket_path = Path("/tmp") / f"deskmate-cmux-{os.getpid()}-{id(tmp_path)}.sock"
    socket_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.settimeout(2)
    server.bind(str(socket_path))
    server.listen(1)
    received: list[str] = []

    def _serve_once() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            received.append(conn.recv(4096).decode("utf-8"))

    thread = threading.Thread(target=_serve_once)
    thread.start()
    activated: list[str] = []
    monkeypatch.setattr(
        router_module,
        "_resolve_cmux_socket_path",
        lambda: str(socket_path),
    )
    monkeypatch.setattr(
        router_module,
        "_default_activator",
        lambda app: activated.append(app) and False,
    )
    try:
        routed = _default_terminal_router(
            TerminalJumpTarget(
                terminal_app="cmux",
                cmux_surface_id="surface-1",
            )
        )
    finally:
        server.close()
        thread.join(timeout=2)
        socket_path.unlink(missing_ok=True)

    assert routed is True
    assert activated == ["cmux"]
    assert len(received) == 1
    payload = json.loads(received[0])
    assert payload == {
        "jsonrpc": "2.0",
        "method": "surface.focus",
        "params": {"surface_id": "surface-1"},
        "id": 1,
    }


def test_default_terminal_router_focuses_iterm_session(monkeypatch) -> None:
    import deskmate_agent.sessions.router as router_module

    scripts: list[str] = []

    def run_osascript(script: str) -> str | None:
        scripts.append(script)
        if "repeat with aWindow in windows" in script:
            return "matched"
        return None

    monkeypatch.setattr(
        router_module,
        "_run_osascript",
        run_osascript,
    )

    routed = _default_terminal_router(
        TerminalJumpTarget(
            terminal_app="iTerm2",
            terminal_session_id='session-"quoted"',
            terminal_tty="/dev/ttys002",
        )
    )

    assert routed is True
    assert len(scripts) == 2
    assert 'tell application "iTerm"' in scripts[0]
    assert 'id of aSession as text) is "session-\\"quoted\\""' in scripts[0]
    assert 'tty of aSession as text) is "/dev/ttys002"' in scripts[0]


def test_default_terminal_router_focuses_ghostty_terminal(monkeypatch) -> None:
    import deskmate_agent.sessions.router as router_module

    scripts: list[str] = []

    def run_osascript(script: str) -> str | None:
        scripts.append(script)
        if 'tell application "Ghostty"' in script:
            return "matched"
        return None

    monkeypatch.setattr(
        router_module,
        "_run_osascript",
        run_osascript,
    )

    routed = _default_terminal_router(
        TerminalJumpTarget(
            terminal_app="Ghostty",
            terminal_session_id='ghostty-"quoted"',
            pane_title='codex "demo"',
            cwd="/Users/test/demo",
        )
    )

    assert routed is True
    assert len(scripts) == 2
    assert 'tell application "Ghostty"' in scripts[0]
    assert 'id of aTerminal as text) is "ghostty-\\"quoted\\""' in scripts[0]
    assert 'working directory of aTerminal as text) is "/Users/test/demo"' in scripts[0]
    assert 'name of aTerminal as text) contains "codex \\"demo\\""' in scripts[0]
    assert "activate window targetWindow" in scripts[0]
    assert "select tab targetTab" in scripts[0]
    assert "focus targetTerminal" in scripts[0]
    assert "repeat 3 times" in scripts[0]


def test_default_terminal_router_focuses_ghostty_by_title_without_session_id(
    monkeypatch,
) -> None:
    import deskmate_agent.sessions.router as router_module

    scripts: list[str] = []

    def run_osascript(script: str) -> str | None:
        scripts.append(script)
        if 'tell application "Ghostty"' in script:
            return "matched"
        return None

    monkeypatch.setattr(
        router_module,
        "_run_osascript",
        run_osascript,
    )

    routed = _default_terminal_router(
        TerminalJumpTarget(
            terminal_app="Ghostty",
            pane_title="codex demo",
            cwd="/Users/test/demo",
        )
    )

    assert routed is True
    assert len(scripts) == 2
    assert 'if "" is "" then' in scripts[0]
    assert 'working directory of aTerminal as text) is "/Users/test/demo"' in scripts[0]
    assert 'name of aTerminal as text) contains "codex demo"' in scripts[0]
    assert "focus targetTerminal" in scripts[0]


async def test_session_jump_ghostty_pane_title_counts_as_precise_locator(
    tmp_path,
) -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            cwd=str(tmp_path),
            extras={
                "terminalApp": "Ghostty",
                "paneTitle": "codex demo",
            },
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    targets: list[TerminalJumpTarget] = []
    router = SessionInteractionRouter(
        store,
        clock=lambda: 7_777,
        opener=lambda _target: False,
        terminal_router=lambda target: targets.append(target) is None,
    )

    result = await router.handle(_jump("s1"))

    assert result.effect == "session.jump.terminal_routed"
    assert len(targets) == 1
    assert targets[0].terminal_app == "Ghostty"
    assert targets[0].pane_title == "codex demo"
    assert targets[0].cwd == str(tmp_path)


def test_workspace_candidates_cover_supported_ide_and_agents() -> None:
    assert _workspace_candidates("cursor") == ["Cursor"]
    assert _workspace_candidates("windsurf") == ["Windsurf"]
    assert _workspace_candidates("vscode") == ["Visual Studio Code"]
    assert _workspace_candidates("codex")[0] == "Codex"
    assert _workspace_candidates("claude_code")[0] == "Terminal"
    assert _workspace_candidates("unknown", "cli_agent")[0] == "Terminal"
    assert _workspace_candidates("zed") == ["Zed"]
    assert _workspace_candidates("gemini")[0] == "Terminal"


def test_activation_candidates_cover_supported_ide_and_agents() -> None:
    assert _activation_candidates("cursor") == ["Cursor"]
    assert _activation_candidates("windsurf") == ["Windsurf"]
    assert _activation_candidates("vscode") == ["Visual Studio Code"]
    assert _activation_candidates("xcode") == ["Xcode"]
    assert _activation_candidates("jetbrains")[0] == "IntelliJ IDEA"
    assert _activation_candidates("zed") == ["Zed"]
    assert _activation_candidates("sublime") == ["Sublime Text"]
    assert _activation_candidates("aider")[0] == "Terminal"
    assert _activation_candidates("unknown", "cli_agent")[0] == "Terminal"


async def test_question_answer_updates_waiting_session() -> None:
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            title="Claude question",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = await router.handle(_answer("s1", "Use Cursor"))

    assert result.handled
    assert result.effect == "session.question_answer.accepted"
    got = store.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.RUNNING
    assert got.updated_at_ms == 7_777
    assert got.summary == "User answered: Use Cursor"
    assert got.extras["last_answer"] == "Use Cursor"
    assert got.extras["last_answer_at_ms"] == "7777"


async def test_question_answer_rejects_missing_answer() -> None:
    store = _store_with("s1")
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = await router.handle(_answer("s1", " "))

    assert not result.handled
    assert result.effect == "missing_answer"


async def test_question_answer_unknown_id_is_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = await router.handle(_answer("ghost", "yes"))

    assert result.handled
    assert result.effect == "session.question_answer.unknown_id"


async def test_session_jump_unknown_id_is_still_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store, clock=lambda: 10)
    result = await router.handle(_jump("ghost"))
    assert result.handled  # we own the verb even if the id is missing
    assert result.effect == "session.jump.unknown_id"
    assert result.session_id == "ghost"


async def test_missing_session_id_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.SESSION_JUMP,
        payload={},
    )
    result = await router.handle(action)
    assert not result.handled
    assert result.effect == "missing_session_id"


async def test_wrong_target_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.REMINDER,
        kind=InteractionKind.SESSION_JUMP,
        payload={"session_id": "s1"},
    )
    result = await router.handle(action)
    assert not result.handled
    assert result.effect == "wrong_target"


async def test_unknown_kind_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.PERMISSION_RESOLVE,  # wrong kind for SESSION target
        payload={"session_id": "s1"},
    )
    result = await router.handle(action)
    assert not result.handled
    assert result.effect == "unknown_kind"


# --- R2 close-loop tests ---


async def test_question_answer_phase_mismatch_is_noop() -> None:
    """R2.5: if session phase is not WAITING_FOR_ANSWER, return no-op."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.RUNNING,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = await router.handle(_answer("s1", "hello"))

    assert result.handled
    assert result.effect == "session.question_answer.phase_mismatch"
    assert result.session_id == "s1"
    # Session unchanged
    got = store.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.RUNNING
    assert got.updated_at_ms == 1_000


async def test_question_answer_too_long_is_rejected() -> None:
    """R2.6: answers exceeding 4096 chars are rejected."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    router = SessionInteractionRouter(store, clock=lambda: 7_777)
    long_answer = "x" * 4097

    result = await router.handle(_answer("s1", long_answer))

    assert not result.handled
    assert result.effect == "answer_too_long"
    assert result.session_id == "s1"
    # Session unchanged
    got = store.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.WAITING_FOR_ANSWER


async def test_question_answer_exactly_4096_is_accepted() -> None:
    """R2.6: answers of exactly 4096 chars are accepted."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    router = SessionInteractionRouter(store, clock=lambda: 7_777)
    answer = "x" * 4096

    result = await router.handle(_answer("s1", answer))

    assert result.handled
    assert result.effect == "session.question_answer.accepted"
    got = store.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.RUNNING


async def test_question_answer_emits_dismiss_island() -> None:
    """R2.1: on accepted answer, emit dismiss_island with last_question_surface_id."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            last_question_surface_id="question:s1:1",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    emitted: list[CompanionIntent] = []

    async def _sink(intent: CompanionIntent) -> None:
        emitted.append(intent)

    router = SessionInteractionRouter(store, clock=lambda: 7_777, intent_sink=_sink)

    result = await router.handle(_answer("s1", "yes"))

    assert result.handled
    assert result.effect == "session.question_answer.accepted"
    assert len(emitted) == 1
    assert emitted[0].kind == IntentKind.DISMISS_ISLAND
    assert emitted[0].payload == {"id": "question:s1:1"}


async def test_question_answer_no_dismiss_when_no_surface_id() -> None:
    """R2.1: if last_question_surface_id is None, no dismiss is emitted."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            last_question_surface_id=None,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    emitted: list[CompanionIntent] = []

    async def _sink(intent: CompanionIntent) -> None:
        emitted.append(intent)

    router = SessionInteractionRouter(store, clock=lambda: 7_777, intent_sink=_sink)

    result = await router.handle(_answer("s1", "yes"))

    assert result.handled
    assert result.effect == "session.question_answer.accepted"
    assert len(emitted) == 0


async def test_question_answer_phase_mismatch_emits_no_intents() -> None:
    """R2.5: phase mismatch emits zero CompanionIntents."""
    store = SessionStore()
    store.upsert(
        SessionInfo(
            session_id="s1",
            phase=SessionPhase.COMPLETED,
            last_question_surface_id="question:s1:1",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    emitted: list[CompanionIntent] = []

    async def _sink(intent: CompanionIntent) -> None:
        emitted.append(intent)

    router = SessionInteractionRouter(store, clock=lambda: 7_777, intent_sink=_sink)

    result = await router.handle(_answer("s1", "yes"))

    assert result.handled
    assert result.effect == "session.question_answer.phase_mismatch"
    assert len(emitted) == 0
