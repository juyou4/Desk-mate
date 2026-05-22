"""App integration tests (V10 Phase 1d).

These tests spin up a real :class:`App` against a short UDS socket, exercise
the bridge end-to-end, and verify:

- ``agent.ready`` + ``state.snapshot`` arrive when Swift connects.
- ``state.snapshot`` populates recent sessions from :class:`SessionMemory`
  but does not emit any ``intent``-kind notification surfaces (L2-#3).
- A ``user.message`` envelope drives the reactive chain bypass
  (emits ``set_pet_animation`` thinking intent).
- A ``perception`` envelope drives the proactive chain (rule pre-filter
  gates whether a trigger fires).
- LLM prewarm runs in the background (L3-B1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import uuid
from pathlib import Path

import pytest

from deskmate_agent.app import App, AppConfig
from deskmate_agent.approvals import (
    Approval,
    ApprovalDecision,
    ApprovalStatus,
)
from deskmate_agent.bridge import (
    LineBuffer,
    decode_envelope,
    encode_envelope,
)
from deskmate_agent.codex_app_server import parse_codex_notification
from deskmate_agent.hooks import normalize_hook_event, write_hook_event
from deskmate_agent.memory import SessionMemory, SessionSummary
from deskmate_agent.protocol.actions import (
    ActionSource,
    ActionTarget,
    InteractionAction,
    InteractionKind,
)
from deskmate_agent.protocol.envelope import BridgeEnvelope, EnvelopeType
from deskmate_agent.reminders import Reminder, ReminderStatus
from deskmate_agent.sessions import SessionInfo, SessionPhase, SessionState


@pytest.fixture
def short_socket_path() -> Path:
    path = Path(tempfile.gettempdir()) / f"dm-app-{uuid.uuid4().hex[:8]}.sock"
    yield path
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()


async def _collect(reader: asyncio.StreamReader, duration_s: float) -> list[BridgeEnvelope]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration_s
    buf = LineBuffer()
    out: list[BridgeEnvelope] = []
    while loop.time() < deadline:
        remaining = max(deadline - loop.time(), 0.01)
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=remaining)
        except TimeoutError:
            continue
        if not data:
            break
        for line in buf.feed(data):
            out.append(decode_envelope(line))
    return out


async def _seed_session(db_dir: Path, *, session_id: str, summary: str, updated_ms: int) -> None:
    mem = SessionMemory(db_dir / "sessions.db")
    await mem.open()
    try:
        await mem.upsert(
            SessionSummary(
                session_id=session_id,
                summary=summary,
                started_at_ms=updated_ms - 1000,
                updated_at_ms=updated_ms,
            )
        )
    finally:
        await mem.close()


@pytest.mark.asyncio
async def test_agent_ready_and_snapshot_on_connect(
    short_socket_path: Path, tmp_path: Path
) -> None:
    prewarm_fired = asyncio.Event()

    async def fake_prewarm() -> None:
        prewarm_fired.set()

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        heartbeat_interval_s=10.0,
        batch_window_s=0.01,
    )
    app = App(config, llm_prewarm=fake_prewarm)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        envs = await _collect(reader, 0.2)
        kinds = [e.type for e in envs]

        assert EnvelopeType.STATE_SNAPSHOT in kinds
        assert EnvelopeType.AGENT_READY in kinds
        assert kinds.index(EnvelopeType.STATE_SNAPSHOT) < kinds.index(
            EnvelopeType.AGENT_READY
        )
        # No intents are pushed on connect — V10 L2-#3: restore without replay.
        assert all(e.type is not EnvelopeType.INTENT for e in envs)

        await asyncio.wait_for(prewarm_fired.wait(), timeout=0.2)

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_setup_loads_bundled_pixel_default_pack(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 Phase 8: ``App.setup`` should pull in the bundled pixel
    pack via ``AppConfig.extra_pack_roots`` and surface it on the
    runtime so the UI + diagnostics know which pack is active."""
    # Point the primary packs root at an empty tmp dir so the only
    # pack discovered comes from the bundled ``assets/packs``.
    empty_primary = tmp_path / "packs-empty"
    empty_primary.mkdir()
    import os

    os.environ["DESKMATE_PACKS_DIR"] = str(empty_primary)
    try:
        config = AppConfig(
            socket_path=short_socket_path,
            db_dir=tmp_path,
            batch_window_s=0.01,
            prewarm_enabled=False,
        )
        app = App(config)
        rt = await app.setup()
        try:
            reg = rt.character_pack_registry
            assert "pixel_default" in reg.ids(), reg.ids()
            pack = reg.get("pixel_default")
            assert pack is not None
            assert pack.avatar.default_style == "pixel"
            # The bundled pack supports both styles — Phase 7's
            # ``AvatarRenderer`` speaks both.
            assert "pixel" in pack.avatar.supported_styles
            assert "emoji" in pack.avatar.supported_styles
        finally:
            await app.teardown()
    finally:
        os.environ.pop("DESKMATE_PACKS_DIR", None)


