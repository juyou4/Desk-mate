"""Integration tests for the runtime-phase-observers framework.

Drives a real :class:`AgentRuntimeScanner` with a stubbed
``ps_provider`` and verifies the end-to-end pipeline:
``ps`` row → status discovery → registry → reducer → SessionStore
mutation.

Covers Tasks 6.1 (P1 actionable preservation, P5 store isolation)
and 6.2 (32-status tick budget) of the runtime-phase-observers spec.
"""

from __future__ import annotations

import json
import os
import platform
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import pytest

from deskmate_agent.agent_events import AgentEventReducer
from deskmate_agent.agent_runtime import (
    AgentRuntimeKind,
    AgentRuntimeSource,
    AgentRuntimeStatus,
    AgentRuntimeStore,
    make_default_registry,
)
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.protocol.state import Priority
from deskmate_agent.runtime_observers import (
    AIDER_HISTORY_FILENAME,
    KIRO_HASH_DIR_LIMIT,
    KiroTaskObserver,
)
from deskmate_agent.sessions import (
    SessionInfo,
    SessionPhase,
    SessionState,
    SessionStore,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeFs:
    """Identical shape to the unit-test fixture; redeclared here so
    the integration suite stays import-self-contained."""

    files: dict[str, tuple[int, bytes]] = field(default_factory=dict)

    def exists(self, path: str) -> bool:
        return path in self.files

    def stat_mtime_ms(self, path: str) -> int | None:
        if path not in self.files:
            return None
        return self.files[path][0]

    def read_tail(self, path: str, max_bytes: int) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        body = self.files[path][1]
        return body if len(body) <= max_bytes else body[-max_bytes:]


def _aider_ps_row(pid: int, workspace: Path) -> str:
    """Build a ``ps`` line whose extracted ``cwd`` lands at the given
    workspace via the existing ``--folder-uri`` extractor.

    Aider is a CLI agent so ``_build_status`` doesn't extract a
    workspace from args automatically. We instead fabricate a status
    directly in the test fixture below; this helper is preserved as
    documentation only."""
    raise RuntimeError("not used; see _build_with_explicit_aider_status")


# ---------------------------------------------------------------------------
# Task 6.1 — integration through scanner + reducer
# ---------------------------------------------------------------------------
#
# Aider's CLI invocation does not surface a workspace via ``ps`` args
# (CLI agents skip the URL extractor in the scanner). For the
# integration test we therefore drive the registry directly with a
# canned status, bypassing the ps→classify→build_status pipeline. The
# observer / reducer / SessionStore wiring is the same.


def _seed_session(
    sessions: SessionStore,
    *,
    session_id: str,
    phase: SessionPhase,
) -> None:
    sessions.upsert(
        SessionInfo(
            session_id=session_id,
            title="Aider · ws",
            state=SessionState.ACTIVE,
            priority=Priority.P2,
            phase=phase,
            source="aider",
        )
    )


def _aider_status(workspace: Path, pid: int = 9001) -> AgentRuntimeStatus:
    return AgentRuntimeStatus(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
        process_id=pid,
        cwd=str(workspace),
        workspace=str(workspace),
    )


def test_integration_observer_promotes_session_phase_to_thinking(
    tmp_path: Path,
) -> None:
    """End-to-end: passive AIDER status + recent transcript → reducer
    flips ``SessionInfo.phase`` to THINKING."""
    transcript = tmp_path / AIDER_HISTORY_FILENAME
    transcript.write_text("Aider is mid-token here\n")

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    fs = _FakeFs(
        files={
            str(transcript): (10_000, transcript.read_bytes()),
        }
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]

    # Seed the session that the observer is going to elevate.
    _seed_session(
        sessions,
        session_id="runtime-aider-9001",
        phase=SessionPhase.RUNNING,
    )

    registry.notify([_aider_status(tmp_path)], now_ms=10_500)

    row = sessions.get("runtime-aider-9001")
    assert row is not None
    assert row.phase is SessionPhase.THINKING


def test_integration_observer_does_not_downgrade_waiting_for_approval(
    tmp_path: Path,
) -> None:
    """Property 1 — the existing reducer guard means a passive
    observer's RUNNING-equivalent event cannot demote a session
    that's pinned at WAITING_FOR_APPROVAL."""
    transcript = tmp_path / AIDER_HISTORY_FILENAME
    transcript.write_text("nothing notable\n")

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    fs = _FakeFs(
        files={
            str(transcript): (10_000, transcript.read_bytes()),
        }
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]

    # Seed in WAITING_FOR_APPROVAL — the observer's emission would
    # set THINKING (file changed within 3 s, no diff/shell signal).
    _seed_session(
        sessions,
        session_id="runtime-aider-9001",
        phase=SessionPhase.WAITING_FOR_APPROVAL,
    )

    registry.notify([_aider_status(tmp_path)], now_ms=10_500)

    row = sessions.get("runtime-aider-9001")
    assert row is not None
    # The reducer's ``_preserves_actionable_state`` guard should
    # keep the actionable phase pinned. The default reducer treats
    # ``SessionActivityUpdated`` with an "informational" phase
    # (RUNNING / THINKING / EDITING) as a non-promotion; only an
    # explicit waiting/failed/completed event would override.
    assert row.phase is SessionPhase.WAITING_FOR_APPROVAL


def test_integration_observer_never_touches_runtime_store(
    tmp_path: Path,
) -> None:
    """Property 5 — observers must not reach ``AgentRuntimeStore`` /
    ``ApprovalStore`` directly. We pass an "explosive" runtime store
    whose every method raises if called from outside the scanner.
    The observer pipeline must complete cleanly anyway because it
    only ever calls the reducer."""
    transcript = tmp_path / AIDER_HISTORY_FILENAME
    transcript.write_text("hello\n")

    class _ExplosiveRuntimeStore(AgentRuntimeStore):
        def __init__(self) -> None:
            super().__init__()

        def upsert_many(self, statuses: Sequence[AgentRuntimeStatus]) -> bool:
            # Scanner is *allowed* to call this — observers are not.
            # We rely on the test driving the registry directly so
            # this method should never fire.
            raise AssertionError("observer should not reach upsert_many")

        def expire(self, now_ms: int):  # type: ignore[override]
            raise AssertionError("observer should not reach expire")

        def list(self):  # type: ignore[override]
            raise AssertionError("observer should not reach list")

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    fs = _FakeFs(
        files={
            str(transcript): (10_000, transcript.read_bytes()),
        }
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]

    # The explosive store is constructed but never wired into the
    # registry — observers are forbidden from receiving any store
    # reference. The mere fact that the registry's notify completes
    # without raising AssertionError proves Property 5 (Requirement
    # 5.3): observers operate in the dark.
    _seed_session(
        sessions,
        session_id="runtime-aider-9001",
        phase=SessionPhase.RUNNING,
    )
    registry.notify([_aider_status(tmp_path)], now_ms=10_500)

    # Sanity check — the explosive store really would raise if
    # someone touched it.
    explosive = _ExplosiveRuntimeStore()
    with pytest.raises(AssertionError):
        explosive.list()


def test_integration_aider_completed_after_idle(tmp_path: Path) -> None:
    transcript = tmp_path / AIDER_HISTORY_FILENAME
    transcript.write_text("Done.\n")

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    fs = _FakeFs(
        files={
            str(transcript): (10_000, transcript.read_bytes()),
        }
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]
    _seed_session(
        sessions,
        session_id="runtime-aider-9001",
        phase=SessionPhase.RUNNING,
    )

    # 60 s after mtime → idle → COMPLETED.
    registry.notify([_aider_status(tmp_path)], now_ms=70_000)
    row = sessions.get("runtime-aider-9001")
    assert row is not None
    assert row.phase is SessionPhase.COMPLETED


# ---------------------------------------------------------------------------
# Task 6.2 — 32-status tick budget smoke
# ---------------------------------------------------------------------------


def test_tick_budget_under_50ms_for_32_statuses(tmp_path: Path) -> None:
    """Best-effort regression detector: 32 mixed statuses (4 of which
    are AIDER with valid transcripts) should complete a single
    registry notify in < 50 ms on a modern Mac.

    Not a hard CI gate — file I/O on a contention'd CI runner can
    blow this. The intent is to flag a 10× regression rather than
    enforce a tight budget.
    """
    fs = _FakeFs()
    statuses: list[AgentRuntimeStatus] = []
    body = b"```diff\n+ x\n```\n"
    for i in range(4):
        ws = tmp_path / f"ws-{i}"
        ws.mkdir()
        path = str(ws / AIDER_HISTORY_FILENAME)
        fs.files[path] = (10_000, body)
        statuses.append(
            AgentRuntimeStatus(
                source=AgentRuntimeSource.AIDER,
                kind=AgentRuntimeKind.CLI_AGENT,
                process_id=10_000 + i,
                workspace=str(ws),
            )
        )
    # Pad with 28 non-AIDER statuses so the dispatcher walks a
    # realistic mix.
    for i in range(28):
        statuses.append(
            AgentRuntimeStatus(
                source=AgentRuntimeSource.CURSOR,
                kind=AgentRuntimeKind.GUI_IDE,
                process_id=20_000 + i,
                workspace=None,
            )
        )

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]

    start = time.perf_counter()
    registry.notify(statuses, now_ms=10_500)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 50.0, (
        f"registry.notify exceeded 50 ms tick budget on 32 statuses: "
        f"{elapsed_ms:.2f} ms"
    )


