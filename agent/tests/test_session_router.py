"""V10 L1-F — session interaction router."""

from __future__ import annotations

from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)
from deskmate_agent.sessions import (
    SessionInfo,
    SessionInteractionRouter,
    SessionPhase,
    SessionState,
    SessionStore,
)
from deskmate_agent.sessions.router import _workspace_candidates


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


def test_session_jump_accepted_touches_existing_session() -> None:
    store = _store_with("s1")
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = router.handle(_jump("s1"))

    assert result.handled
    assert result.effect == "session.jump.accepted"
    assert result.session_id == "s1"
    got = store.get("s1")
    assert got is not None
    assert got.state is SessionState.ACTIVE
    assert got.updated_at_ms == 7_777


def test_session_jump_opens_allowed_url_scheme() -> None:
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

    result = router.handle(_jump("s1"))

    assert result.handled
    assert result.effect == "session.jump.opened"
    assert opened == ["codex://session/s1"]


def test_session_jump_rejects_unallowed_url_and_uses_cwd(tmp_path) -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == [str(tmp_path)]


def test_session_jump_opens_workspace_with_source_app_before_plain_cwd(tmp_path) -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.workspace_opened"
    assert workspaces == [("Cursor", str(tmp_path))]
    assert opened == []


def test_session_jump_workspace_route_falls_back_to_plain_cwd(tmp_path) -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert workspaces == [("Windsurf", str(tmp_path))]
    assert opened == [str(tmp_path)]


def test_session_jump_url_beats_workspace_route(tmp_path) -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.opened"
    assert opened == ["codex://session/s1"]
    assert workspaces == []


def test_session_jump_does_not_open_missing_local_path(tmp_path) -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.accepted"
    assert opened == []


def test_session_jump_activates_known_source_when_no_precise_target() -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.activated"
    assert activated == ["Cursor"]


def test_session_jump_cli_agent_falls_back_to_terminal_activation() -> None:
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

    result = router.handle(_jump("s1"))

    assert result.effect == "session.jump.activated"
    assert activated == ["Terminal"]


def test_workspace_candidates_cover_supported_ide_and_agents() -> None:
    assert _workspace_candidates("cursor") == ["Cursor"]
    assert _workspace_candidates("windsurf") == ["Windsurf"]
    assert _workspace_candidates("vscode") == ["Visual Studio Code"]
    assert _workspace_candidates("codex")[0] == "Codex"
    assert _workspace_candidates("claude_code")[0] == "Terminal"
    assert _workspace_candidates("unknown", "cli_agent")[0] == "Terminal"


def test_question_answer_updates_waiting_session() -> None:
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

    result = router.handle(_answer("s1", "Use Cursor"))

    assert result.handled
    assert result.effect == "session.question_answer.accepted"
    got = store.get("s1")
    assert got is not None
    assert got.phase is SessionPhase.RUNNING
    assert got.updated_at_ms == 7_777
    assert got.summary == "User answered: Use Cursor"
    assert got.extras["last_answer"] == "Use Cursor"
    assert got.extras["last_answer_at_ms"] == "7777"


def test_question_answer_rejects_missing_answer() -> None:
    store = _store_with("s1")
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = router.handle(_answer("s1", " "))

    assert not result.handled
    assert result.effect == "missing_answer"


def test_question_answer_unknown_id_is_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store, clock=lambda: 7_777)

    result = router.handle(_answer("ghost", "yes"))

    assert result.handled
    assert result.effect == "session.question_answer.unknown_id"


def test_session_jump_unknown_id_is_still_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store, clock=lambda: 10)
    result = router.handle(_jump("ghost"))
    assert result.handled  # we own the verb even if the id is missing
    assert result.effect == "session.jump.unknown_id"
    assert result.session_id == "ghost"


def test_missing_session_id_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.SESSION_JUMP,
        payload={},
    )
    result = router.handle(action)
    assert not result.handled
    assert result.effect == "missing_session_id"


def test_wrong_target_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.REMINDER,
        kind=InteractionKind.SESSION_JUMP,
        payload={"session_id": "s1"},
    )
    result = router.handle(action)
    assert not result.handled
    assert result.effect == "wrong_target"


def test_unknown_kind_is_not_handled() -> None:
    store = SessionStore()
    router = SessionInteractionRouter(store)
    action = InteractionAction(
        source=ActionSource.ISLAND,
        target=ActionTarget.SESSION,
        kind=InteractionKind.PERMISSION_RESOLVE,  # wrong kind for SESSION target
        payload={"session_id": "s1"},
    )
    result = router.handle(action)
    assert not result.handled
    assert result.effect == "unknown_kind"