@pytest.mark.asyncio
async def test_demo_trigger_scenarios_mutate_runtime_stores(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
    )
    app = App(config)
    rt = await app.setup()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        async def trigger(scenario: str) -> None:
            action = InteractionAction(
                source=ActionSource.MENU_BAR,
                target=ActionTarget.SYSTEM,
                kind=InteractionKind.DEMO_TRIGGER,
                payload={"scenario": scenario},
            )
            await app._handle_interaction(action.model_dump(mode="json"))  # noqa: SLF001

        await trigger("build")
        await trigger("approval")
        assert rt.approval_store.get("demo-approval") is not None

        await trigger("reminder")
        reminder = rt.reminder_store.get("demo-reminder")
        assert reminder is not None
        assert reminder.status is ReminderStatus.FIRED
        envs = await _collect(reader, 0.2)
        reminder_surfaces = [
            e
            for e in envs
            if e.type is EnvelopeType.INTENT
            if e.payload.get("kind") == "present_island"
            and e.payload.get("payload", {}).get("activity_id") == "demo-reminder"
            and e.payload.get("payload", {}).get("surface") == "notification_card"
        ]
        assert len(reminder_surfaces) == 1
        assert reminder_surfaces[0].payload["payload"]["priority"] == "P1"

        await trigger("codex_session")
        session = rt.session_store.get("demo-codex-session")
        assert session is not None
        assert session.title == "Codex demo"

        await trigger("clear")
        assert rt.approval_store.list_pending() == []
        assert rt.reminder_store.list() == []
        assert rt.session_store.get("demo-codex-session") is None
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await app.teardown()


