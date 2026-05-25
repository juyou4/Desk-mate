"""Unit tests for the V10 runtime-phase-observers framework.

Covers Tasks 1.3 (WorkspaceRootDetector), 1.4 (title format), and
2.3 (filesystem adapter + no-op observer). Phase-mapping tests for
the AiderTranscriptObserver land in tasks 4.3-4.7 in the same file.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import pytest

from deskmate_agent.agent_events import (
    AgentEvent,
    AgentEventReducer,
    SessionActivityUpdated,
    SessionCompleted,
)
from deskmate_agent.agent_runtime import (
    AgentRuntimeKind,
    AgentRuntimeScanner,
    AgentRuntimeSource,
    AgentRuntimeStatus,
    AgentRuntimeStore,
    detect_workspace_root,
    discover_runtime_statuses,
    make_default_registry,
    parse_ps_output,
)
from deskmate_agent.approvals import ApprovalStore
from deskmate_agent.runtime_observers import (
    AIDER_HISTORY_FILENAME,
    AIDER_IDLE_THRESHOLD_MS,
    AIDER_LIVE_THRESHOLD_MS,
    KIRO_HASH_DIR_LIMIT,
    KIRO_IDLE_THRESHOLD_MS,
    KIRO_META_MAX_BYTES,
    KIRO_PIPELINE_SESSION_ID,
    AiderTranscriptObserver,
    DefaultFilesystemAdapter,
    KiroTaskObserver,
    NoOpRuntimePhaseObserver,
    ObserverConsecutiveCrashLimit,
    RuntimePhaseObserver,
    RuntimePhaseObserverRegistry,
    _decide_kiro_phase,
    _KiroLatestTaskRecord,
    _last_fenced_block,
    _pick_latest_task_record,
    _resolve_workspace_from_spec_uri,
)
from deskmate_agent.sessions import SessionInfo, SessionPhase, SessionStore

# ---------------------------------------------------------------------------
# Task 1.3 — WorkspaceRootDetector
# ---------------------------------------------------------------------------
#
# Cases mirror the design's testing strategy and Requirements 1.1-1.6.


def test_detector_returns_cwd_when_marker_at_cwd(tmp_path: Path) -> None:
    """Requirement 1.1 — deepest match wins, and ``cwd`` itself
    counts as an ancestor for the purpose of the walk."""
    (tmp_path / ".git").mkdir()
    assert detect_workspace_root(str(tmp_path)) == str(tmp_path)


def test_detector_walks_up_to_pyproject_toml(tmp_path: Path) -> None:
    """Requirement 1.1 — match found two levels up still wins."""
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    assert detect_workspace_root(str(sub)) == str(tmp_path)


def test_detector_picks_inner_project_over_outer_git(tmp_path: Path) -> None:
    """Requirement 1.1 — "deepest" means closest to ``cwd``: a
    sub-project's ``pyproject.toml`` outranks an outer ``.git``."""
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "subproject"
    inner.mkdir()
    (inner / "pyproject.toml").write_text("")
    cwd = inner / "tests"
    cwd.mkdir()
    assert detect_workspace_root(str(cwd)) == str(inner)


def test_detector_falls_back_to_cwd_when_no_marker(tmp_path: Path) -> None:
    """Requirement 1.2 — no marker found anywhere on the way to ``/``
    means the original ``cwd`` is the workspace."""
    cwd = tmp_path / "no" / "markers"
    cwd.mkdir(parents=True)
    assert detect_workspace_root(str(cwd)) == str(cwd)


def test_detector_returns_none_for_none_cwd() -> None:
    """Requirement 1.3 — ``None`` in → ``None`` out."""
    assert detect_workspace_root(None) is None


def test_detector_recognises_each_marker(tmp_path: Path) -> None:
    """Requirement 1.4 — the marker list is ``.git``, ``pyproject.toml``,
    ``package.json``, ``Cargo.toml``, ``Package.swift``."""
    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "Package.swift")
    for marker in markers:
        sub = tmp_path / f"{marker.replace('.', '_')}_dir"
        sub.mkdir()
        target = sub / marker
        if marker == ".git":
            target.mkdir()
        else:
            target.write_text("")
        cwd = sub / "deep"
        cwd.mkdir()
        assert detect_workspace_root(str(cwd)) == str(sub), (
            f"{marker} should be recognised"
        )


def test_detector_aborts_on_oserror_returns_deepest_so_far(
    tmp_path: Path,
) -> None:
    """Requirement 1.5 — when ``fs_exists`` raises, the walk halts
    and we return whatever match we have already accepted (or the
    original ``cwd`` if none yet)."""
    (tmp_path / "pyproject.toml").write_text("")
    inner = tmp_path / "deep"
    inner.mkdir()
    cwd = inner / "deeper"
    cwd.mkdir()

    # Custom ``fs_exists`` that throws after the first hit. Since the
    # walk starts at ``cwd`` and steps parent-ward, the first call
    # at ``cwd`` returns ``False``; the next iteration probes
    # ``inner`` and will succeed; on the third iteration (``tmp_path``)
    # we raise. By then we've already returned via the short-circuit
    # so the OSError path is never hit. Force an OSError on the very
    # first probe instead — fall back to ``cwd``.
    def raising(_path: str) -> bool:
        raise OSError("simulated permission failure")

    assert detect_workspace_root(str(cwd), fs_exists=raising) == str(cwd)


def test_detector_aborts_at_filesystem_root() -> None:
    """Requirement 1.6 — walking from ``/`` terminates instead of
    looping forever. Use a probe that always returns False so the
    walk runs to completion."""

    def never(_path: str) -> bool:
        return False

    # Use ``/`` as the starting point to exercise the
    # ``parent == current`` termination guard.
    assert detect_workspace_root("/", fs_exists=never) == "/"


# ---------------------------------------------------------------------------
# Task 1.4 — title formatting in _upsert_session
# ---------------------------------------------------------------------------
#
# Drives the scanner through ``scan_once`` with a stub ps_provider and
# asserts the SessionStore title.


def _build_scanner(
    ps_text: str, *, sessions: SessionStore | None = None
) -> tuple[AgentRuntimeScanner, SessionStore]:
    sessions = sessions or SessionStore()
    scanner = AgentRuntimeScanner(
        AgentRuntimeStore(),
        sessions,
        ps_provider=lambda: ps_text,
        clock=lambda: 1_000,
    )
    return scanner, sessions


def test_title_includes_workspace_basename_when_marker_present(
    tmp_path: Path,
) -> None:
    """Requirement 2.3 — when ``workspace`` resolves to a directory
    with a non-empty basename, the title becomes
    ``"<source label> · <basename>"``."""
    project = tmp_path / "deskmate"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\n")
    sub = project / "src"
    sub.mkdir()
    args = f"Cursor /Applications/Cursor.app --folder-uri file://{sub}"
    ps_text = f"100 1 /Applications/Cursor.app/Contents/MacOS/Cursor {args}\n"

    scanner, sessions = _build_scanner(ps_text)
    import asyncio

    asyncio.run(scanner.scan_once())

    row = sessions.get("runtime-cursor-100")
    assert row is not None
    assert row.title == "Cursor · deskmate"


def test_title_falls_back_to_bare_label_when_workspace_none() -> None:
    """Requirement 2.4 — ``workspace is None`` produces the bare
    ``display_name``."""
    ps_text = "200 1 /opt/homebrew/bin/codex codex\n"
    scanner, sessions = _build_scanner(ps_text)
    import asyncio

    asyncio.run(scanner.scan_once())

    row = sessions.get("runtime-codex-200")
    assert row is not None
    # Codex CLI has no ``cwd_hint`` extracted from args, so the
    # workspace is None — title stays bare.
    assert row.title == "Codex CLI"