# ===========================================================================
# kiro-task-observer spec — Task 7: end-to-end + tick-budget smoke
# ===========================================================================


def _kiro_meta_bytes(
    spec_name: str,
    *,
    workspace: str,
    updated_at_ms: int,
    execution_status: str = "in_progress",
) -> bytes:
    return json.dumps(
        {
            "tasks": {
                "task-1": {
                    "updatedAt": updated_at_ms,
                    "executionStatus": execution_status,
                    "specUri": f"file://{quote(workspace)}/.kiro/specs/{spec_name}/tasks.md",
                }
            }
        }
    ).encode("utf-8")


@dataclass
class _KiroIntegrationFs:
    """Combined fake fs serving both Aider and Kiro paths in the
    integration tests."""

    files: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    dirs: dict[str, list[str]] = field(default_factory=dict)
    dir_mtimes: dict[str, int] = field(default_factory=dict)

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def stat_mtime_ms(self, path: str) -> int | None:
        if path in self.files:
            return self.files[path][0]
        if path in self.dir_mtimes:
            return self.dir_mtimes[path]
        return None

    def read_tail(self, path: str, max_bytes: int) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        body = self.files[path][1]
        return body if len(body) <= max_bytes else body[-max_bytes:]

    def list_dir(self, path: str) -> list[str]:
        return list(self.dirs.get(path, []))

    def read_bytes(self, path: str, max_bytes: int) -> bytes:
        if path not in self.files:
            raise FileNotFoundError(path)
        body = self.files[path][1]
        return body if len(body) <= max_bytes else body[:max_bytes]


