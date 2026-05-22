"""Unit tests for the V10 runtime-phase-observers framework.

Covers Tasks 1.3 (WorkspaceRootDetector), 1.4 (title format), and
2.3 (filesystem adapter + no-op observer). Phase-mapping tests for
the AiderTranscriptObserver land in tasks 4.3-4.7 in the same file.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from deskmate_agent.agent_events import (
    AgentEvent,
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
)
from deskmate_agent.runtime_observers import (
    AIDER_HISTORY_FILENAME,
    AIDER_IDLE_THRESHOLD_MS,
    AIDER_LIVE_THRESHOLD_MS,
    AiderTranscriptObserver,
    DefaultFilesystemAdapter,
    NoOpRuntimePhaseObserver,
    ObserverConsecutiveCrashLimit,
    RuntimePhaseObserver,
    RuntimePhaseObserverRegistry,
    _last_fenced_block,
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