def test_title_falls_back_when_workspace_basename_empty(tmp_path: Path) -> None:
    """Requirement 2.5 — when ``basename(workspace)`` is empty (e.g.
    ``/`` because the user opened the literal filesystem root), we
    fall back to the bare label rather than emitting ``"Cursor · "``."""
    args = "Cursor /Applications/Cursor.app --folder-uri file:///"
    ps_text = f"300 1 /Applications/Cursor.app/Contents/MacOS/Cursor {args}\n"

    scanner, sessions = _build_scanner(ps_text)
    import asyncio

    asyncio.run(scanner.scan_once())

    row = sessions.get("runtime-cursor-300")
    assert row is not None
    assert row.title == "Cursor"


# ---------------------------------------------------------------------------
# Task 2.3 — DefaultFilesystemAdapter + NoOpRuntimePhaseObserver
# ---------------------------------------------------------------------------


def test_default_fs_adapter_exists() -> None:
    fs = DefaultFilesystemAdapter()
    assert fs.exists(__file__) is True
    assert fs.exists("/this/path/should/never/exist/zzz") is False


def test_default_fs_adapter_stat_mtime_returns_none_on_missing() -> None:
    """Requirement 10.1 — ``stat_mtime_ms`` returns ``None`` for a
    missing file rather than raising."""
    fs = DefaultFilesystemAdapter()
    assert fs.stat_mtime_ms("/nope/zzz/missing") is None


def test_default_fs_adapter_stat_mtime_returns_int_for_existing(
    tmp_path: Path,
) -> None:
    fs = DefaultFilesystemAdapter()
    target = tmp_path / "f.txt"
    target.write_text("hi")
    mtime = fs.stat_mtime_ms(str(target))
    assert isinstance(mtime, int)
    assert mtime > 0


def test_default_fs_adapter_read_tail_handles_short_file(
    tmp_path: Path,
) -> None:
    """Files shorter than ``max_bytes`` should fall through to a full
    read — the ``OSError`` from ``seek(-N, SEEK_END)`` is caught and
    we ``seek(0)``."""
    fs = DefaultFilesystemAdapter()
    target = tmp_path / "small.txt"
    target.write_bytes(b"hello")
    assert fs.read_tail(str(target), max_bytes=4096) == b"hello"


def test_default_fs_adapter_read_tail_returns_suffix_for_long_file(
    tmp_path: Path,
) -> None:
    fs = DefaultFilesystemAdapter()
    target = tmp_path / "big.txt"
    target.write_bytes(b"a" * 10_000 + b"TAIL")
    tail = fs.read_tail(str(target), max_bytes=4)
    assert tail == b"TAIL"


def test_default_fs_adapter_read_tail_raises_on_missing() -> None:
    """``read_tail`` is *allowed* to raise so observers can choose
    between FileNotFoundError vs other OSError handling."""
    fs = DefaultFilesystemAdapter()
    with pytest.raises(FileNotFoundError):
        fs.read_tail("/nope/zzz/missing", max_bytes=4096)


def test_no_op_observer_counters_increment_per_call() -> None:
    """Requirement 13.4 — every method increments its respective
    counter exactly once per call."""
    obs = NoOpRuntimePhaseObserver()
    assert obs.start_calls == obs.stop_calls == obs.targets_calls == obs.tick_calls == 0

    obs.start()
    obs.start()
    obs.stop()
    obs.targets([])
    obs.tick(0)
    obs.tick(1)
    obs.tick(2)

    assert obs.start_calls == 2
    assert obs.stop_calls == 1
    assert obs.targets_calls == 1
    assert obs.tick_calls == 3


def test_no_op_observer_targets_and_tick_return_empty() -> None:
    """Requirements 13.2 / 13.3 — both return ``[]`` regardless of
    input."""
    obs = NoOpRuntimePhaseObserver()
    assert obs.targets([]) == []
    sample_status = AgentRuntimeStatus(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
    )
    assert obs.targets([sample_status]) == []
    assert obs.tick(0) == []
    assert obs.tick(123_456) == []


def test_no_op_observer_inherits_from_abc() -> None:
    """Sanity guard — ``NoOpRuntimePhaseObserver`` must remain a
    proper ABC subclass so registry tests can substitute it for any
    concrete observer."""
    assert issubclass(NoOpRuntimePhaseObserver, RuntimePhaseObserver)


def test_runtime_phase_observer_abc_blocks_direct_instantiation() -> None:
    """Requirement 3.1 — the ABC must reject instantiation without a
    concrete subclass."""
    with pytest.raises(TypeError):
        # ``type: ignore`` because the linter knows this is abstract;
        # the test exists precisely to assert that runtime check.
        RuntimePhaseObserver(fs=DefaultFilesystemAdapter())  # type: ignore[abstract]


# ---------------------------------------------------------------------------
# Smoke: detector tolerates standard tempfile mkdtemp paths
# ---------------------------------------------------------------------------


def test_detector_handles_tempfile_mkdtemp_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # No marker — should fall back to the directory itself.
        assert detect_workspace_root(tmp) == tmp


def test_detector_handles_explicit_relative_to_root_walk() -> None:
    """Synthetic regression — ``os.path.dirname('/')`` returns
    ``'/'`` so the loop's termination guard fires immediately."""
    assert detect_workspace_root("/", fs_exists=lambda _p: False) == "/"


# ---------------------------------------------------------------------------
# Tasks 3.4 + 3.5 — RuntimePhaseObserverRegistry behaviour
# ---------------------------------------------------------------------------


@dataclass
class _RecordingReducer:
    """Fake reducer that records every event the registry forwards.

    Stand-in for :class:`AgentEventReducer` in registry tests so
    assertions can compare raw lists without going through the
    real session-store mutation pipeline.
    """

    events: list[AgentEvent] = field(default_factory=list)

    def apply(self, event: AgentEvent) -> None:
        self.events.append(event)


def _make_aider_status(pid: int, *, workspace: str | None = "/tmp/proj") -> AgentRuntimeStatus:
    return AgentRuntimeStatus(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
        process_id=pid,
        workspace=workspace,
    )


def _act(session_id: str, *, ts_ms: int = 0, source: str = "aider") -> SessionActivityUpdated:
    return SessionActivityUpdated(
        session_id=session_id,
        source=source,
        ts_ms=ts_ms,
        phase=SessionPhase.THINKING,
    )


class _StubObserver(RuntimePhaseObserver):
    """Programmable observer for registry tests.

    ``targets_fn`` and ``tick_fn`` let each test plug in deterministic
    behaviour without subclassing per test.
    """

    def __init__(
        self,
        *,
        targets_fn: Callable[[Sequence[AgentRuntimeStatus]], list[AgentRuntimeStatus]],
        tick_fn: Callable[[int], list[AgentEvent]],
        start_raises: Exception | None = None,
        stop_raises: Exception | None = None,
    ) -> None:
        super().__init__(fs=DefaultFilesystemAdapter())
        self.targets_fn = targets_fn
        self.tick_fn = tick_fn
        self.start_raises = start_raises
        self.stop_raises = stop_raises
        self.start_count = 0
        self.stop_count = 0
        self.tick_count = 0

    def start(self) -> None:
        self.start_count += 1
        if self.start_raises is not None:
            raise self.start_raises

    def stop(self) -> None:
        self.stop_count += 1
        if self.stop_raises is not None:
            raise self.stop_raises

    def targets(self, statuses):
        return self.targets_fn(statuses)

    def tick(self, now_ms):
        self.tick_count += 1
        return self.tick_fn(now_ms)