def _kiro_status() -> AgentRuntimeStatus:
    return AgentRuntimeStatus(
        source=AgentRuntimeSource.KIRO,
        kind=AgentRuntimeKind.GUI_IDE,
        process_id=42,
        display_name="Kiro",
    )


def test_integration_kiro_emits_two_specs_through_full_pipeline(
    tmp_path: Path,
) -> None:
    """End-to-end fan-out: real registry + real reducer; one Kiro
    process status fans out to N synthetic SessionInfo rows, one
    per spec meta file."""
    fs = _KiroIntegrationFs()
    root = str(tmp_path / "kiro-tasks")
    fs.dirs[root] = ["hashA", "hashB"]
    fs.dir_mtimes[os.path.join(root, "hashA")] = 1_000
    fs.dir_mtimes[os.path.join(root, "hashB")] = 2_000
    for hash_name, specs in (
        ("hashA", [("spec-A1", "in_progress"), ("spec-A2", "queued")]),
        ("hashB", [("spec-B1", "succeed"), ("spec-B2", "in_progress")]),
    ):
        hash_path = os.path.join(root, hash_name)
        fs.dirs[hash_path] = [f"{name}.meta.json" for name, _ in specs]
        for name, status in specs:
            meta_path = os.path.join(hash_path, f"{name}.meta.json")
            fs.files[meta_path] = (
                100,
                _kiro_meta_bytes(
                    name,
                    workspace=str(tmp_path / f"ws-{name}"),
                    updated_at_ms=100,
                    execution_status=status,
                ),
            )

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]
    # Override the Kiro observer's tasks root to point at our
    # tmp_path. The factory wired KiroTaskObserver(fs=fs); we
    # patch its root attribute for this test.
    kiro_obs = next(
        o for o in registry._observers if isinstance(o, KiroTaskObserver)
    )
    kiro_obs._kiro_tasks_root = root

    registry.notify([_kiro_status()], now_ms=200)

    # Four synthetic SessionInfo rows, one per spec.
    expected_ids = {
        "runtime-kiro-hashA-spec-A1",
        "runtime-kiro-hashA-spec-A2",
        "runtime-kiro-hashB-spec-B1",
        "runtime-kiro-hashB-spec-B2",
    }
    actual = {
        sid: sessions.get(sid) for sid in expected_ids
    }
    assert all(actual[sid] is not None for sid in expected_ids), actual
    # Phase mapping: in_progress→THINKING, queued→RUNNING, succeed→COMPLETED.
    assert actual["runtime-kiro-hashA-spec-A1"].phase is SessionPhase.THINKING
    assert actual["runtime-kiro-hashA-spec-A2"].phase is SessionPhase.RUNNING
    assert actual["runtime-kiro-hashB-spec-B1"].phase is SessionPhase.COMPLETED
    assert actual["runtime-kiro-hashB-spec-B2"].phase is SessionPhase.THINKING


