"""Top-level App that wires bridge + dispatcher + memory (V10 Phase 1d).

Startup sequence follows V10 L3-B11 (delayed start) and L2-#3 (restore
recent sessions without replaying old notifications):

  1. Open memory stores (Session + Profile).
  2. Build the ProactiveEngine + Dispatcher (proactive chain only gates the
     perception path, per L2-#6).
  3. Start the bridge listener.
  4. When Swift connects, hand it a ``state.snapshot`` (recent sessions +
     :class:`DomainState`) *then* ``agent.ready``. Swift queues user input
     until it sees ``agent.ready``.

This file intentionally ships *plumbing only*. The real Orchestrator /
LLM prompt composition / skill dispatch arrive later (Phase 10+); here we
just prove the wiring is correct and exercisable end-to-end.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agent_events import (
    AgentEvent,
    AgentEventReducer,
    PermissionRequested,
    QuestionAsked,
    SessionActivityUpdated,
    SessionCompleted,
    SessionStarted,
)
from .agent_phase import presentation_for_phase
from .agent_runtime import AgentRuntimeScanner, AgentRuntimeStore
from .approvals import Approval, ApprovalRouter, ApprovalStore, ApprovalSurfacePublisher
from .bridge import BridgeServer
from .character_packs import (
    CharacterPackRegistry,
    resolve_active_pack,
)
from .character_packs import (
    build_default_registry as build_default_pack_registry,
)
from .claude_transcripts import ClaudeTranscriptWatcher
from .codex_app_server import CodexAppServerCoordinator
from .context import PerceptionSnapshot, ProactiveContext
from .decision import (
    EngineKind,
    SwitchableDecisionEngine,
    ThresholdDecisionEngine,
    make_decision_engine,
)
from .degradation import DegradationController
from .dispatcher import Dispatcher
from .hooks import (
    HookEvent,
    HookEventConsumer,
    HookEventWatcher,
    default_hook_events_dir,
)
from .intent_logger import IntentLogger
from .island_notifications import IslandNotificationPublisher
from .logging_setup import get_logger, trace_scope
from .memory import CodingSessionStore, ProfileStore, SessionMemory
from .perception_deduper import PerceptionDeduper
from .proactive import NudgeSelector, ProactiveEngine
from .projector import DomainStateProjector
from .projects import ProjectRegistry
from .protocol.actions import InteractionAction, InteractionKind
from .protocol.envelope import BridgeEnvelope, EnvelopeType
from .protocol.intents import CompanionIntent, IntentKind
from .protocol.state import BubbleKind, BubbleSpec, Priority, UserFocus
from .reminders import Reminder, ReminderScheduler, ReminderStore
from .sessions import (
    SessionInfo,
    SessionInteractionRouter,
    SessionPhase,
    SessionStore,
)
from .skills import (
    BuildStatusSkill,
    BuildStatusWatcher,
    CodingSessionTracker,
    SkillRegistry,
    make_default_composer,
    populate_default_registry,
)

_LOG = get_logger("deskmate_agent.app")


def _local_tz_offset_s() -> int:
    """Return the local timezone offset from UTC in seconds.

    Used by the Phase 15-i daily rollup so a user on UTC+8 sees the
    boundary at their local midnight rather than UTC's.
    """
    import time as _time

    # ``altzone`` handles DST on the local side; ``timezone`` is the
    # non-DST baseline. Both are seconds *west* of UTC, so we negate.
    if _time.daylight and _time.localtime().tm_isdst > 0:
        return -_time.altzone
    return -_time.timezone


def _phase_for_agent_event(event: AgentEvent) -> SessionPhase:
    if isinstance(event, PermissionRequested):
        return SessionPhase.WAITING_FOR_APPROVAL
    if isinstance(event, QuestionAsked):
        return SessionPhase.WAITING_FOR_ANSWER
    if isinstance(event, SessionCompleted):
        return SessionPhase.FAILED if event.failed else SessionPhase.COMPLETED
    if isinstance(event, (SessionActivityUpdated, SessionStarted)):
        return event.phase
    return SessionPhase.RUNNING


def _default_bundled_packs_dir() -> Path:
    """Return the bundled ``assets/packs`` directory shipped next to
    the agent source tree. Serves as the last-resort fallback when
    the user hasn't dropped any packs into ``~/.deskmate/packs``.
    """
    return Path(__file__).resolve().parents[2] / "assets" / "packs"


@dataclass
class AppConfig:
    socket_path: Path
    db_dir: Path
    decision_engine_kind: EngineKind = EngineKind.AUTO
    session_restore_window_hours: int = 24
    heartbeat_interval_s: float = 30.0
    batch_window_s: float = 0.05
    prewarm_enabled: bool = True
    #: Extra character-pack roots scanned in addition to
    #: ``~/.deskmate/packs``. Defaults to the bundled ``assets/packs``
    #: directory so a freshly-installed agent always has at least the
    #: built-in pixel pack available.
    extra_pack_roots: tuple[Path, ...] = field(
        default_factory=lambda: (_default_bundled_packs_dir(),)
    )
    #: Optional override for the active pack id. ``None`` defers to
    #: ``DESKMATE_CHARACTER_PACK`` / the registry fallback chain.
    active_pack_id: str | None = None
    #: V10 L2-#1: when ``True`` (default), the
    #: :class:`IslandNotificationPublisher` drops ``notification_card``
    #: PRESENT_ISLAND intents whose target session is the one whose
    #: window the user is already focused on. Set to ``False`` to opt
    #: every session into receiving every notification regardless of
    #: focus (useful for debugging / accessibility).
    suppress_frontmost_notifications: bool = True
    #: Directory queue consumed by the hook watcher. Defaults to
    #: ``$DESKMATE_HOOK_EVENTS_DIR`` or ``~/.deskmate/hook-events``.
    hook_events_dir: Path = field(default_factory=default_hook_events_dir)
    #: Passive IDE / agent process scanning. Production keeps this on;
    #: tests can disable it to avoid host-machine processes leaking into
    #: deterministic snapshots.
    agent_runtime_scanner_enabled: bool = True
    #: Codex.app app-server integration. ``python -m deskmate_agent`` enables
    #: this explicitly; tests and embedded AppConfig instances stay off unless
    #: requested.
    codex_app_server_enabled: bool = False


LLMPrewarmFn = Callable[[], Awaitable[None]]


async def _noop_prewarm() -> None:
    """Default prewarm hook — Phase 10 swaps in the real LLM probe."""
    await asyncio.sleep(0)


@dataclass
class AppRuntime:
    """Exposes the live sub-components for tests / diagnostics."""

    session_memory: SessionMemory
    coding_session_store: CodingSessionStore
    session_store: SessionStore
    session_router: SessionInteractionRouter
    reminder_store: ReminderStore
    reminder_scheduler: ReminderScheduler
    approval_store: ApprovalStore
    approval_router: ApprovalRouter
    approval_surface: ApprovalSurfacePublisher
    domain_projector: DomainStateProjector
    profile: ProfileStore
    bridge: BridgeServer
    dispatcher: Dispatcher
    proactive: ProactiveEngine
    build_status: BuildStatusSkill
    build_status_watcher: BuildStatusWatcher
    agent_runtime_store: AgentRuntimeStore
    agent_runtime_scanner: AgentRuntimeScanner
    codex_app_server: CodexAppServerCoordinator
    claude_transcript_watcher: ClaudeTranscriptWatcher
    hook_event_watcher: HookEventWatcher
    project_registry: ProjectRegistry
    degradation: DegradationController
    skill_registry: SkillRegistry
    character_pack_registry: CharacterPackRegistry
    island_notifications: IslandNotificationPublisher
    prewarm_started: asyncio.Event = field(default_factory=asyncio.Event)
    agent_ready_sent: asyncio.Event = field(default_factory=asyncio.Event)


class App:
    """Deskmate agent core — bridge owner, dispatcher owner, memory owner."""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm_prewarm: LLMPrewarmFn | None = None,
    ) -> None:
        self.config = config
        self._llm_prewarm = llm_prewarm or _noop_prewarm
        self._runtime: AppRuntime | None = None
        self._bg_tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    async def setup(self) -> AppRuntime:
        cfg = self.config
        cfg.db_dir.mkdir(parents=True, exist_ok=True)

        session_mem = SessionMemory(cfg.db_dir / "sessions.db")
        await session_mem.open()

        # Phase 15-i: the coding-session log lives in the same SQLite
        # file as session summaries so users only have one place to
        # back up. Different table, same connection file.
        coding_session_store = CodingSessionStore(
            cfg.db_dir / "sessions.db"
        )
        await coding_session_store.open()

        profile = ProfileStore(cfg.db_dir / "profile.db")
        await profile.open()

        # Phase 9 · §4: build the degradation controller early so
        # every downstream component (cooldown, projector, future
        # Swift mirror) can wire through it at construction time.
        degradation = DegradationController()

        from .proactive.cooldown import CooldownTracker

        proactive_cooldown = CooldownTracker(
            interval_multiplier_provider=degradation.proactive_interval_multiplier,
        )
        # V10 L2-#5: wrap the configured engine in a switchable shell so
        # that whenever degradation enters ``LEVEL_PROACTIVE_X2`` the
        # proactive chain stops calling the (potentially expensive) AI
        # probe and routes through the cheap threshold engine instead.
        # When the level falls back below 2 the primary engine is
        # restored on the next ``evaluate``.
        primary_decision_engine = make_decision_engine(cfg.decision_engine_kind)
        proactive_decision_engine = primary_decision_engine
        if not isinstance(primary_decision_engine, ThresholdDecisionEngine):
            proactive_decision_engine = SwitchableDecisionEngine(
                primary_decision_engine,
                ThresholdDecisionEngine(),
                should_use_fallback=degradation.force_threshold_engine,
            )
        proactive = ProactiveEngine(
            decision_engine=proactive_decision_engine,
            cooldown=proactive_cooldown,
        )

        bridge = BridgeServer(
            cfg.socket_path,
            on_envelope=self._on_envelope,
            on_connect=self._on_client_connect,
            on_disconnect=self._on_client_disconnect,
            heartbeat_interval_s=cfg.heartbeat_interval_s,
            batch_window_s=cfg.batch_window_s,
        )
        await bridge.start()

        async def _bridge_sink(intent: CompanionIntent) -> None:
            # V10 L1-C: Python never hands UI instructions to Swift; only
            # intents cross the boundary.
            await bridge.send(
                BridgeEnvelope.of(
                    EnvelopeType.INTENT,
                    {"kind": intent.kind.value, "payload": dict(intent.payload)},
                )
            )

        # Phase 14-iv: wrap the bridge sink in an :class:`IntentLogger`
        # so every outgoing intent lands as a JSON line in the
        # Application Support folder. ``deskmate tail-status`` streams
        # that file so contributors + CI can watch behaviour without
        # hijacking the bridge socket.
        intent_sink: Callable[[CompanionIntent], Awaitable[None]] = IntentLogger(
            path=cfg.db_dir / "intents.jsonl",
            inner=_bridge_sink,
        )

        # Phase 13-v: 1s dwell keeps cmd-tab flicker from flashing the
        # pill; 2s grace lets brief Messages / Finder detours stay
        # connected to the session. Phase 13-iv keeps the duration
        # readout enabled (default ``show_duration=True``).
        # Phase 15-i: persist each finished session into
        # ``coding_session_store`` so the menu bar can surface
        # today-so-far rollups + historical browsing. The projector
        # reference is filled in right after it's constructed so
        # this closure can poke it on every session close.
        domain_projector: DomainStateProjector | None = None

        async def _record_session(
            ide: str, started_at_ms: int, ended_at_ms: int
        ) -> None:
            try:
                await coding_session_store.record(
                    ide=ide,
                    started_at_ms=started_at_ms,
                    ended_at_ms=ended_at_ms,
                )
            except Exception as exc:  # noqa: BLE001 — fail-soft
                _LOG.warning(
                    "app.coding_session_record_failed",
                    ide=ide,
                    error=str(exc),
                )
                return
            # Update the daily rollup so the menu bar stays honest.
            try:
                tz = _local_tz_offset_s()
                today_ms = await coding_session_store.today_duration_ms(
                    tz_offset_s=tz
                )
                by_ide = await coding_session_store.today_duration_by_ide(
                    tz_offset_s=tz
                )
                if domain_projector is not None:
                    domain_projector.set_coding_today_ms(today_ms)
                    domain_projector.set_coding_today_by_ide(by_ide)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning(
                    "app.coding_today_rollup_failed", error=str(exc)
                )

        # Phase 15-ii: load the registered projects (if any) so the
        # tracker can stamp branch info onto each island detail.
        project_registry = ProjectRegistry(
            path=cfg.db_dir / "projects.json"
        )
        project_registry.load()

        def _resolve_project(
            bundle_id: str | None, window_title: str | None
        ):
            return project_registry.resolve(
                bundle_id=bundle_id, window_title=window_title
            )

        coding_tracker = CodingSessionTracker(
            intent_sink,
            dwell_ms=1_000,
            grace_ms=2_000,
            on_session_end=_record_session,
            project_resolver=_resolve_project,
        )

        # Phase 14-i: build/test status island. The skill is pure
        # (just knows how to emit island intents); the watcher is the
        # only I/O concern — it polls ``~/.deskmate/build-status.json``
        # so any tool (Makefile, pytest hook, CI script, …) can light
        # up the pill with a single ``deskmate build-*`` invocation.
        build_status = BuildStatusSkill(intent_sink)
        build_status_watcher = BuildStatusWatcher(build_status)
        # Phase 8: scan ~/.deskmate/packs (+ bundled assets/packs) so
        # the agent has an answer when Swift asks "what avatar style
        # should I render?". Best-effort — a missing root simply
        # yields an empty registry; the Swift pet overlay keeps its
        # current env-var fallback.
        character_pack_registry = build_default_pack_registry(
            extra_roots=cfg.extra_pack_roots
        )
        active_pack = resolve_active_pack(
            character_pack_registry, preferred_id=cfg.active_pack_id
        )
        if active_pack is not None:
            _LOG.info(
                "app.character_pack_selected",
                pack_id=active_pack.id,
                avatar_style=active_pack.avatar.default_style,
            )
        else:
            _LOG.info("app.character_pack_absent")

        # Phase 10: resident metadata catalog + on-demand body loaders.
        # The LLM composer (only path that reads it) consults the
        # registry on every user turn to inject matching system prompt
        # fragments. Built once at setup so Phase 12-ii's composer
        # picks it up.
        skill_registry = populate_default_registry(SkillRegistry())
        # V10 Phase 9 · §4 step 3: a deduper that coalesces
        # near-duplicate perception ticks. The widening factor reads
        # straight from the degradation controller so once the system
        # enters ``LEVEL_PERCEPTION_WIDE`` the gap doubles and CPU
        # usage on the perception path falls in lockstep.
        perception_deduper = PerceptionDeduper(
            widening_factor_provider=degradation.perception_widening_factor,
        )
        dispatcher = Dispatcher(
            proactive=proactive,
            intent_sink=intent_sink,
            # Phase 12-ii: ``make_default_composer`` picks the LLM
            # composer when ``DESKMATE_LLM_API_KEY`` is set and falls
            # back to the canned composer otherwise. The dispatcher /
            # intent sink / bridge / Swift UI are all unchanged across
            # both modes — the skill seam is a single async callable.
            reply_composer=make_default_composer(
                skill_registry=skill_registry
            ),
            # Phase 13-i: observe perception ticks so the island
            # reflects what the user is actually doing (coding in
            # Xcode / VSCode / Cursor / …). Additional observers plug
            # in as a flat list; order defines emission order on the
            # wire for any given tick.
            perception_observers=[coding_tracker.as_observer()],
            # Phase 16-i: the proactive chain's first real voice.
            # Default pool of idle nudges rotates deterministically;
            # a future patch can swap in an LLM-backed composer
            # without touching the dispatcher.
            nudge_selector=NudgeSelector(),
            perception_deduper=perception_deduper,
        )

        session_store = SessionStore()
        session_router = SessionInteractionRouter(session_store)

        reminder_store = ReminderStore()
        reminder_scheduler = ReminderScheduler(reminder_store, intent_sink)

        approval_store = ApprovalStore()
        approval_router = ApprovalRouter(approval_store)
        approval_surface = ApprovalSurfacePublisher(approval_store, intent_sink)

        domain_projector = DomainStateProjector(
            approval_store=approval_store,
            session_store=session_store,
            intent_sink=intent_sink,
        )
        # Phase 9 · §4: any controller-level change shows up in the
        # next ``UPDATE_DOMAIN_STATE`` intent so Swift can apply its
        # FPS / HUD / orderOut policies in lock-step with Python's.
        degradation.subscribe(domain_projector.set_degradation_level)
        # Seed the current value so the projector starts at the
        # right number (avoids a "null → 0" update later).
        domain_projector.set_degradation_level(degradation.level)
        # Phase 15-i: seed the projector with today's rollup on start
        # so the very first UPDATE_DOMAIN_STATE intent already has an
        # accurate number, even before any new session closes.
        try:
            tz_seed = _local_tz_offset_s()
            today_ms_seed = await coding_session_store.today_duration_ms(
                tz_offset_s=tz_seed
            )
            by_ide_seed = await coding_session_store.today_duration_by_ide(
                tz_offset_s=tz_seed
            )
            domain_projector.set_coding_today_ms(today_ms_seed)
            domain_projector.set_coding_today_by_ide(by_ide_seed)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "app.coding_today_rollup_seed_failed", error=str(exc)
            )

        # V10 L2-#1: build the island notification publisher with
        # closures over the now-ready dispatcher + session store. The
        # publisher itself never re-imports either, so swapping
        # internals (mock dispatcher in tests, or a future
        # SessionStore subclass) doesn't ripple here.
        def _active_session_lookup():
            actives = session_store.list_active(limit=1)
            return actives[0] if actives else None

        # V10 L2-#3 "不重弹旧通知" 保险丝: load the recent-window
        # session ids from disk and seed the publisher's silenced
        # set. Until the orchestrator (or a user interaction routed
        # through ``session_router``) writes a fresh upsert for one
        # of these ids, ``show_notification(session_id=...)`` for
        # them returns ``emitted=False`` regardless of how many
        # callers (reminders, build-status, future skills) try to
        # raise a notification. ``force=True`` still wins for
        # genuinely-urgent paths (P0 approvals etc.).
        restore_cutoff_ms = int(time.time() * 1000) - (
            cfg.session_restore_window_hours * 3600 * 1000
        )
        try:
            restored_summaries = await session_mem.list_updated_since(
                restore_cutoff_ms
            )
        except Exception as exc:  # noqa: BLE001 — restore is best-effort
            _LOG.warning(
                "app.session_restore.read_failed", error=str(exc)
            )
            restored_summaries = []
        restored_ids = [
            s.session_id for s in restored_summaries if s.session_id
        ]
        if restored_ids:
            _LOG.info(
                "app.session_restore.silenced",
                count=len(restored_ids),
                window_hours=cfg.session_restore_window_hours,
                cutoff_ms=restore_cutoff_ms,
            )

        island_notifications = IslandNotificationPublisher(
            intent_sink,
            active_session_provider=_active_session_lookup,
            perception_provider=lambda: dispatcher.last_perception,
            suppress_frontmost_notifications=cfg.suppress_frontmost_notifications,
            silenced_session_ids=restored_ids,
        )

        # When real activity flows back through the session_store
        # (orchestrator upsert, user reply routed to the session)
        # we drop the restore-silence so the next legitimate
        # ``show_notification`` for that session emits normally.
        # The subscriber is intentionally sync + cheap so it's safe
        # to fire from any code path that mutates the store.
        def _unsilence_on_session_activity(event) -> None:
            if event.kind not in ("upsert", "touch"):
                return
            if island_notifications.unsilence(event.session_id):
                _LOG.info(
                    "app.session_restore.unsilenced_on_activity",
                    session_id=event.session_id,
                    via=event.kind,
                )

        session_store.subscribe(_unsilence_on_session_activity)

        hook_consumer = HookEventConsumer(
            session_store=session_store,
            approval_store=approval_store,
        )

        async def _handle_hook_event(event: HookEvent) -> None:
            hook_consumer.handle(event)
            phase_ui = presentation_for_phase(
                event.phase,
                source=event.source,
                summary=event.summary,
                title=event.title,
                prompt=event.prompt or "",
            )
            try:
                await intent_sink(
                    CompanionIntent(
                        kind=IntentKind.SET_PET_ANIMATION,
                        payload={"state": phase_ui.pet_state},
                    )
                )
                await island_notifications.show_notification(
                    activity_id=f"hook-{event.session_id}-{event.phase.value}",
                    session_id=event.session_id,
                    priority=phase_ui.priority,
                    detail=phase_ui.island_detail,
                    force=phase_ui.force_notification,
                )
            except RuntimeError:
                pass
            await self._send_snapshot_best_effort()

        hook_event_watcher = HookEventWatcher(
            cfg.hook_events_dir,
            _handle_hook_event,
        )
        agent_runtime_store = AgentRuntimeStore()
        agent_runtime_scanner = AgentRuntimeScanner(
            agent_runtime_store,
            session_store,
        )
        agent_event_reducer = AgentEventReducer(
            session_store=session_store,
            approval_store=approval_store,
        )

        async def _handle_codex_agent_event(event) -> None:
            agent_event_reducer.apply(event)
            phase = _phase_for_agent_event(event)
            phase_ui = presentation_for_phase(
                phase,
                source=event.source,
                summary=event.summary,
                title=event.title,
                prompt=getattr(event, "prompt", ""),
            )
            try:
                await intent_sink(
                    CompanionIntent(
                        kind=IntentKind.SET_PET_ANIMATION,
                        payload={"state": phase_ui.pet_state},
                    )
                )
                await island_notifications.show_notification(
                    activity_id=f"{event.source}-{event.session_id}-{phase.value}",
                    session_id=event.session_id,
                    priority=phase_ui.priority,
                    detail=phase_ui.island_detail,
                    force=phase_ui.force_notification,
                )
            except RuntimeError:
                pass
            await self._send_snapshot_best_effort()

        codex_app_server = CodexAppServerCoordinator(
            event_handler=_handle_codex_agent_event
        )
        claude_transcript_watcher = ClaudeTranscriptWatcher(
            handler=_handle_codex_agent_event
        )

        self._runtime = AppRuntime(
            session_memory=session_mem,
            coding_session_store=coding_session_store,
            session_store=session_store,
            session_router=session_router,
            reminder_store=reminder_store,
            reminder_scheduler=reminder_scheduler,
            approval_store=approval_store,
            approval_router=approval_router,
            approval_surface=approval_surface,
            domain_projector=domain_projector,
            profile=profile,
            bridge=bridge,
            dispatcher=dispatcher,
            proactive=proactive,
            build_status=build_status,
            build_status_watcher=build_status_watcher,
            agent_runtime_store=agent_runtime_store,
            agent_runtime_scanner=agent_runtime_scanner,
            codex_app_server=codex_app_server,
            claude_transcript_watcher=claude_transcript_watcher,
            hook_event_watcher=hook_event_watcher,
            project_registry=project_registry,
            degradation=degradation,
            skill_registry=skill_registry,
            character_pack_registry=character_pack_registry,
            island_notifications=island_notifications,
        )
        return self._runtime

    async def serve_forever(self) -> None:
        rt = self._require()
        if self.config.prewarm_enabled:
            self._bg_tasks.append(asyncio.create_task(self._run_prewarm()))
        rt.domain_projector.start()
        rt.approval_surface.start()
        await rt.reminder_scheduler.start()
        # Phase 14-i: start polling ``~/.deskmate/build-status.json``
        # for build/test status updates emitted by the ``deskmate``
        # CLI. Cheap, self-terminates on teardown.
        await rt.build_status_watcher.start()
        if self.config.agent_runtime_scanner_enabled:
            await rt.agent_runtime_scanner.start()
        if self.config.codex_app_server_enabled:
            await rt.codex_app_server.start()
        await rt.claude_transcript_watcher.start()
        await rt.hook_event_watcher.start()
        try:
            await rt.bridge.serve_forever()
        finally:
            await self.teardown()

    async def teardown(self) -> None:
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        self._bg_tasks.clear()
        rt = self._runtime
        if rt is None:
            return
        try:
            await rt.build_status_watcher.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.build_status_watcher_stop_failed", error=str(exc))
        try:
            await rt.hook_event_watcher.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.hook_event_watcher_stop_failed", error=str(exc))
        try:
            await rt.agent_runtime_scanner.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.agent_runtime_scanner_stop_failed", error=str(exc))
        try:
            await rt.codex_app_server.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.codex_app_server_stop_failed", error=str(exc))
        try:
            await rt.claude_transcript_watcher.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.claude_transcript_watcher_stop_failed", error=str(exc))
        try:
            await rt.reminder_scheduler.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.reminder_scheduler_stop_failed", error=str(exc))
        try:
            await rt.domain_projector.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.domain_projector_stop_failed", error=str(exc))
        try:
            await rt.approval_surface.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.approval_surface_stop_failed", error=str(exc))
        try:
            await rt.bridge.stop()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.bridge_stop_failed", error=str(exc))
        try:
            await rt.session_memory.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.session_close_failed", error=str(exc))
        try:
            await rt.coding_session_store.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "app.coding_session_store_close_failed", error=str(exc)
            )
        try:
            await rt.profile.close()
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.profile_close_failed", error=str(exc))
        self._runtime = None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _on_client_connect(self) -> None:
        """V10 L2-#3 + L3-B11: snapshot then agent.ready."""
        rt = self._require()
        snapshot = await self._build_snapshot()
        await rt.bridge.send(
            BridgeEnvelope.of(EnvelopeType.STATE_SNAPSHOT, snapshot)
        )
        await rt.bridge.send(BridgeEnvelope.of(EnvelopeType.AGENT_READY))
        rt.agent_ready_sent.set()

    async def _on_client_disconnect(self) -> None:
        rt = self._runtime
        if rt is not None:
            rt.agent_ready_sent.clear()

    async def _on_envelope(self, env: BridgeEnvelope) -> None:
        rt = self._require()
        with trace_scope(env.trace_id):
            if env.type is EnvelopeType.USER_MESSAGE:
                await rt.dispatcher.on_user_message(
                    env.payload.get("text", ""), trace_id=env.trace_id
                )
            elif env.type is EnvelopeType.USER_CLICK_PET:
                await rt.dispatcher.on_user_click_pet()
            elif env.type is EnvelopeType.PERCEPTION:
                # Phase 16-ii: enrich the proactive context with the
                # latest ``coding_today_ms`` so the nudge selector can
                # pick longer-break copy after a long coding day.
                snapshot = rt.domain_projector.current_state()
                await rt.dispatcher.on_perception_tick(
                    _context_from_perception(
                        env.payload,
                        coding_today_ms=snapshot.coding_today_ms,
                    )
                )
            elif env.type is EnvelopeType.INTERACTION:
                await self._handle_interaction(env.payload)
            elif env.type is EnvelopeType.STATE_SNAPSHOT_REQUEST:
                snapshot = await self._build_snapshot()
                await rt.bridge.send(
                    BridgeEnvelope.of(
                        EnvelopeType.STATE_SNAPSHOT, snapshot, trace_id=env.trace_id
                    )
                )
            elif env.type is EnvelopeType.PERF_METRICS:
                self._handle_perf_metrics(env.payload)
            else:
                _LOG.debug("app.unhandled_envelope", type=env.type.value)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_prewarm(self) -> None:
        rt = self._require()
        rt.prewarm_started.set()
        try:
            await self._llm_prewarm()
        except Exception as exc:  # noqa: BLE001 — prewarm failure is never fatal
            _LOG.warning("app.prewarm_failed", error=str(exc))

    def _handle_perf_metrics(self, payload: dict[str, Any]) -> None:
        """Log Swift-side §3.1 hard budget readings.

        The agent never gates business logic on these — it just emits
        structured logs so a regression shows up in the same stream
        a contributor is already tailing. Validation is best-effort:
        a malformed payload yields a warning instead of an exception
        so the bridge handler stays resilient (V10 L1 forward-compat).
        """
        try:
            wake_s_raw = payload.get("last_wake_seconds")
            wake_s = float(wake_s_raw) if wake_s_raw is not None else None
            total_frames = int(payload.get("total_frames", 0))
            dropped_frames = int(payload.get("dropped_frames", 0))
            ratio_raw = payload.get("frame_drop_ratio", 0.0)
            frame_drop_ratio = float(ratio_raw)
        except (TypeError, ValueError) as exc:
            _LOG.warning("app.perf_metrics_invalid", error=str(exc), payload=payload)
            return
        _LOG.info(
            "app.perf_metrics",
            wake_s=wake_s,
            total_frames=total_frames,
            dropped_frames=dropped_frames,
            frame_drop_pct=frame_drop_ratio * 100.0,
        )

    async def _handle_interaction(self, payload: dict[str, Any]) -> None:
        """Route typed :class:`InteractionAction` payloads (V10 L1-F).

        Malformed actions are logged but never raise — the bridge handler
        must stay resilient so a single bad client packet can't take the
        agent down.
        """
        rt = self._require()
        try:
            action = InteractionAction.model_validate(payload)
        except ValueError as exc:
            _LOG.warning("app.interaction_invalid", error=str(exc), payload=payload)
            return
        _LOG.info(
            "app.interaction_received",
            kind=action.kind.value,
            source=action.source.value,
            target=action.target.value,
        )
        # Dispatch on kind so each router owns its verbs regardless of
        # which surface originated the action (V10 L1-F).
        if action.kind is InteractionKind.PERMISSION_RESOLVE:
            rt.approval_router.handle(action)
        elif action.kind in {
            InteractionKind.SESSION_JUMP,
            InteractionKind.QUESTION_ANSWER,
        }:
            result = rt.session_router.handle(action)
            if action.kind is InteractionKind.SESSION_JUMP and result.handled:
                await self._handle_session_jump_result(result)
            elif action.kind is InteractionKind.QUESTION_ANSWER and result.handled:
                await self._handle_question_answer_result(result)
        elif action.kind is InteractionKind.DEMO_TRIGGER:
            await self._handle_demo_trigger(action)
        elif action.kind is InteractionKind.SURFACE_DISMISS:
            _LOG.debug(
                "app.surface_dismiss",
                source=action.source.value,
                surface=action.payload.get("surface"),
            )
        else:
            # V10 L1-F catch-all: every typed kind must surface as a
            # structured log line, never get silently dropped.
            # ``TASK_OPEN_DETAIL / PET_INTERACT / PET_DRAG / PET_NEST``
            # currently land here as no-ops; future skills that bind to
            # them should add their own ``elif`` branch above.
            _LOG.info(
                "app.interaction_unhandled",
                kind=action.kind.value,
                source=action.source.value,
                target=action.target.value,
                payload_keys=sorted(action.payload.keys()),
            )

    async def _handle_session_jump_result(self, result) -> None:
        if result.effect in {
            "session.jump.opened",
            "session.jump.workspace_opened",
            "session.jump.activated",
        }:
            return
        text = "Session marked active."
        if result.effect == "session.jump.accepted":
            text = "I could not find an exact window, but marked that session active."
        elif result.effect == "session.jump.unknown_id":
            text = "That session is no longer active."
        await self._send_bridge_envelope_best_effort(
            BridgeEnvelope.of(
                EnvelopeType.INTENT,
                {
                    "kind": IntentKind.SHOW_PET_BUBBLE.value,
                    "payload": {
                        "bubble": BubbleSpec(
                            id=f"session-jump-{result.session_id or 'unknown'}",
                            kind=BubbleKind.STATUS,
                            text=text,
                            ttl_ms=3_000,
                            priority=Priority.P2,
                        ).model_dump(mode="json")
                    },
                },
            )
        )

    async def _handle_question_answer_result(self, result) -> None:
        if result.effect != "session.question_answer.accepted":
            return
        await self._send_bridge_envelope_best_effort(
            BridgeEnvelope.of(
                EnvelopeType.INTENT,
                {
                    "kind": IntentKind.SHOW_PET_BUBBLE.value,
                    "payload": {
                        "bubble": BubbleSpec(
                            id=f"session-answer-{result.session_id or 'unknown'}",
                            kind=BubbleKind.STATUS,
                            text="Answer sent to the agent session.",
                            ttl_ms=2_500,
                            priority=Priority.P2,
                        ).model_dump(mode="json")
                    },
                },
            )
        )
        await self._set_pet_animation("working")
        await self._send_snapshot_best_effort()

    async def _handle_demo_trigger(self, action: InteractionAction) -> None:
        rt = self._require()
        scenario = str(action.payload.get("scenario", "")).strip()
        now_ms = int(time.time() * 1000)
        if scenario == "build":
            with contextlib.suppress(RuntimeError):
                await rt.build_status.on_build_start("Demo Build", branch="demo")
            await self._set_pet_animation("working")
        elif scenario == "approval":
            rt.approval_store.add(
                Approval(
                    approval_id="demo-approval",
                    prompt="Allow Deskmate to run the demo action?",
                    priority=Priority.P0,
                    created_at_ms=now_ms,
                    expires_at_ms=now_ms + 10 * 60 * 1000,
                    extras={"demo": True},
                )
            )
            await self._set_pet_animation("alert")
        elif scenario == "reminder":
            rt.reminder_store.add(
                Reminder(
                    reminder_id="demo-reminder",
                    text="Demo reminder is due now.",
                    due_at_ms=now_ms,
                    created_at_ms=now_ms,
                    priority=Priority.P1,
                    extras={"demo": True},
                )
            )
            with contextlib.suppress(RuntimeError):
                await rt.reminder_scheduler.process_due(now_ms)
            with contextlib.suppress(RuntimeError):
                await rt.island_notifications.show_notification(
                    activity_id="demo-reminder",
                    priority=Priority.P1,
                    detail="Reminder due now",
                    force=True,
                )
            await self._set_pet_animation("alert")
        elif scenario == "codex_session":
            rt.session_store.upsert(
                SessionInfo(
                    session_id="demo-codex-session",
                    title="Codex demo",
                    summary="Fake Codex session routed through Deskmate.",
                    priority=Priority.P1,
                    created_at_ms=now_ms,
                    updated_at_ms=now_ms,
                    phase=SessionPhase.RUNNING,
                    cwd=str(Path.cwd()),
                    extras={"demo": True, "agent": "codex"},
                )
            )
            await self._send_bridge_envelope_best_effort(
                BridgeEnvelope.of(
                    EnvelopeType.INTENT,
                    {
                        "kind": IntentKind.PRESENT_ISLAND.value,
                        "payload": {
                            "surface": "live_activity",
                            "activity_id": "demo-codex-session",
                            "session_id": "demo-codex-session",
                            "priority": "P1",
                            "detail": "Codex demo · running",
                        },
                    },
                )
            )
            await self._set_pet_animation("working")
        elif scenario == "clear":
            rt.approval_store.cancel("demo-approval", now_ms)
            rt.approval_store.clear()
            rt.reminder_store.clear()
            rt.session_store.remove("demo-codex-session")
            with contextlib.suppress(RuntimeError):
                await rt.build_status.on_external_dismiss()
            await self._send_bridge_envelope_best_effort(
                BridgeEnvelope.of(
                    EnvelopeType.INTENT,
                    {
                        "kind": IntentKind.DISMISS_ISLAND.value,
                        "payload": {"id": "demo-codex-session"},
                    },
                )
            )
            await self._set_pet_animation("idle")
            await self._send_bridge_envelope_best_effort(
                BridgeEnvelope.of(
                    EnvelopeType.INTENT,
                    {
                        "kind": IntentKind.SHOW_PET_BUBBLE.value,
                        "payload": {
                            "bubble": BubbleSpec(
                                id="demo-cleared",
                                kind=BubbleKind.SYSTEM,
                                text="Demo state cleared.",
                                ttl_ms=3_000,
                                priority=Priority.P3,
                            ).model_dump(mode="json")
                        },
                    },
                )
            )
        else:
            _LOG.warning("app.demo_unknown_scenario", scenario=scenario)
            return
        await self._send_snapshot_best_effort()

    async def _set_pet_animation(self, state: str) -> None:
        await self._send_bridge_envelope_best_effort(
            BridgeEnvelope.of(
                EnvelopeType.INTENT,
                {
                    "kind": IntentKind.SET_PET_ANIMATION.value,
                    "payload": {"state": state},
                },
            )
        )

    async def _send_bridge_envelope_best_effort(self, env: BridgeEnvelope) -> None:
        rt = self._require()
        try:
            await rt.bridge.send(env)
        except RuntimeError:
            return

    async def _send_snapshot_best_effort(self) -> None:
        rt = self._require()
        try:
            snapshot = await self._build_snapshot()
            await rt.bridge.send(BridgeEnvelope.of(EnvelopeType.STATE_SNAPSHOT, snapshot))
        except RuntimeError:
            return
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("app.snapshot_push_failed", error=str(exc))

    async def _build_snapshot(self) -> dict[str, Any]:
        rt = self._require()
        cutoff_ms = int(time.time() * 1000) - (
            self.config.session_restore_window_hours * 3600 * 1000
        )
        sessions = await rt.session_memory.list_updated_since(cutoff_ms)
        active_sessions = [
            s.model_dump(mode="json") for s in rt.session_store.list()
        ]
        pending_reminders = [
            r.model_dump(mode="json") for r in rt.reminder_store.list()
        ]
        pending_approvals_detail = [
            a.model_dump(mode="json") for a in rt.approval_store.list_pending()
        ]
        domain_state = rt.domain_projector.current_state()
        return {
            "domain_state": domain_state.model_dump(mode="json"),
            "recent_sessions": [
                {
                    "session_id": s.session_id,
                    "summary": s.summary,
                    "started_at_ms": s.started_at_ms,
                    "updated_at_ms": s.updated_at_ms,
                    "ended_at_ms": s.ended_at_ms,
                }
                for s in sessions
            ],
            "active_sessions": active_sessions,
            "pending_reminders": pending_reminders,
            "pending_approvals_detail": pending_approvals_detail,
        }

    def _require(self) -> AppRuntime:
        if self._runtime is None:
            raise RuntimeError("App.setup() not called")
        return self._runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _context_from_perception(
    payload: dict[str, Any],
    *,
    coding_today_ms: int = 0,
) -> ProactiveContext:
    """Build a :class:`ProactiveContext` from a raw ``perception`` payload."""
    focus_raw = payload.get("focus", "casual")
    try:
        focus = UserFocus(focus_raw)
    except ValueError:
        focus = UserFocus.CASUAL
    snap = PerceptionSnapshot(
        user_state=str(payload.get("user_state", "idle")),
        focus=focus,
        app_bundle_id=payload.get("app"),
        window_title=payload.get("title"),
        idle_ms=int(payload.get("idle_ms", 0)),
    )
    return ProactiveContext(
        perception=snap,
        coding_today_ms=max(0, int(coding_today_ms)),
    )


__all__ = ["App", "AppConfig", "AppRuntime", "LLMPrewarmFn"]