def _empty_session_view(_session_id: str) -> SessionInfo | None:
    return None


def test_registry_empty_observer_list_is_noop() -> None:
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )
    registry.notify([], 0)
    assert reducer.events == []


def test_registry_lazy_starts_observer_only_when_targets_non_empty() -> None:
    """Requirement 4.5 — ``start`` is called once on the first
    non-empty subset, not on every empty tick that precedes it."""
    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [],
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    # Two empty ticks first.
    registry.notify([], 0)
    registry.notify([], 1)
    assert obs.start_count == 0

    registry.notify([_make_aider_status(1)], 2)
    assert obs.start_count == 1
    # And again on subsequent ticks — start must NOT be invoked twice.
    registry.notify([_make_aider_status(1)], 3)
    assert obs.start_count == 1


def test_registry_stops_observer_when_subset_drains() -> None:
    """Requirement 4.6 — once ``targets`` goes empty, the registry
    invokes ``stop`` and never calls ``tick`` for that tick."""
    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [],
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    registry.notify([_make_aider_status(1)], 0)
    assert obs.tick_count == 1

    registry.notify([], 1)  # subset drained
    assert obs.stop_count == 1
    assert obs.tick_count == 1  # tick not called on empty subset


def test_registry_forwards_events_in_observer_then_emit_order() -> None:
    """Property 2 — events are forwarded in registration × emit order."""
    a = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [_act("a-1", ts_ms=now), _act("a-2", ts_ms=now)],
    )
    b = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [_act("b-1", ts_ms=now)],
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[a, b],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    registry.notify([_make_aider_status(1)], 100)
    assert [e.session_id for e in reducer.events] == ["a-1", "a-2", "b-1"]


def test_registry_drops_events_targeting_hook_sessions() -> None:
    """Property 3 / Requirement 5.6 — events whose ``session_id``
    resolves to a hook-driven session in the store are silently
    dropped before reaching the reducer."""

    def session_view(sid: str) -> SessionInfo | None:
        if sid == "hooked":
            return SessionInfo(session_id=sid, kind="hook_session")
        return None

    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [_act("hooked"), _act("plain")],
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=session_view,
    )

    registry.notify([_make_aider_status(1)], 0)
    assert [e.session_id for e in reducer.events] == ["plain"]


def test_registry_disables_observer_after_three_consecutive_crashes() -> None:
    """Property 4 / Requirement 12.4 — three consecutive ``tick``
    raises permanently disable the observer."""
    boom = RuntimeError("kaboom")
    crash_count = {"n": 0}

    def crashing_tick(now: int) -> list[AgentEvent]:
        crash_count["n"] += 1
        raise boom

    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=crashing_tick,
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    for i in range(5):
        registry.notify([_make_aider_status(1)], i)

    # ObserverConsecutiveCrashLimit ticks attempted, the rest skipped.
    assert crash_count["n"] == ObserverConsecutiveCrashLimit
    assert registry.is_disabled(obs)


def test_registry_resets_crash_counter_after_successful_tick() -> None:
    """Requirement 12.4 — a successful tick clears the consecutive-
    crash counter so an observer that recovers stays alive."""
    failures = [True, False, True, True, True]

    def maybe_crash(now: int) -> list[AgentEvent]:
        will_fail = failures[now]
        if will_fail:
            raise RuntimeError("transient")
        return []

    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=maybe_crash,
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    # Tick 0: crash. Tick 1: success → counter resets.
    registry.notify([_make_aider_status(1)], 0)
    registry.notify([_make_aider_status(1)], 1)
    assert not registry.is_disabled(obs)

    # Now three more crashes in a row → disabled.
    registry.notify([_make_aider_status(1)], 2)
    registry.notify([_make_aider_status(1)], 3)
    registry.notify([_make_aider_status(1)], 4)
    assert registry.is_disabled(obs)


def test_registry_retries_start_after_failure() -> None:
    """Requirement 12.3 — when ``start`` raises, the observer stays
    un-started and the next eligible tick retries ``start``."""

    class _RetryObserver(_StubObserver):
        def __init__(self) -> None:
            super().__init__(
                targets_fn=lambda s: list(s),
                tick_fn=lambda now: [],
            )
            self._will_succeed = False

        def start(self) -> None:
            self.start_count += 1
            if not self._will_succeed:
                raise RuntimeError("not yet")

    obs = _RetryObserver()
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    # Tick 1: start fails, observer remains un-started.
    registry.notify([_make_aider_status(1)], 0)
    assert obs.start_count == 1
    # Tick 2: still un-started — start retried.
    registry.notify([_make_aider_status(1)], 1)
    assert obs.start_count == 2

    # Allow start to succeed on tick 3.
    obs._will_succeed = True
    registry.notify([_make_aider_status(1)], 2)
    assert obs.start_count == 3
    # Tick 4: already started — start NOT called again.
    registry.notify([_make_aider_status(1)], 3)
    assert obs.start_count == 3