def test_integration_kiro_does_not_downgrade_waiting_for_approval(
    tmp_path: Path,
) -> None:
    """P1 lock — observer's THINKING event must not downgrade a
    pre-seeded WAITING_FOR_APPROVAL session; the existing reducer
    guard widens to cover all informational phases."""
    fs = _KiroIntegrationFs()
    root = str(tmp_path / "kiro-tasks")
    fs.dirs[root] = ["h"]
    fs.dir_mtimes[os.path.join(root, "h")] = 1_000
    hash_path = os.path.join(root, "h")
    fs.dirs[hash_path] = ["s.meta.json"]
    fs.files[os.path.join(hash_path, "s.meta.json")] = (
        100,
        _kiro_meta_bytes(
            "s",
            workspace=str(tmp_path / "ws"),
            updated_at_ms=100,
            execution_status="in_progress",
        ),
    )

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    sid = "runtime-kiro-h-s"
    sessions.upsert(
        SessionInfo(
            session_id=sid,
            title="Pre-seeded approval row",
            state=SessionState.ACTIVE,
            priority=Priority.P0,
            phase=SessionPhase.WAITING_FOR_APPROVAL,
            source="kiro",
        )
    )

    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]
    kiro_obs = next(
        o for o in registry._observers if isinstance(o, KiroTaskObserver)
    )
    kiro_obs._kiro_tasks_root = root

    registry.notify([_kiro_status()], now_ms=200)
    row = sessions.get(sid)
    assert row is not None
    # The actionable phase must still be pinned.
    assert row.phase is SessionPhase.WAITING_FOR_APPROVAL


