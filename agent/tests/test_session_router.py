"""V10 L1-F — session interaction router."""

from __future__ import annotations

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


def test_workspace_candidates_cover_supported_ide_and_agents() -> None:
    assert _workspace_candidates("cursor") == ["Cursor"]
    assert _workspace_candidates("windsurf") == ["Windsurf"]
    assert _workspace_candidates("vscode") == ["Visual Studio Code"]
    assert _workspace_candidates("codex")[0] == "Codex"
    assert _workspace_candidates("claude_code")[0] == "Terminal"
    assert _workspace_candidates("unknown", "cli_agent")[0] == "Terminal"


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