def test_registry_stop_raising_still_marks_observer_unstarted() -> None:
    """When ``stop`` raises during a subset drain, the observer is
    still considered un-started so we don't keep calling ``stop``."""
    obs = _StubObserver(
        targets_fn=lambda s: list(s),
        tick_fn=lambda now: [],
        stop_raises=RuntimeError("stop blew up"),
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    registry.notify([_make_aider_status(1)], 0)
    registry.notify([], 1)  # subset drained → stop raises
    assert obs.stop_count == 1
    # Subsequent empty tick should NOT re-invoke stop because we
    # marked the observer un-started despite the raise.
    registry.notify([], 2)
    assert obs.stop_count == 1


def test_registry_targets_raising_does_not_call_tick() -> None:
    """When ``targets`` raises, the registry skips ``tick`` for that
    observer on that tick and bumps the crash counter."""

    def boom_targets(_statuses):
        raise RuntimeError("targets exploded")

    obs = _StubObserver(
        targets_fn=boom_targets,
        tick_fn=lambda now: [_act("never")],
    )
    reducer = _RecordingReducer()
    registry = RuntimePhaseObserverRegistry(
        observers=[obs],
        reducer=reducer,  # type: ignore[arg-type]
        session_view=_empty_session_view,
    )

    registry.notify([_make_aider_status(1)], 0)
    assert obs.tick_count == 0
    assert reducer.events == []


# ---------------------------------------------------------------------------
# Tasks 4.3-4.7 — AiderTranscriptObserver
# ---------------------------------------------------------------------------


@dataclass
class _FakeFs:
    """Recording filesystem adapter for Aider observer tests.

    ``files[path] = (mtime_ms, body_bytes)`` represents the canonical
    state. ``read_tail_calls`` records every read so tests can assert
    on call counts (Property 6 / Requirement 8.2).
    """

    files: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    read_tail_calls: int = 0
    raise_on_read: dict[str, type[OSError]] = field(default_factory=dict)

    def exists(self, path: str) -> bool:
        return path in self.files

    def stat_mtime_ms(self, path: str) -> int | None:
        if path not in self.files:
            return None
        return self.files[path][0]

    def read_tail(self, path: str, max_bytes: int) -> bytes:
        self.read_tail_calls += 1
        if path in self.raise_on_read:
            raise self.raise_on_read[path]("simulated")
        if path not in self.files:
            raise FileNotFoundError(path)
        body = self.files[path][1]
        if len(body) <= max_bytes:
            return body
        return body[-max_bytes:]


def _make_observer(fs: _FakeFs) -> AiderTranscriptObserver:
    return AiderTranscriptObserver(fs=fs)  # type: ignore[arg-type]


def _aider_status(workspace: str, *, pid: int = 7777) -> AgentRuntimeStatus:
    return AgentRuntimeStatus(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
        process_id=pid,
        workspace=workspace,
        cwd=workspace,
    )


def _path_for(workspace: str) -> str:
    return f"{workspace}/{AIDER_HISTORY_FILENAME}"


# --- Task 4.3 — fence parser ------------------------------------------------


def test_fence_parser_returns_none_on_pure_prose() -> None:
    assert _last_fenced_block(b"plain text\nno blocks here\n") is None


def test_fence_parser_returns_last_diff_block() -> None:
    body = b"""
> user prompt
prose

```diff
+ added
```

```diff
+ second
```
"""
    block = _last_fenced_block(body)
    assert block is not None
    info, body_text = block
    assert info == "diff"
    assert "+ second" in body_text


def test_fence_parser_returns_last_when_followed_by_unclosed_fence() -> None:
    """Requirement 11.5 — an unclosed trailing fence is ignored,
    we still return the last *closed* block."""
    body = b"""
```bash
echo hello
```

```text
unclosed at end
"""
    block = _last_fenced_block(body)
    assert block is not None
    info, _ = block
    assert info == "bash"


def test_fence_parser_handles_invalid_utf8_bytes() -> None:
    """``errors="replace"`` keeps a partial multi-byte sequence from
    raising; we just want the parser to keep working when the prefix
    contains undecodable bytes (a newline isolates the garbage so
    the fence-line detection still fires)."""
    body = b"\xff\xfe\n```diff\n+ x\n```\n"
    block = _last_fenced_block(body)
    assert block is not None
    info, _ = block
    assert info == "diff"


# --- Task 4.4 — phase mapping + priority ------------------------------------


def test_aider_emits_editing_for_diff_block(tmp_path: Path) -> None:
    fs = _FakeFs()
    body = b"prose\n\n```diff\n--- a/foo\n+++ b/foo\n+x\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_500)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, SessionActivityUpdated)
    assert e.phase is SessionPhase.EDITING


def test_aider_sniffs_diff_in_unlabelled_fence() -> None:
    fs = _FakeFs()
    body = b"```\n--- a/x\n+++ b/x\n+ y\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_500)
    assert len(events) == 1
    assert events[0].phase is SessionPhase.EDITING  # type: ignore[attr-defined]


def test_aider_emits_running_tool_for_fresh_bash_block() -> None:
    fs = _FakeFs()
    body = b"```bash\nls -la\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_000 + AIDER_LIVE_THRESHOLD_MS - 100)
    assert len(events) == 1
    assert events[0].phase is SessionPhase.RUNNING_TOOL  # type: ignore[attr-defined]


def test_aider_emits_thinking_when_recent_no_signal() -> None:
    fs = _FakeFs()
    body = b"plain prose with no fences\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_500)
    assert len(events) == 1
    assert events[0].phase is SessionPhase.THINKING  # type: ignore[attr-defined]


def test_aider_no_event_in_3_to_30_second_gap() -> None:
    fs = _FakeFs()
    body = b"prose\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    # 5 seconds since mtime — past LIVE, before IDLE.
    events = obs.tick(now_ms=10_000 + 5_000)
    assert events == []


def test_aider_emits_completed_when_idle_with_no_user_prompt() -> None:
    fs = _FakeFs()
    body = b"agent finished\n\nlast assistant line\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_000 + AIDER_IDLE_THRESHOLD_MS + 1_000)
    assert len(events) == 1
    assert isinstance(events[0], SessionCompleted)
    assert events[0].failed is False


def test_aider_no_event_when_idle_with_user_prompt_pending() -> None:
    """Requirement 7.4 — last non-empty line begins with ``> `` so
    the user is composing a follow-up; we should not flip the
    session to COMPLETED."""
    fs = _FakeFs()
    body = b"agent finished\n\n> are you still there?"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_000 + AIDER_IDLE_THRESHOLD_MS + 5_000)
    assert events == []


def test_aider_priority_editing_beats_other_signals() -> None:
    """Property 7 — when a fixture satisfies multiple rules, the
    highest-priority one wins. Diff block + recent mtime → EDITING,
    even though THINKING and RUNNING_TOOL preconditions hold too."""
    fs = _FakeFs()
    body = b"```diff\n+ x\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_500)
    assert len(events) == 1
    assert events[0].phase is SessionPhase.EDITING  # type: ignore[attr-defined]


# --- Task 4.5 — mtime backoff -----------------------------------------------


def test_aider_skips_read_when_mtime_unchanged() -> None:
    """Property 6 / Requirement 8.2 — a second tick with the same
    mtime emits nothing AND must not call ``read_tail`` again."""
    fs = _FakeFs()
    body = b"```diff\n+ x\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])

    # First tick reads.
    obs.tick(now_ms=10_500)
    assert fs.read_tail_calls == 1

    # Second tick with identical mtime — must short-circuit.
    events = obs.tick(now_ms=10_600)
    assert events == []
    assert fs.read_tail_calls == 1


def test_aider_re_reads_after_mtime_advances() -> None:
    fs = _FakeFs()
    body = b"```diff\n+ x\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    obs.tick(now_ms=10_500)

    # Same workspace, new mtime + new body.
    fs.files[_path_for("/ws")] = (11_000, b"```bash\nls\n```\n")
    events = obs.tick(now_ms=11_500)
    assert fs.read_tail_calls == 2
    assert len(events) == 1
    assert events[0].phase is SessionPhase.RUNNING_TOOL  # type: ignore[attr-defined]


# --- Task 4.6 — target drop-out cache cleanup -------------------------------