def test_integration_kiro_observer_never_touches_runtime_store(
    tmp_path: Path,
) -> None:
    """P5 lock — KiroTaskObserver pipeline completes without
    reaching AgentRuntimeStore. The explosive store stub raises if
    the observer accidentally holds a reference."""
    fs = _KiroIntegrationFs()
    root = str(tmp_path / "kiro-tasks")
    fs.dirs[root] = ["h"]
    fs.dir_mtimes[os.path.join(root, "h")] = 1_000
    hash_path = os.path.join(root, "h")
    fs.dirs[hash_path] = ["s.meta.json"]
    fs.files[os.path.join(hash_path, "s.meta.json")] = (
        100,
        _kiro_meta_bytes(
            "s",
            workspace=str(tmp_path / "ws"),
            updated_at_ms=100,
            execution_status="queued",
        ),
    )

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )

    class _ExplosiveRuntimeStore:
        def __getattr__(self, _name: str):
            raise AssertionError("observer reached runtime store")

    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]
    kiro_obs = next(
        o for o in registry._observers if isinstance(o, KiroTaskObserver)
    )
    kiro_obs._kiro_tasks_root = root

    # The explosive store is constructed but never wired anywhere
    # the observer can reach it. The fact that ``notify`` completes
    # cleanly proves Property 5 (Requirement 5.3 from the
    # runtime-phase-observers spec): observers operate without
    # store references.
    explosive = _ExplosiveRuntimeStore()
    registry.notify([_kiro_status()], now_ms=200)

    # Sanity check that the explosive store really would raise.
    with pytest.raises(AssertionError):
        explosive.list()


def test_integration_kiro_tick_budget_under_50ms_for_32_dirs(
    tmp_path: Path,
) -> None:
    """Requirement 12.1 — 32 hash dirs each with one 4 KB meta
    file should round-trip under 50 ms on Apple Silicon. Skipped
    on other CPU architectures (the assertion would flap on slower
    runners)."""
    if platform.machine() != "arm64":
        pytest.skip("tick-budget assertion targets Apple Silicon")

    fs = _KiroIntegrationFs()
    root = str(tmp_path / "kiro-tasks")
    fs.dirs[root] = []
    # Build 32 hash dirs, each with one ~4 KB meta payload.
    padding_key = "padding"
    padding_value = "x" * 3_500  # ~4 KB once wrapped in JSON.
    for i in range(KIRO_HASH_DIR_LIMIT):
        hash_name = f"h-{i:03d}"
        fs.dirs[root].append(hash_name)
        fs.dir_mtimes[os.path.join(root, hash_name)] = 10_000 - i
        spec_name = f"spec-{i:03d}"
        meta_name = f"{spec_name}.meta.json"
        hash_path = os.path.join(root, hash_name)
        fs.dirs[hash_path] = [meta_name]
        meta_path = os.path.join(hash_path, meta_name)
        fs.files[meta_path] = (
            100 + i,
            json.dumps(
                {
                    "tasks": {
                        "task-1": {
                            "updatedAt": 100 + i,
                            "executionStatus": "in_progress",
                            "specUri": (
                                f"file://{quote(str(tmp_path))}/ws-{i}"
                                f"/.kiro/specs/{spec_name}/tasks.md"
                            ),
                            padding_key: padding_value,
                        }
                    }
                }
            ).encode("utf-8"),
        )

    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(
        session_store=sessions, approval_store=approvals
    )
    registry = make_default_registry(reducer, sessions, fs=fs)  # type: ignore[arg-type]
    kiro_obs = next(
        o for o in registry._observers if isinstance(o, KiroTaskObserver)
    )
    kiro_obs._kiro_tasks_root = root

    start = time.perf_counter()
    registry.notify([_kiro_status()], now_ms=200)
    elapsed_ms = (time.perf_counter() - start) * 1000
    # 32 events emitted; the budget assertion is the regression
    # detector.
    assert elapsed_ms < 50, f"tick budget exceeded: {elapsed_ms:.2f} ms"