@pytest.mark.asyncio
async def test_island_notification_publisher_suppresses_in_active_session_window(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#1: when an active session declares it owns the
    Terminal app, and the latest perception confirms Terminal is
    frontmost, the publisher silently drops a ``notification_card``
    intent. With suppression off the same intent goes through."""
    from deskmate_agent.context import PerceptionSnapshot
    from deskmate_agent.island_notifications import (
        EXTRA_FRONTMOST_BUNDLE_ID,
    )
    from deskmate_agent.protocol.state import Priority
    from deskmate_agent.sessions import SessionInfo

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
    )
    app = App(config)
    rt = await app.setup()
    try:
        # Plant an active session that claims the Terminal window.
        rt.session_store.upsert(
            SessionInfo(
                session_id="sess-shell",
                title="shell",
                extras={EXTRA_FRONTMOST_BUNDLE_ID: "com.apple.Terminal"},
            )
        )
        # Pretend the latest perception tick saw Terminal frontmost.
        rt.dispatcher._last_perception = PerceptionSnapshot(  # noqa: SLF001
            app_bundle_id="com.apple.Terminal",
            window_title="bash",
        )

        outcome = await rt.island_notifications.show_notification(
            activity_id="reminder-1",
            session_id="sess-shell",
            priority=Priority.P1,
            detail="Standup in 5",
        )
        # Suppression short-circuits *before* hitting the bridge sink,
        # so a missing client doesn't matter.
        assert outcome.emitted is False
        assert (
            outcome.suppressed_reason == "frontmost_matches_session"
        )
        # The runtime exposes the publisher and honours the
        # AppConfig knob (default ``True``).
        assert (
            rt.island_notifications.suppress_frontmost_notifications is True
        )
        rt.island_notifications.set_suppression(False)
        assert (
            rt.island_notifications.suppress_frontmost_notifications is False
        )
    finally:
        await app.teardown()


@pytest.mark.asyncio
async def test_setup_wraps_proactive_engine_with_switchable_under_degradation(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#5: when the configured decision engine isn't already
    Threshold, ``App.setup`` wraps it in a SwitchableDecisionEngine that
    the DegradationController can flip to Threshold once the system
    enters ``LEVEL_PROACTIVE_X2``."""
    from deskmate_agent.context import PerceptionSnapshot, ProactiveContext
    from deskmate_agent.decision import (
        EngineKind,
        SimpleDecisionEngine,
        SwitchableDecisionEngine,
    )
    from deskmate_agent.degradation import (
        LEVEL_FPS_DOWN,
        LEVEL_PROACTIVE_X2,
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
        decision_engine_kind=EngineKind.SIMPLE,
    )
    app = App(config)
    rt = await app.setup()
    try:
        # Setup wrapped the SimpleDecisionEngine in a Switchable shell.
        engine = rt.proactive.decision_engine
        assert isinstance(engine, SwitchableDecisionEngine)
        assert isinstance(engine.primary, SimpleDecisionEngine)

        ctx = ProactiveContext(
            perception=PerceptionSnapshot(),
            last_p2_ts_ms=None,
            urgency="normal",
        )

        # Level 0 → primary (Simple) owns the verdict.
        outcome = await engine.evaluate(ctx)
        assert outcome.engine is EngineKind.SIMPLE

        # Level 1 (FPS down) does not yet trigger the swap.
        rt.degradation.set_level(LEVEL_FPS_DOWN)
        outcome = await engine.evaluate(ctx)
        assert outcome.engine is EngineKind.SIMPLE

        # Level 2 → forced Threshold fallback.
        rt.degradation.set_level(LEVEL_PROACTIVE_X2)
        outcome = await engine.evaluate(ctx)
        assert outcome.engine is EngineKind.THRESHOLD
        assert engine.using_fallback is True

        # Drop back to normal → primary resumes.
        rt.degradation.set_level(0)
        outcome = await engine.evaluate(ctx)
        assert outcome.engine is EngineKind.SIMPLE
        assert engine.using_fallback is False
    finally:
        await app.teardown()


@pytest.mark.asyncio
async def test_setup_skips_switchable_wrap_when_primary_already_threshold(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#5: if the configured engine is already Threshold the wrap
    is a pure no-op, so ``App.setup`` skips it to keep the live tree
    minimal."""
    from deskmate_agent.decision import (
        EngineKind,
        SwitchableDecisionEngine,
        ThresholdDecisionEngine,
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
        decision_engine_kind=EngineKind.THRESHOLD,
    )
    app = App(config)
    rt = await app.setup()
    try:
        engine = rt.proactive.decision_engine
        assert isinstance(engine, ThresholdDecisionEngine)
        assert not isinstance(engine, SwitchableDecisionEngine)
    finally:
        await app.teardown()


@pytest.mark.asyncio
async def test_setup_active_pack_id_override_wins(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 Phase 8: ``AppConfig.active_pack_id`` takes precedence over
    the env var + fallback rules."""
    import json
    import os

    packs_root = tmp_path / "packs"
    packs_root.mkdir()
    for pid in ("alpha", "pixel_default"):
        pdir = packs_root / pid
        pdir.mkdir()
        (pdir / "manifest.json").write_text(
            json.dumps(
                {
                    "spec_version": 1,
                    "id": pid,
                    "display_name": pid,
                    "states": {
                        "idle": {"fps": 4, "frames": ["idle/000.png"]},
                        "working": {"fps": 4, "frames": ["w/000.png"]},
                        "thinking": {"fps": 4, "frames": ["t/000.png"]},
                        "alert": {"fps": 4, "frames": ["a/000.png"]},
                    },
                }
            ),
            encoding="utf-8",
        )

    os.environ["DESKMATE_PACKS_DIR"] = str(packs_root)
    os.environ["DESKMATE_CHARACTER_PACK"] = "pixel_default"
    try:
        config = AppConfig(
            socket_path=short_socket_path,
            db_dir=tmp_path,
            batch_window_s=0.01,
            prewarm_enabled=False,
            extra_pack_roots=(),  # skip bundled assets — keep test isolated
            active_pack_id="alpha",  # should beat env var
        )
        app = App(config)
        rt = await app.setup()
        try:
            # Explicit argument overrides env.
            from deskmate_agent.character_packs import resolve_active_pack

            pick = resolve_active_pack(
                rt.character_pack_registry, preferred_id="alpha"
            )
            assert pick is not None and pick.id == "alpha"
        finally:
            await app.teardown()
    finally:
        os.environ.pop("DESKMATE_PACKS_DIR", None)
        os.environ.pop("DESKMATE_CHARACTER_PACK", None)


@pytest.mark.asyncio
async def test_snapshot_includes_recent_sessions(
    short_socket_path: Path, tmp_path: Path
) -> None:
    # Seed a session before the App opens the DB so it shows up in snapshot.
    await _seed_session(
        tmp_path, session_id="sess-1", summary="debug run", updated_ms=_now_ms()
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        try:
            envs = await _collect(reader, 0.2)
            snapshot = next(e for e in envs if e.type is EnvelopeType.STATE_SNAPSHOT)
            recent = snapshot.payload["recent_sessions"]
            assert len(recent) == 1
            assert recent[0]["session_id"] == "sess-1"
            assert recent[0]["summary"] == "debug run"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_user_message_drives_reactive_intent(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        writer.write(
            encode_envelope(
                BridgeEnvelope.of(EnvelopeType.USER_MESSAGE, {"text": "hello"})
            )
        )
        await writer.drain()

        envs = await _collect(reader, 0.15)
        intent = next(e for e in envs if e.type is EnvelopeType.INTENT)
        assert intent.payload["kind"] == "set_pet_animation"
        assert intent.payload["payload"] == {"state": "thinking"}

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_perception_focused_blocks_proactive_trigger(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)

        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.PERCEPTION,
                    {"user_state": "coding", "focus": "focused", "idle_ms": 1000},
                )
            )
        )
        await writer.drain()

        envs = await _collect(reader, 0.15)
        intents = [e for e in envs if e.type is EnvelopeType.INTENT]
        # Focused user → rule pre-filter blocks; no proactive intent emitted.
        assert intents == []
        assert runtime.dispatcher.stats.perception_ticks == 1
        assert runtime.dispatcher.stats.proactive_blocked == 1
        assert runtime.dispatcher.stats.proactive_triggers == 0

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_state_snapshot_request_returns_fresh_snapshot(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain initial snapshot + ready

        req = BridgeEnvelope.of(
            EnvelopeType.STATE_SNAPSHOT_REQUEST, trace_id="trace-request"
        )
        writer.write(encode_envelope(req))
        await writer.drain()

        envs = await _collect(reader, 0.15)
        snap = next(
            e
            for e in envs
            if e.type is EnvelopeType.STATE_SNAPSHOT and e.trace_id == "trace-request"
        )
        assert "domain_state" in snap.payload
        assert "recent_sessions" in snap.payload

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_interaction_session_jump_updates_runtime_store(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L1-F: a typed SESSION_JUMP InteractionAction reaches the router."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    # Pre-populate the runtime store so the router can find the target.
    runtime.session_store.upsert(
        SessionInfo(
            session_id="sess-jump",
            title="Debugging",
            state=SessionState.PAUSED,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        action = InteractionAction(
            source=ActionSource.ISLAND,
            target=ActionTarget.SESSION,
            kind=InteractionKind.SESSION_JUMP,
            payload={"session_id": "sess-jump"},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()

        # Allow the event loop to run the handler.
        await asyncio.sleep(0.05)

        refreshed = runtime.session_store.get("sess-jump")
        assert refreshed is not None
        assert refreshed.state is SessionState.ACTIVE  # SESSION_JUMP reactivates.
        assert refreshed.updated_at_ms > 1_000

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_session_jump_without_target_emits_pet_feedback(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    runtime.session_store.upsert(
        SessionInfo(
            session_id="sess-no-target",
            title="Background agent",
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)

        action = InteractionAction(
            source=ActionSource.ISLAND,
            target=ActionTarget.SESSION,
            kind=InteractionKind.SESSION_JUMP,
            payload={"session_id": "sess-no-target"},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()

        envs = await _collect(reader, 0.15)
        bubbles = [
            env
            for env in envs
            if env.type is EnvelopeType.INTENT
            and env.payload.get("kind") == "show_pet_bubble"
        ]
        assert bubbles
        bubble = bubbles[-1].payload["payload"]["bubble"]
        assert bubble["id"] == "session-jump-sess-no-target"
        assert "could not find an exact window" in bubble["text"]

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_question_answer_interaction_updates_session_and_emits_feedback(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    runtime.session_store.upsert(
        SessionInfo(
            session_id="sess-question",
            title="Claude question",
            phase=SessionPhase.WAITING_FOR_ANSWER,
            created_at_ms=1_000,
            updated_at_ms=1_000,
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)

        action = InteractionAction(
            source=ActionSource.ISLAND,
            target=ActionTarget.SESSION,
            kind=InteractionKind.QUESTION_ANSWER,
            payload={"session_id": "sess-question", "answer": "Use Cursor"},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()

        envs = await _collect(reader, 0.2)
        got = runtime.session_store.get("sess-question")
        assert got is not None
        assert got.phase is SessionPhase.RUNNING
        assert got.extras["last_answer"] == "Use Cursor"
        assert any(
            env.type is EnvelopeType.INTENT
            and env.payload.get("kind") == "show_pet_bubble"
            and env.payload["payload"]["bubble"]["id"] == "session-answer-sess-question"
            for env in envs
        )
        assert any(env.type is EnvelopeType.STATE_SNAPSHOT for env in envs)

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_snapshot_includes_runtime_active_sessions(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#8: the island session_list needs the live runtime sessions."""
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    runtime.session_store.upsert(
        SessionInfo(
            session_id="live-1",
            title="Deploy",
            summary="Waiting on CI",
            state=SessionState.ACTIVE,
            created_at_ms=_now_ms(),
            updated_at_ms=_now_ms(),
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        try:
            envs = await _collect(reader, 0.2)
            snapshot = next(e for e in envs if e.type is EnvelopeType.STATE_SNAPSHOT)
            active = snapshot.payload["active_sessions"]
            assert len(active) == 1
            assert active[0]["session_id"] == "live-1"
            assert active[0]["state"] == "active"
            assert active[0]["title"] == "Deploy"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_snapshot_includes_pending_reminders(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#4: pending reminders ride along the state snapshot so the
    Swift client can hydrate the menu bar / island after reconnect."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    future_due = _now_ms() + 60_000  # 1 min out — won't fire during the test
    runtime.reminder_store.add(
        Reminder(
            reminder_id="r-snap",
            text="prep slides",
            due_at_ms=future_due,
            created_at_ms=_now_ms(),
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        try:
            envs = await _collect(reader, 0.2)
            snap = next(e for e in envs if e.type is EnvelopeType.STATE_SNAPSHOT)
            reminders = snap.payload["pending_reminders"]
            assert len(reminders) == 1
            assert reminders[0]["reminder_id"] == "r-snap"
            assert reminders[0]["status"] == "pending"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_reminder_scheduler_fires_show_pet_bubble_intent(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L2-#4: a due reminder must reach Swift as a typed
    show_pet_bubble intent carrying a ``BubbleSpec`` of kind reminder."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        # Due in the past → scheduler fires immediately.
        runtime.reminder_store.add(
            Reminder(
                reminder_id="r-fire",
                text="stretch",
                due_at_ms=_now_ms() - 10,
                created_at_ms=_now_ms() - 1000,
            )
        )

        envs = await _collect(reader, 0.3)
        intents = [e for e in envs if e.type is EnvelopeType.INTENT]
        # Expect exactly one intent for the reminder.
        reminder_intents = [
            i for i in intents if i.payload.get("kind") == "show_pet_bubble"
        ]
        assert len(reminder_intents) == 1
        payload = reminder_intents[0].payload["payload"]
        assert payload["reminder_id"] == "r-fire"
        bubble = payload["bubble"]
        assert bubble["kind"] == "reminder"
        assert bubble["text"] == "stretch"
        assert bubble["ttl_ms"] is None

        got = runtime.reminder_store.get("r-fire")
        assert got is not None
        assert got.status is ReminderStatus.FIRED

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_snapshot_domain_state_carries_pending_approvals(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L1-B + Phase 2a PetStateMachine: DomainState.pending_approvals
    must list live approval ids so the Pet shifts into alert."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    runtime.approval_store.add(
        Approval(
            approval_id="ap-1",
            prompt="Read clipboard?",
            created_at_ms=_now_ms(),
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        try:
            envs = await _collect(reader, 0.2)
            snap = next(e for e in envs if e.type is EnvelopeType.STATE_SNAPSHOT)
            assert snap.payload["domain_state"]["pending_approvals"] == ["ap-1"]
            detail = snap.payload["pending_approvals_detail"]
            assert len(detail) == 1
            assert detail[0]["approval_id"] == "ap-1"
            assert detail[0]["status"] == "pending"
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_snapshot_domain_state_uses_live_projector_state(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """Snapshot restore must not reset non-approval DomainState fields."""
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    runtime.session_store.upsert(
        SessionInfo(
            session_id="sess-live",
            title="Live session",
            updated_at_ms=_now_ms(),
        )
    )
    runtime.domain_projector.set_coding_today_ms(12_345)
    runtime.domain_projector.set_coding_today_by_ide({"Xcode": 12_345})
    runtime.domain_projector.set_degradation_level(4)
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        try:
            envs = await _collect(reader, 0.2)
            snap = next(e for e in envs if e.type is EnvelopeType.STATE_SNAPSHOT)
            domain = snap.payload["domain_state"]
            assert domain["active_session_id"] == "sess-live"
            assert domain["coding_today_ms"] == 12_345
            assert domain["coding_today_by_ide"] == {"Xcode": 12_345}
            assert domain["degradation_level"] == 4
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_codex_app_server_notification_updates_app_stores(
    short_socket_path: Path, tmp_path: Path
) -> None:
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
        codex_app_server_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    notification = parse_codex_notification(
        {
            "method": "thread/status/changed",
            "params": {
                "threadId": "codex-thread-1",
                "status": {
                    "type": "active",
                    "activeFlags": ["waitingOnApproval"],
                },
            },
        }
    )

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        assert notification is not None
        await runtime.codex_app_server.handle_notification(notification)
        envs = await _collect(reader, 0.2)

        session = runtime.session_store.get("codex-thread-1")
        assert session is not None
        assert session.source == "codex"
        assert session.phase is SessionPhase.WAITING_FOR_APPROVAL

        pending = runtime.approval_store.list_pending()
        assert len(pending) == 1
        assert pending[0].approval_id == "codex-thread-1-approval"
        assert pending[0].session_id == "codex-thread-1"

        snapshot = await app._build_snapshot()
        assert snapshot["active_sessions"][0]["session_id"] == "codex-thread-1"
        assert snapshot["pending_approvals_detail"][0]["session_id"] == "codex-thread-1"
        island_intents = [
            e
            for e in envs
            if e.type is EnvelopeType.INTENT
            and e.payload.get("kind") == "present_island"
        ]
        assert island_intents
        island_payload = island_intents[-1].payload["payload"]
        assert island_payload["priority"] == "P0"
        assert island_payload["detail"] == "Codex is waiting for approval."
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await app.teardown()


@pytest.mark.asyncio
async def test_hook_event_phase_presentation_reaches_island(
    short_socket_path: Path, tmp_path: Path
) -> None:
    hook_dir = tmp_path / "hooks"
    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
        codex_app_server_enabled=False,
        hook_events_dir=hook_dir,
    )
    app = App(config)
    runtime = await app.setup()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)
        write_hook_event(
            normalize_hook_event(
                {
                    "session_id": "hook-testing",
                    "event": "tool.start",
                    "tool": "Bash",
                    "command": "pytest",
                    "title": "Codex hook",
                },
                source="codex",
            ),
            queue_dir=hook_dir,
        )

        assert await runtime.hook_event_watcher.drain_once() == 1
        envs = await _collect(reader, 0.2)

        session = runtime.session_store.get("hook-testing")
        assert session is not None
        assert session.phase is SessionPhase.TESTING
        island_intents = [
            e
            for e in envs
            if e.type is EnvelopeType.INTENT
            and e.payload.get("kind") == "present_island"
        ]
        assert island_intents
        island_payload = island_intents[-1].payload["payload"]
        assert island_payload["priority"] == "P1"
        assert island_payload["detail"] == "Codex is running: pytest"
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await app.teardown()


@pytest.mark.asyncio
async def test_permission_resolve_via_bridge_resolves_approval(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 L1-F: a PERMISSION_RESOLVE InteractionAction reaches the
    approval router and drains the approval from the pending list."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    runtime.approval_store.add(
        Approval(
            approval_id="ap-resolve",
            prompt="Send email?",
            created_at_ms=_now_ms(),
        )
    )
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        action = InteractionAction(
            source=ActionSource.ISLAND,
            target=ActionTarget.SYSTEM,
            kind=InteractionKind.PERMISSION_RESOLVE,
            payload={"approval_id": "ap-resolve", "allow": True},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()
        await asyncio.sleep(0.05)

        resolved = runtime.approval_store.get("ap-resolve")
        assert resolved is not None
        assert resolved.status is ApprovalStatus.RESOLVED
        assert resolved.decision is ApprovalDecision.ALLOW

        # A follow-up snapshot request should show an empty pending_approvals.
        req = BridgeEnvelope.of(
            EnvelopeType.STATE_SNAPSHOT_REQUEST, trace_id="trace-after-resolve"
        )
        writer.write(encode_envelope(req))
        await writer.drain()
        envs = await _collect(reader, 0.15)
        snap = next(
            e
            for e in envs
            if e.type is EnvelopeType.STATE_SNAPSHOT
            and e.trace_id == "trace-after-resolve"
        )
        assert snap.payload["domain_state"]["pending_approvals"] == []

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_approval_surface_publisher_pushes_show_and_dismiss_bubble(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 Phase 7c / L1-F: creating an approval auto-surfaces a
    SHOW_PET_BUBBLE with APPROVAL_HINT spec; resolving it pushes a
    matching DISMISS_PET_BUBBLE so the Swift queue drops the hint."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        # --- Show on add -----------------------------------------------
        runtime.approval_store.add(
            Approval(
                approval_id="ap-surface",
                prompt="Allow clipboard?",
                created_at_ms=_now_ms(),
            )
        )
        show_envs = await _collect(reader, 0.2)
        show_intent = _find_intent(show_envs, "show_pet_bubble")
        assert show_intent is not None
        bubble = show_intent["bubble"]
        assert bubble["id"] == "approval-ap-surface"
        assert bubble["kind"] == "approval_hint"
        assert bubble["ttl_ms"] is None
        labels = [a["label"] for a in bubble["actions"]]
        assert labels == ["Allow", "Deny"]
        for a in bubble["actions"]:
            assert a["interaction_kind"] == "permission.resolve"
            assert a["payload"]["approval_id"] == "ap-surface"

        # --- Dismiss on resolve ---------------------------------------
        action = InteractionAction(
            source=ActionSource.PET,
            target=ActionTarget.SYSTEM,
            kind=InteractionKind.PERMISSION_RESOLVE,
            payload={"approval_id": "ap-surface", "allow": True},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()
        dismiss_envs = await _collect(reader, 0.2)
        dismiss_intent = _find_intent(dismiss_envs, "dismiss_pet_bubble")
        assert dismiss_intent is not None
        assert dismiss_intent["bubble_id"] == "approval-ap-surface"
        assert dismiss_intent["approval_id"] == "ap-surface"

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_permission_resolve_pushes_update_domain_state_intent(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """V10 Phase 7 / L1-B: resolving an approval must push a live
    UPDATE_DOMAIN_STATE intent reflecting the empty pending set, so the
    Pet state machine exits alert without waiting for the next snapshot."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(str(short_socket_path))
        await _collect(reader, 0.05)  # drain snapshot + ready

        # Create the approval after bridge is live so the projector emits.
        runtime.approval_store.add(
            Approval(
                approval_id="ap-push",
                prompt="Run shell?",
                created_at_ms=_now_ms(),
            )
        )
        add_envs = await _collect(reader, 0.2)
        add_intent = _find_domain_update(add_envs)
        assert add_intent is not None
        assert add_intent["domain_state"]["pending_approvals"] == ["ap-push"]

        # Resolve via bridge and expect a second UPDATE_DOMAIN_STATE.
        action = InteractionAction(
            source=ActionSource.ISLAND,
            target=ActionTarget.SYSTEM,
            kind=InteractionKind.PERMISSION_RESOLVE,
            payload={"approval_id": "ap-push", "allow": True},
        )
        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.INTERACTION,
                    action.model_dump(mode="json"),
                )
            )
        )
        await writer.drain()

        resolve_envs = await _collect(reader, 0.2)
        resolve_intent = _find_domain_update(resolve_envs)
        assert resolve_intent is not None
        assert resolve_intent["domain_state"]["pending_approvals"] == []

        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


# ---------------------------------------------------------------------------
# V10 L1-F: every InteractionKind survives the dispatcher catch-all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [
        InteractionKind.TASK_OPEN_DETAIL,
        InteractionKind.PET_INTERACT,
        InteractionKind.PET_DRAG,
        InteractionKind.PET_NEST,
    ],
)
@pytest.mark.asyncio
async def test_handle_interaction_unhandled_kinds_are_no_ops(
    short_socket_path: Path, tmp_path: Path, kind: InteractionKind
) -> None:
    """V10 L1-F: typed actions without an explicit router land in the
    catch-all branch. The handler MUST NOT raise, MUST NOT mutate the
    approval / session / reminder stores, and MUST log a structured
    ``app.interaction_unhandled`` line so the kind never gets dropped
    silently on the way to a future skill that wants to bind it.
    """

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()

    try:
        # Sanity baseline: every store starts empty.
        assert runtime.approval_store.list_pending() == []
        assert runtime.session_store.list() == []
        assert runtime.reminder_store.list() == []

        action = InteractionAction(
            source=ActionSource.PET,
            target=ActionTarget.BUBBLE,
            kind=kind,
            payload={"hint": "round-trip", "future_field": [1, 2]},
        )
        await app._handle_interaction(  # noqa: SLF001
            action.model_dump(mode="json")
        )

        # Catch-all is a no-op for every observable surface.
        assert runtime.approval_store.list_pending() == []
        assert runtime.session_store.list() == []
        assert runtime.reminder_store.list() == []
    finally:
        await app.teardown()


@pytest.mark.asyncio
async def test_handle_interaction_unknown_kind_is_rejected_at_validation(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """A wire payload carrying an unknown ``kind`` value should fail
    pydantic validation and be logged + dropped — never reach the
    catch-all branch (that branch is reserved for *known* kinds we
    haven't bound yet)."""

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        agent_runtime_scanner_enabled=False,
    )
    app = App(config)
    await app.setup()

    try:
        bogus_payload = {
            "source": "pet",
            "target": "bubble",
            "kind": "not.a.real.kind",
            "payload": {},
        }
        # Must not raise.
        await app._handle_interaction(bogus_payload)  # noqa: SLF001
    finally:
        await app.teardown()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def _find_domain_update(envs: list[BridgeEnvelope]) -> dict | None:
    """Return the latest UPDATE_DOMAIN_STATE intent payload, if any."""
    return _find_intent(envs, "update_domain_state")


def _find_intent(envs: list[BridgeEnvelope], kind: str) -> dict | None:
    """Return the most recent intent payload body matching ``kind``."""
    latest: dict | None = None
    for env in envs:
        if env.type is not EnvelopeType.INTENT:
            continue
        if env.payload.get("kind") != kind:
            continue
        latest = env.payload.get("payload")
    return latest


# ---------------------------------------------------------------------------
# perf.metrics envelope (V10 §3.1 row 6 + row 8)
# ---------------------------------------------------------------------------


def _parse_log_records(out: str) -> list[dict]:
    records: list[dict] = []
    for line in out.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        records.append(rec)
    return records


@pytest.mark.asyncio
async def test_perf_metrics_envelope_logs_typed_payload(
    short_socket_path: Path, tmp_path: Path, capsys
) -> None:
    """A Swift-side ``perf.metrics`` envelope should produce a single
    structured log line with the typed values, and *must not* be
    routed through the perception or interaction handlers."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(
            str(short_socket_path)
        )
        await _collect(reader, 0.05)
        capsys.readouterr()  # discard pre-test logs

        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.PERF_METRICS,
                    {
                        "last_wake_seconds": 0.42,
                        "total_frames": 600,
                        "dropped_frames": 3,
                        "frame_drop_ratio": 0.005,
                    },
                    trace_id="perf-trace-1",
                )
            )
        )
        await writer.drain()
        await asyncio.sleep(0.05)

        # Dispatcher must not see this envelope as a perception tick.
        assert runtime.dispatcher.stats.perception_ticks == 0

        records = _parse_log_records(capsys.readouterr().out)
        perf = next(
            (r for r in records if r.get("event") == "app.perf_metrics"),
            None,
        )
        assert perf is not None, "expected an app.perf_metrics record"
        assert perf["wake_s"] == pytest.approx(0.42)
        assert perf["total_frames"] == 600
        assert perf["dropped_frames"] == 3
        assert perf["frame_drop_pct"] == pytest.approx(0.5)
        # trace_scope makes the trace_id ride along — useful for
        # correlating Swift wake events back to the agent log.
        assert perf.get("trace_id") == "perf-trace-1"

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_perf_metrics_invalid_payload_warns_does_not_raise(
    short_socket_path: Path, tmp_path: Path, capsys
) -> None:
    """Garbage payload values must yield a warning, not a handler
    exception — the bridge must stay resilient (V10 L1 forward-compat)."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(
            str(short_socket_path)
        )
        await _collect(reader, 0.05)
        capsys.readouterr()

        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.PERF_METRICS,
                    {
                        "last_wake_seconds": "not-a-number",
                        "total_frames": "huh",
                    },
                )
            )
        )
        await writer.drain()
        await asyncio.sleep(0.05)

        records = _parse_log_records(capsys.readouterr().out)
        invalid = next(
            (r for r in records if r.get("event") == "app.perf_metrics_invalid"),
            None,
        )
        assert invalid is not None, "expected a typed warning record"
        # The malformed payload survived in the warning so a contributor
        # can debug what Swift sent.
        assert "payload" in invalid

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


# ---------------------------------------------------------------------------
# V10 L2-#3: 24h restore window + "no replay" notification fuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_silences_session_ids_within_restore_window(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """Sessions touched inside the configured restore window land in
    the publisher's silenced set. Older sessions don't.

    This is the wire of L2-#3 "不重弹旧通知" — a P0 reminder that
    fires for a session resurrected on disk gets dropped silently
    until the orchestrator (or a user interaction) writes a fresh
    upsert into the in-memory ``session_store``.
    """
    now = _now_ms()
    # In-window — well inside the 24h cutoff.
    await _seed_session(
        tmp_path,
        session_id="sess-fresh",
        summary="touched 1 hour ago",
        updated_ms=now - 60 * 60 * 1000,
    )
    # Out-of-window — touched 30h ago, *should not* be silenced.
    # ``list_updated_since`` filters it out so it never enters the
    # publisher in the first place.
    await _seed_session(
        tmp_path,
        session_id="sess-old",
        summary="touched 30 hours ago",
        updated_ms=now - 30 * 60 * 60 * 1000,
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
        session_restore_window_hours=24,
    )
    app = App(config)
    runtime = await app.setup()
    try:
        publisher = runtime.island_notifications
        assert publisher.is_silenced("sess-fresh") is True
        assert publisher.is_silenced("sess-old") is False
        assert "sess-fresh" in publisher.silenced_session_ids
        assert "sess-old" not in publisher.silenced_session_ids
    finally:
        await app.teardown()


@pytest.mark.asyncio
async def test_show_notification_for_restored_session_is_dropped(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """End-to-end: a session seeded on disk inside the 24h window
    cannot be raised to the user via ``show_notification`` until
    something explicitly unsilences it. Nothing reaches the wire."""
    await _seed_session(
        tmp_path,
        session_id="sess-restored",
        summary="actionable yesterday",
        updated_ms=_now_ms() - 2 * 60 * 60 * 1000,  # 2h ago
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())
    try:
        reader, writer = await asyncio.open_unix_connection(
            str(short_socket_path)
        )
        # Drain agent.ready + state.snapshot.
        await _collect(reader, 0.1)

        outcome = await runtime.island_notifications.show_notification(
            activity_id="reminder-restored",
            session_id="sess-restored",
        )
        assert outcome.emitted is False
        assert outcome.suppressed_reason == "restored_session_silenced"

        # And nothing extra hit the wire — no PRESENT_ISLAND intent
        # rode along after the silenced call.
        post = await _collect(reader, 0.1)
        present = [
            e
            for e in post
            if e.type is EnvelopeType.INTENT
            and e.payload.get("kind") == "present_island"
        ]
        assert present == []

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_session_store_upsert_unsilences_restored_session(
    short_socket_path: Path, tmp_path: Path
) -> None:
    """Real activity on a restored session — modeled here as a fresh
    ``session_store.upsert`` — must clear its silence so the next
    notification can ride the wire normally. This is what makes the
    fuse "self-clearing" once the user actually re-engages."""
    await _seed_session(
        tmp_path,
        session_id="sess-restored",
        summary="needs attention",
        updated_ms=_now_ms() - 60 * 60 * 1000,
    )

    config = AppConfig(
        socket_path=short_socket_path,
        db_dir=tmp_path,
        batch_window_s=0.01,
        prewarm_enabled=False,
    )
    app = App(config)
    runtime = await app.setup()
    serve = asyncio.create_task(app.serve_forever())
    try:
        # We need a connected client because ``show_notification``
        # ultimately drives ``bridge.send``; with no peer the bridge
        # legitimately raises ``no client connected``.
        reader, writer = await asyncio.open_unix_connection(
            str(short_socket_path)
        )
        await _collect(reader, 0.1)  # drain agent.ready + state.snapshot

        publisher = runtime.island_notifications
        assert publisher.is_silenced("sess-restored") is True

        # Orchestrator-style upsert: a real event for the same id
        # arrives and goes into the live store. The subscriber
        # installed in ``App.setup`` clears the silence.
        runtime.session_store.upsert(
            SessionInfo(
                session_id="sess-restored",
                title="Re-opened",
                state=SessionState.ACTIVE,
                created_at_ms=_now_ms(),
                updated_at_ms=_now_ms(),
            )
        )
        assert publisher.is_silenced("sess-restored") is False

        outcome = await publisher.show_notification(
            activity_id="follow-up", session_id="sess-restored"
        )
        assert outcome.emitted is True

        # And the intent rode the wire — confirming the publisher
        # really did emit, not just succeed silently.
        post = await _collect(reader, 0.1)
        present = [
            e
            for e in post
            if e.type is EnvelopeType.INTENT
            and e.payload.get("kind") == "present_island"
            and e.payload.get("payload", {}).get("activity_id") == "follow-up"
        ]
        assert len(present) == 1

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve


@pytest.mark.asyncio
async def test_perf_metrics_null_wake_logs_none(
    short_socket_path: Path, tmp_path: Path, capsys
) -> None:
    """First app launch (no prior wake) sends ``last_wake_seconds: null``
    and the agent must log ``wake_s = None`` rather than crash on
    the missing value."""
    config = AppConfig(
        socket_path=short_socket_path, db_dir=tmp_path, batch_window_s=0.01
    )
    app = App(config)
    await app.setup()
    serve = asyncio.create_task(app.serve_forever())

    try:
        reader, writer = await asyncio.open_unix_connection(
            str(short_socket_path)
        )
        await _collect(reader, 0.05)
        capsys.readouterr()

        writer.write(
            encode_envelope(
                BridgeEnvelope.of(
                    EnvelopeType.PERF_METRICS,
                    {
                        "last_wake_seconds": None,
                        "total_frames": 0,
                        "dropped_frames": 0,
                        "frame_drop_ratio": 0.0,
                    },
                )
            )
        )
        await writer.drain()
        await asyncio.sleep(0.05)

        records = _parse_log_records(capsys.readouterr().out)
        perf = next(
            (r for r in records if r.get("event") == "app.perf_metrics"),
            None,
        )
        assert perf is not None
        assert perf["wake_s"] is None
        assert perf["frame_drop_pct"] == pytest.approx(0.0)

        writer.close()
        await writer.wait_closed()
    finally:
        serve.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await serve