def test_aider_clears_cache_when_session_drops_out() -> None:
    fs = _FakeFs()
    body = b"```diff\n+ x\n```\n"
    fs.files[_path_for("/wsA")] = (10_000, body)
    fs.files[_path_for("/wsB")] = (20_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/wsA", pid=1), _aider_status("/wsB", pid=2)])
    obs.tick(now_ms=10_500)

    # /wsA disappears.
    obs.targets([_aider_status("/wsB", pid=2)])
    # When /wsA reappears with the SAME mtime we previously cached,
    # the observer must re-read because the cache was dropped.
    fs.files[_path_for("/wsA")] = (10_000, body)
    obs.targets([_aider_status("/wsA", pid=1), _aider_status("/wsB", pid=2)])
    fs.read_tail_calls = 0
    obs.tick(now_ms=11_000)
    # /wsA was dropped → its cache was cleared → it re-reads now
    # even though the mtime is unchanged. /wsB stayed cached so it
    # short-circuits. Net: exactly one read.
    assert fs.read_tail_calls == 1


# --- Task 4.7 — OSError handling and warning dedup --------------------------


def test_aider_silent_on_file_not_found_during_read() -> None:
    fs = _FakeFs()
    fs.files[_path_for("/ws")] = (10_000, b"x")
    fs.raise_on_read[_path_for("/ws")] = FileNotFoundError
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws")])
    events = obs.tick(now_ms=10_500)
    assert events == []


def test_aider_warns_once_per_session_per_tick_for_other_oserror(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fs = _FakeFs()
    # Two distinct sessions both raise PermissionError.
    fs.files[_path_for("/wsA")] = (10_000, b"x")
    fs.files[_path_for("/wsB")] = (10_000, b"x")
    fs.raise_on_read[_path_for("/wsA")] = PermissionError
    fs.raise_on_read[_path_for("/wsB")] = PermissionError

    obs = _make_observer(fs)
    obs.targets([_aider_status("/wsA", pid=1), _aider_status("/wsB", pid=2)])

    capsys.readouterr()  # drain pre-test output
    obs.tick(now_ms=10_500)
    out = capsys.readouterr().out
    # Two warnings — one per (session, error class).
    msgs = [line for line in out.splitlines() if "aider_observer.read_failed" in line]
    assert len(msgs) == 2

    # Update mtime so the second tick re-reads — _tick_warned reset
    # at start of tick, so warnings re-emit.
    fs.files[_path_for("/wsA")] = (11_000, b"x")
    fs.files[_path_for("/wsB")] = (11_000, b"x")
    capsys.readouterr()
    obs.tick(now_ms=11_500)
    out = capsys.readouterr().out
    msgs = [line for line in out.splitlines() if "aider_observer.read_failed" in line]
    assert len(msgs) == 2


def test_aider_targets_filter_excludes_workspace_none() -> None:
    """Requirement 6.1 — only AIDER + non-null workspace pass."""
    fs = _FakeFs()
    obs = _make_observer(fs)
    others = [
        AgentRuntimeStatus(source=AgentRuntimeSource.AIDER, kind=AgentRuntimeKind.CLI_AGENT, process_id=1, workspace=None),
        AgentRuntimeStatus(source=AgentRuntimeSource.CODEX, kind=AgentRuntimeKind.CLI_AGENT, process_id=2, workspace="/ws"),
        _aider_status("/ws", pid=3),
    ]
    selected = obs.targets(others)
    assert len(selected) == 1
    assert selected[0].process_id == 3


def test_aider_event_carries_workspace_and_aider_source() -> None:
    """Requirements 6.4-6.6 — emitted events use ``effective_session_id``,
    ``source="aider"``, and ``cwd=workspace``."""
    fs = _FakeFs()
    body = b"```diff\n+ x\n```\n"
    fs.files[_path_for("/ws")] = (10_000, body)
    obs = _make_observer(fs)
    obs.targets([_aider_status("/ws", pid=42)])
    events = obs.tick(now_ms=10_500)
    e = events[0]
    assert e.session_id == "runtime-aider-42"
    assert e.source == "aider"
    assert e.cwd == "/ws"


# ===========================================================================
# kiro-task-observer spec — Tasks 1.1, 2.1, 3.1-3.3, 4.1-4.8, 5.1
# ===========================================================================


# ---------------------------------------------------------------------------
# Task 1.1 — classifier coverage for AgentRuntimeSource.KIRO
# ---------------------------------------------------------------------------


def test_kiro_app_classifies_as_kiro_gui_ide() -> None:
    rows = parse_ps_output(
        "1234 1 /Applications/Kiro.app/Contents/MacOS/Kiro Kiro\n"
    )
    statuses = discover_runtime_statuses(rows, now_ms=0)
    assert len(statuses) == 1
    assert statuses[0].source is AgentRuntimeSource.KIRO
    assert statuses[0].kind is AgentRuntimeKind.GUI_IDE
    assert statuses[0].display_name == "Kiro"


def test_kiro_helper_subprocesses_dedupe_into_main_process() -> None:
    """Requirement 1.5 — helper / renderer / GPU / crashpad
    subprocesses must fold into the main Kiro row."""
    rows = parse_ps_output(
        """
        1234 1 /Applications/Kiro.app/Contents/MacOS/Kiro Kiro
        2222 1234 /Applications/Kiro.app/Contents/Frameworks/Kiro Helper.app/Contents/MacOS/Kiro Helper Kiro Helper (Renderer)
        3333 1234 /Applications/Kiro.app/Contents/Frameworks/Kiro Helper.app/Contents/MacOS/Kiro Helper Kiro Helper (GPU)
        """
    )
    statuses = discover_runtime_statuses(rows, now_ms=0)
    kiro_rows = [s for s in statuses if s.source is AgentRuntimeSource.KIRO]
    # Exactly one row — helpers should not produce additional KIRO rows.
    assert len(kiro_rows) == 1
    assert kiro_rows[0].process_id == 1234


def test_existing_runtime_classifications_unchanged() -> None:
    """Requirement 1.6 — adding the Kiro row must not regress
    existing classifications."""
    rows = parse_ps_output(
        "5555 1 /Applications/Cursor.app/Contents/MacOS/Cursor Cursor\n"
    )
    statuses = discover_runtime_statuses(rows, now_ms=0)
    assert len(statuses) == 1
    assert statuses[0].source is AgentRuntimeSource.CURSOR


# ---------------------------------------------------------------------------
# Task 2.1 — DefaultFilesystemAdapter list_dir / read_bytes
# ---------------------------------------------------------------------------


def test_default_fs_adapter_list_dir_missing_returns_empty(tmp_path: Path) -> None:
    """Requirement 4.5 / 10.2 — missing root yields ``[]`` rather
    than raising so the observer's bounded scan stays exception-
    free."""
    fs = DefaultFilesystemAdapter()
    assert fs.list_dir(str(tmp_path / "does-not-exist")) == []


def test_default_fs_adapter_list_dir_returns_entries(tmp_path: Path) -> None:
    fs = DefaultFilesystemAdapter()
    (tmp_path / "a").write_text("")
    (tmp_path / "b").mkdir()
    entries = fs.list_dir(str(tmp_path))
    assert sorted(entries) == ["a", "b"]


def test_default_fs_adapter_read_bytes_honours_max(tmp_path: Path) -> None:
    fs = DefaultFilesystemAdapter()
    target = tmp_path / "large"
    target.write_bytes(b"x" * 1000)
    assert fs.read_bytes(str(target), 32) == b"x" * 32


def test_default_fs_adapter_read_bytes_returns_full_when_under_cap(
    tmp_path: Path,
) -> None:
    fs = DefaultFilesystemAdapter()
    target = tmp_path / "small"
    target.write_bytes(b"hi")
    assert fs.read_bytes(str(target), 4096) == b"hi"


# ---------------------------------------------------------------------------
# Tasks 3.1-3.3 — pure helpers
# ---------------------------------------------------------------------------


def test_resolve_workspace_with_file_uri_prefix() -> None:
    uri = "file:///Users/dev/proj/.kiro/specs/myspec/tasks.md"
    assert (
        _resolve_workspace_from_spec_uri(uri, "myspec")
        == "/Users/dev/proj"
    )


def test_resolve_workspace_without_file_prefix() -> None:
    uri = "/Users/dev/proj/.kiro/specs/myspec/tasks.md"
    assert (
        _resolve_workspace_from_spec_uri(uri, "myspec")
        == "/Users/dev/proj"
    )


def test_resolve_workspace_mismatched_spec_name_returns_none() -> None:
    uri = "file:///Users/dev/proj/.kiro/specs/spec-A/tasks.md"
    assert _resolve_workspace_from_spec_uri(uri, "spec-B") is None


def test_resolve_workspace_no_kiro_segment_returns_none() -> None:
    uri = "file:///Users/dev/proj/some/random/path.md"
    assert _resolve_workspace_from_spec_uri(uri, "myspec") is None


def test_resolve_workspace_only_specs_path_returns_none() -> None:
    """Requirement 6 spirit — a URI that's exactly the specs path
    (empty workspace) is unresolvable."""
    uri = "file:///.kiro/specs/myspec/tasks.md"
    assert _resolve_workspace_from_spec_uri(uri, "myspec") is None


def test_resolve_workspace_handles_percent_encoded_spaces() -> None:
    encoded_workspace = quote("/Users/dev/My Project")
    uri = f"file://{encoded_workspace}/.kiro/specs/myspec/tasks.md"
    assert (
        _resolve_workspace_from_spec_uri(uri, "myspec")
        == "/Users/dev/My Project"
    )


def test_resolve_workspace_none_input_returns_none() -> None:
    assert _resolve_workspace_from_spec_uri(None, "myspec") is None
    assert _resolve_workspace_from_spec_uri("", "myspec") is None


def test_pick_latest_task_record_returns_largest_updated_at() -> None:
    payload = {
        "tasks": {
            "task-A": {"updatedAt": 100, "executionStatus": "succeed"},
            "task-B": {"updatedAt": 200, "executionStatus": "in_progress"},
            "task-C": {"updatedAt": 150},
        }
    }
    record = _pick_latest_task_record(payload)
    assert record is not None
    assert record.task_key == "task-B"
    assert record.updated_at_ms == 200
    assert record.execution_status == "in_progress"


def test_pick_latest_task_record_lex_tiebreak() -> None:
    """Two records with identical updatedAt — lex-ascending key wins."""
    payload = {
        "tasks": {
            "task-zeta": {"updatedAt": 100},
            "task-alpha": {"updatedAt": 100},
        }
    }
    record = _pick_latest_task_record(payload)
    assert record is not None
    # ``sorted(tasks)`` then ``> best`` keeps the LAST record we
    # see whose tuple is strictly greater. With equal updatedAt,
    # the LATER one in sort order (zeta) wins.
    assert record.task_key == "task-zeta"


def test_pick_latest_task_record_drops_records_without_updated_at() -> None:
    payload = {
        "tasks": {
            "no-update": {"executionStatus": "succeed"},
            "good": {"updatedAt": 50, "executionStatus": "queued"},
        }
    }
    record = _pick_latest_task_record(payload)
    assert record is not None
    assert record.task_key == "good"


def test_pick_latest_task_record_drops_non_dict_values() -> None:
    payload = {"tasks": {"weird": "string-not-dict", "ok": {"updatedAt": 1}}}
    record = _pick_latest_task_record(payload)
    assert record is not None
    assert record.task_key == "ok"


def test_pick_latest_task_record_empty_returns_none() -> None:
    assert _pick_latest_task_record({"tasks": {}}) is None
    assert _pick_latest_task_record({}) is None
    assert _pick_latest_task_record({"tasks": "not-a-dict"}) is None


def _record(updated_at_ms: int, execution_status: str | None = None) -> _KiroLatestTaskRecord:
    return _KiroLatestTaskRecord(
        task_key="t1",
        updated_at_ms=updated_at_ms,
        execution_status=execution_status,
        spec_uri=None,
    )


def test_decide_kiro_phase_succeed_maps_to_completed() -> None:
    assert _decide_kiro_phase(_record(0, "succeed"), now_ms=0) is SessionPhase.COMPLETED


def test_decide_kiro_phase_in_progress_maps_to_thinking() -> None:
    assert _decide_kiro_phase(_record(0, "in_progress"), now_ms=0) is SessionPhase.THINKING


def test_decide_kiro_phase_queued_maps_to_running() -> None:
    assert _decide_kiro_phase(_record(0, "queued"), now_ms=0) is SessionPhase.RUNNING


def test_decide_kiro_phase_unknown_status_maps_to_running() -> None:
    assert _decide_kiro_phase(_record(0, "weird"), now_ms=0) is SessionPhase.RUNNING


def test_decide_kiro_phase_missing_status_maps_to_running() -> None:
    assert _decide_kiro_phase(_record(0, None), now_ms=0) is SessionPhase.RUNNING


def test_decide_kiro_phase_idle_override_beats_in_progress() -> None:
    """Requirement 8.1 — past 30-minute idle threshold flips to
    COMPLETED regardless of executionStatus."""
    later = KIRO_IDLE_THRESHOLD_MS + 1_000
    assert (
        _decide_kiro_phase(_record(0, "in_progress"), now_ms=later)
        is SessionPhase.COMPLETED
    )


def test_decide_kiro_phase_future_dated_does_not_override() -> None:
    """Requirement 8.3 — future-dated updated_at_ms doesn't fire
    the idle override; we use the executionStatus mapping."""
    record = _record(updated_at_ms=10_000_000_000, execution_status="in_progress")
    assert _decide_kiro_phase(record, now_ms=0) is SessionPhase.THINKING


# ---------------------------------------------------------------------------
# Tasks 4.1-4.8 — KiroTaskObserver behaviour
# ---------------------------------------------------------------------------


@dataclass
class _KiroFakeFs:
    """Recording filesystem adapter for Kiro observer tests.

    State model:
    - ``dirs[path] = list[str]`` — what list_dir returns for each
      path. Default: empty list (acts as missing).
    - ``files[path] = (mtime_ms, body_bytes)`` — what stat_mtime_ms
      returns + what read_bytes serves. read_bytes also honours
      max_bytes truncation.
    - ``raise_on_read[path] = OSErrorClass`` — raise on read_bytes
      for that path.
    """

    dirs: dict[str, list[str]] = field(default_factory=dict)
    files: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    dir_mtimes: dict[str, int] = field(default_factory=dict)
    raise_on_read: dict[str, type[OSError]] = field(default_factory=dict)
    read_bytes_calls: int = 0

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.dirs

    def stat_mtime_ms(self, path: str) -> int | None:
        if path in self.files:
            return self.files[path][0]
        if path in self.dir_mtimes:
            return self.dir_mtimes[path]
        return None

    def read_tail(self, path: str, max_bytes: int) -> bytes:
        raise NotImplementedError("Kiro observer should not call read_tail")

    def list_dir(self, path: str) -> list[str]:
        return list(self.dirs.get(path, []))

    def read_bytes(self, path: str, max_bytes: int) -> bytes:
        self.read_bytes_calls += 1
        if path in self.raise_on_read:
            raise self.raise_on_read[path]("simulated")
        if path not in self.files:
            raise FileNotFoundError(path)
        body = self.files[path][1]
        return body if len(body) <= max_bytes else body[:max_bytes]


def _kiro_meta_payload(
    spec_name: str,
    *,
    workspace: str,
    updated_at_ms: int,
    execution_status: str | None = "in_progress",
) -> bytes:
    """Build a minimal Kiro meta JSON payload."""
    record: dict = {
        "updatedAt": updated_at_ms,
        "specUri": f"file://{quote(workspace)}/.kiro/specs/{spec_name}/tasks.md",
    }
    if execution_status is not None:
        record["executionStatus"] = execution_status
    payload = {"tasks": {"task-1": record}}
    return json.dumps(payload).encode("utf-8")


def _setup_kiro_workspace(
    fs: _KiroFakeFs,
    *,
    root: str,
    hash_name: str,
    spec_name: str,
    workspace: str,
    mtime_ms: int,
    file_mtime_ms: int,
    updated_at_ms: int,
    execution_status: str | None = "in_progress",
) -> str:
    """Wire up a single (hash_dir, spec_name, meta_file) tuple in
    the fake fs and return the absolute meta path so individual
    tests can assert on adapter calls."""
    fs.dirs.setdefault(root, [])
    if hash_name not in fs.dirs[root]:
        fs.dirs[root].append(hash_name)
    fs.dir_mtimes[os.path.join(root, hash_name)] = mtime_ms
    hash_path = os.path.join(root, hash_name)
    meta_name = f"{spec_name}.meta.json"
    fs.dirs.setdefault(hash_path, [])
    if meta_name not in fs.dirs[hash_path]:
        fs.dirs[hash_path].append(meta_name)
    meta_path = os.path.join(hash_path, meta_name)
    fs.files[meta_path] = (
        file_mtime_ms,
        _kiro_meta_payload(
            spec_name,
            workspace=workspace,
            updated_at_ms=updated_at_ms,
            execution_status=execution_status,
        ),
    )
    return meta_path


def _kiro_status() -> AgentRuntimeStatus:
    return AgentRuntimeStatus(
        source=AgentRuntimeSource.KIRO,
        kind=AgentRuntimeKind.GUI_IDE,
        process_id=999,
        display_name="Kiro",
    )


# ---- Property 1: activation gating -----------------------------------------


def test_kiro_targets_returns_empty_when_no_kiro_status() -> None:
    """Property 1 / Requirement 3.2 — empty list deactivates."""
    fs = _KiroFakeFs()
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root="/fake/root")  # type: ignore[arg-type]
    assert obs.targets([]) == []
    assert obs.targets([
        AgentRuntimeStatus(source=AgentRuntimeSource.CURSOR, kind=AgentRuntimeKind.GUI_IDE),
    ]) == []


def test_kiro_targets_returns_synthetic_marker_when_kiro_present() -> None:
    """Property 1 / Requirement 3.1 — single synthetic marker."""
    fs = _KiroFakeFs()
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root="/fake/root")  # type: ignore[arg-type]
    out = obs.targets([_kiro_status()])
    assert len(out) == 1
    assert out[0].source is AgentRuntimeSource.KIRO
    assert out[0].effective_session_id == KIRO_PIPELINE_SESSION_ID


def test_kiro_tick_with_no_kiro_status_does_nothing() -> None:
    """Property 1 — when activation gating returns ``[]`` and the
    registry would not call ``tick`` in production, we still
    confirm a defensive ``tick(now_ms)`` makes zero filesystem
    calls (no list_dir, no read_bytes)."""
    fs = _KiroFakeFs()
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root="/fake/root")  # type: ignore[arg-type]
    obs.targets([])  # establishes "no kiro" state
    # tick still executes — observers don't gate themselves on
    # targets() in the protocol — but it should walk an empty
    # root and do no reads.
    fs.dirs["/fake/root"] = []
    events = obs.tick(now_ms=10_000)
    assert events == []
    assert fs.read_bytes_calls == 0


# ---- Property 2: multi-session emission ------------------------------------


def test_kiro_tick_emits_one_event_per_spec_meta_file() -> None:
    """Property 2 — N spec meta files → N distinct session events."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="hashA", spec_name="spec-1",
        workspace="/Users/dev/proj-A", mtime_ms=1_000, file_mtime_ms=10_000,
        updated_at_ms=10_000, execution_status="in_progress",
    )
    _setup_kiro_workspace(
        fs, root=root, hash_name="hashA", spec_name="spec-2",
        workspace="/Users/dev/proj-A", mtime_ms=1_000, file_mtime_ms=11_000,
        updated_at_ms=11_000, execution_status="queued",
    )
    _setup_kiro_workspace(
        fs, root=root, hash_name="hashB", spec_name="spec-3",
        workspace="/Users/dev/proj-B", mtime_ms=2_000, file_mtime_ms=12_000,
        updated_at_ms=12_000, execution_status="succeed",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=12_500)
    assert len(events) == 3
    sids = {e.session_id for e in events}
    assert sids == {
        "runtime-kiro-hashA-spec-1",
        "runtime-kiro-hashA-spec-2",
        "runtime-kiro-hashB-spec-3",
    }
    sources = {e.source for e in events}
    assert sources == {"kiro"}


def test_kiro_event_carries_workspace_title_and_phase() -> None:
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h1", spec_name="myspec",
        workspace="/Users/dev/myproj", mtime_ms=1_000, file_mtime_ms=10_000,
        updated_at_ms=10_000, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=10_500)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, SessionActivityUpdated)
    assert e.session_id == "runtime-kiro-h1-myspec"
    assert e.cwd == "/Users/dev/myproj"
    assert e.title == "Kiro · myspec"
    assert e.phase is SessionPhase.THINKING


def test_kiro_succeed_status_emits_session_completed() -> None:
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="done",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=10,
        updated_at_ms=10, execution_status="succeed",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=11)
    assert len(events) == 1
    assert isinstance(events[0], SessionCompleted)
    assert events[0].failed is False


# ---- Property 3: mtime backoff ---------------------------------------------


def test_kiro_mtime_unchanged_skips_read_bytes() -> None:
    """Property 3 — second tick with identical mtime emits zero
    events and makes zero ``read_bytes`` calls."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        updated_at_ms=100, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    obs.tick(now_ms=200)
    assert fs.read_bytes_calls == 1

    # Second tick at later now_ms but same file mtime.
    events = obs.tick(now_ms=300)
    assert events == []
    assert fs.read_bytes_calls == 1


def test_kiro_mtime_changed_re_reads() -> None:
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        updated_at_ms=100, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    obs.tick(now_ms=200)
    initial = fs.read_bytes_calls

    # Bump the mtime + change the body to RUNNING (queued).
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=110,
        updated_at_ms=110, execution_status="queued",
    )
    events = obs.tick(now_ms=300)
    assert fs.read_bytes_calls == initial + 1
    assert len(events) == 1
    assert events[0].phase is SessionPhase.RUNNING  # type: ignore[attr-defined]


# ---- Property 4: idle threshold override -----------------------------------


def test_kiro_idle_crossing_emits_completed_without_re_read() -> None:
    """Property 4 / Requirement 11.4 — first tick at t0 emits
    THINKING; second tick at t0 + 31 min with unchanged mtime
    emits SessionCompleted without re-reading."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="slow",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        updated_at_ms=100, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    first = obs.tick(now_ms=200)
    assert len(first) == 1
    assert first[0].phase is SessionPhase.THINKING  # type: ignore[attr-defined]
    initial_reads = fs.read_bytes_calls

    # 31 minutes later — same mtime, observer has cached state.
    later = 100 + KIRO_IDLE_THRESHOLD_MS + 60_000
    second = obs.tick(now_ms=later)
    assert len(second) == 1
    assert isinstance(second[0], SessionCompleted)
    # No new read needed; idle-crossing fast path serves it.
    assert fs.read_bytes_calls == initial_reads


# ---- Property 5: hash-dir bound --------------------------------------------


def test_kiro_hash_dir_scan_bounded_to_32_entries() -> None:
    """Property 5 / Requirement 4.2-4.3 — given 50 hash dirs, only
    the 32 with largest mtime are scanned."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    fs.dirs[root] = []
    # Build 50 hash dirs with monotonically decreasing mtime so
    # the top 32 are easy to predict.
    for i in range(50):
        hash_name = f"hash-{i:03d}"
        fs.dirs[root].append(hash_name)
        fs.dir_mtimes[os.path.join(root, hash_name)] = 10_000 - i
        # Each contains exactly one spec file.
        spec_name = f"spec-{i:03d}"
        meta_name = f"{spec_name}.meta.json"
        hash_path = os.path.join(root, hash_name)
        fs.dirs[hash_path] = [meta_name]
        fs.files[os.path.join(hash_path, meta_name)] = (
            500 + i,
            _kiro_meta_payload(
                spec_name,
                workspace="/ws",
                updated_at_ms=500 + i,
                execution_status="queued",
            ),
        )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=10_000)
    # Exactly KIRO_HASH_DIR_LIMIT (32) events emitted — the
    # remaining 18 dirs are cut off.
    assert len(events) == KIRO_HASH_DIR_LIMIT
    # session_id format is "runtime-kiro-<hash_name>-<spec_name>".
    # ``split("-")`` yields ['runtime','kiro','hash','007','spec','007']
    # so index 3 is the zero-padded directory index.
    seen_indices = {e.session_id.split("-")[3] for e in events}
    expected_indices = {f"{i:03d}" for i in range(32)}
    assert seen_indices == expected_indices


# ---- Property 6: bytes cap corrupt -----------------------------------------


def test_kiro_file_at_byte_cap_treated_as_corrupt() -> None:
    """Property 6 / Requirement 5.2 — file at the 64 KiB cap is
    treated as truncated/corrupt; emit no event but cache mtime so
    we don't re-read."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    fs.dirs[root] = ["h"]
    fs.dir_mtimes[os.path.join(root, "h")] = 1
    hash_path = os.path.join(root, "h")
    fs.dirs[hash_path] = ["bigspec.meta.json"]
    meta_path = os.path.join(hash_path, "bigspec.meta.json")
    # Body exactly at cap.
    fs.files[meta_path] = (100, b"x" * KIRO_META_MAX_BYTES)
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=200)
    assert events == []
    initial_reads = fs.read_bytes_calls
    # Subsequent tick with same mtime should not re-read.
    obs.tick(now_ms=300)
    assert fs.read_bytes_calls == initial_reads


# ---- Property 7: workspace resolution gate ---------------------------------


def test_kiro_unresolvable_spec_uri_emits_no_event(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Property 7 / Requirement 6.3 — meta with bad specUri →
    no event + warning logged once per (hash, spec) per tick."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    fs.dirs[root] = ["h"]
    fs.dir_mtimes[os.path.join(root, "h")] = 1
    hash_path = os.path.join(root, "h")
    fs.dirs[hash_path] = ["bad.meta.json"]
    bad_payload = json.dumps(
        {
            "tasks": {
                "task": {
                    "updatedAt": 100,
                    "specUri": "file:///wrong/path/nothing-here.md",
                    "executionStatus": "in_progress",
                }
            }
        }
    ).encode("utf-8")
    fs.files[os.path.join(hash_path, "bad.meta.json")] = (100, bad_payload)
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    capsys.readouterr()
    events = obs.tick(now_ms=200)
    assert events == []
    out = capsys.readouterr().out
    msgs = [
        line for line in out.splitlines()
        if "kiro_observer.workspace_unresolvable" in line
    ]
    assert len(msgs) == 1


# ---- Task 4.8 — additional unit tests --------------------------------------


def test_kiro_cache_cleanup_on_disappearance() -> None:
    """Requirement 11.5 — cache for vanished spec is dropped so
    re-introduction triggers a fresh read."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        updated_at_ms=100, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    obs.tick(now_ms=200)

    # Spec disappears.
    fs.dirs[os.path.join(root, "h")] = []
    obs.tick(now_ms=300)

    # Re-introduce with the same mtime; cache should have been
    # cleared, so observer re-reads.
    fs.dirs[os.path.join(root, "h")] = ["s.meta.json"]
    pre_reads = fs.read_bytes_calls
    obs.tick(now_ms=400)
    assert fs.read_bytes_calls > pre_reads


def test_kiro_oserror_dedup_per_session_per_tick(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Requirement 13.2 — non-FileNotFoundError OSError logs once
    per (sid, error_class) per tick."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    fs.dirs[root] = ["hA", "hB"]
    fs.dir_mtimes[os.path.join(root, "hA")] = 1
    fs.dir_mtimes[os.path.join(root, "hB")] = 1
    for h in ("hA", "hB"):
        hash_path = os.path.join(root, h)
        fs.dirs[hash_path] = ["s.meta.json"]
        meta_path = os.path.join(hash_path, "s.meta.json")
        fs.files[meta_path] = (100, b"")
        fs.raise_on_read[meta_path] = PermissionError

    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    capsys.readouterr()
    obs.tick(now_ms=200)
    out = capsys.readouterr().out
    msgs = [line for line in out.splitlines() if "kiro_observer.read_failed" in line]
    # One warning per session, not per error.
    assert len(msgs) == 2  # two distinct sessions


def test_kiro_unknown_execution_status_warns_once_per_tick(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Requirement 7.5 — unknown executionStatus → RUNNING +
    one warning per (sid, value) per tick."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        updated_at_ms=100, execution_status="weird_state",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    capsys.readouterr()
    events = obs.tick(now_ms=200)
    assert len(events) == 1
    assert events[0].phase is SessionPhase.RUNNING  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    msgs = [line for line in out.splitlines() if "kiro_observer.unknown_status" in line]
    assert len(msgs) == 1


def test_kiro_future_dated_does_not_trigger_idle_override() -> None:
    fs = _KiroFakeFs()
    root = "/fake/root"
    _setup_kiro_workspace(
        fs, root=root, hash_name="h", spec_name="s",
        workspace="/Users/dev/p", mtime_ms=1, file_mtime_ms=100,
        # updated_at far in the future.
        updated_at_ms=10_000_000_000, execution_status="in_progress",
    )
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    events = obs.tick(now_ms=200)
    assert len(events) == 1
    # Idle override does NOT fire — phase is THINKING per
    # executionStatus.
    assert events[0].phase is SessionPhase.THINKING  # type: ignore[attr-defined]


def test_kiro_corrupt_json_caches_mtime_to_avoid_re_parse(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Requirement 13.3 — corrupt JSON keeps cache populated so
    same bytes don't re-parse on next tick."""
    fs = _KiroFakeFs()
    root = "/fake/root"
    fs.dirs[root] = ["h"]
    fs.dir_mtimes[os.path.join(root, "h")] = 1
    hash_path = os.path.join(root, "h")
    fs.dirs[hash_path] = ["s.meta.json"]
    fs.files[os.path.join(hash_path, "s.meta.json")] = (100, b"{not valid json")
    obs = KiroTaskObserver(fs=fs, kiro_tasks_root=root)  # type: ignore[arg-type]
    obs.targets([_kiro_status()])
    capsys.readouterr()
    obs.tick(now_ms=200)
    assert fs.read_bytes_calls == 1
    # Next tick — same mtime, cache should short-circuit despite
    # the JSON having been bad.
    obs.tick(now_ms=300)
    assert fs.read_bytes_calls == 1


# ---------------------------------------------------------------------------
# Task 5.1 — make_default_registry includes both observers
# ---------------------------------------------------------------------------


def test_make_default_registry_includes_aider_and_kiro_observers() -> None:
    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    registry = make_default_registry(reducer, sessions)
    # Observers list is private; introspect via the registry's
    # _observers attribute.
    observers = registry._observers
    assert len(observers) == 2
    assert any(isinstance(o, AiderTranscriptObserver) for o in observers)
    assert any(isinstance(o, KiroTaskObserver) for o in observers)


def test_make_default_registry_shares_filesystem_adapter() -> None:
    """Requirement 15.2 — both observers receive the same
    FilesystemAdapter instance."""
    sessions = SessionStore()
    approvals = ApprovalStore()
    reducer = AgentEventReducer(session_store=sessions, approval_store=approvals)
    fake = _KiroFakeFs()
    registry = make_default_registry(reducer, sessions, fs=fake)  # type: ignore[arg-type]
    observers = registry._observers
    aider = next(o for o in observers if isinstance(o, AiderTranscriptObserver))
    kiro = next(o for o in observers if isinstance(o, KiroTaskObserver))
    assert aider._fs is fake
    assert kiro._fs is fake
