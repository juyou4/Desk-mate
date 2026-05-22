"""Runtime phase observer framework (V10 runtime-phase-observers spec).

Plug-in seam that lets file/log tailers upgrade the phase of a passively-
discovered :class:`AgentRuntimeStatus` row off the default ``RUNNING``
state. Observers subscribe to a subset of statuses, are ticked off the
existing :class:`AgentRuntimeScanner` cadence, and emit
:class:`AgentEvent` instances into the existing
:class:`AgentEventReducer`. The reducer remains the single source of
truth for ``SessionStore`` mutations (Requirement 5).

Inspired by MioIsland's `SessionPhase` state machine
(``repos/MioIsland/ClaudeIsland/Models/SessionPhase.swift``) — we keep
the *boundary* idea (a single typed surface for phase mutations) but
not the Swift code.

Layering, top → bottom:

* :class:`FilesystemAdapter` — read-only seam. The default
  implementation is the only thing that touches the host filesystem in
  production; tests inject fakes (Requirement 10).
* :class:`RuntimePhaseObserver` ABC — abstract surface every observer
  implements (Requirement 3).
* :class:`NoOpRuntimePhaseObserver` — fixture observer for registry
  tests (Requirement 13).
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# V10 Requirement 3.7 — import surface must not pull asyncio.create_task,
# subprocess, or third-party packages absent from agent/pyproject.toml.
# We use only the standard library + project-internal types below.
from .agent_events import (
    AgentEvent,
    AgentEventReducer,
    SessionActivityUpdated,
    SessionCompleted,
)
from .agent_runtime import (
    AgentRuntimeKind,
    AgentRuntimeSource,
    AgentRuntimeStatus,  # noqa: F401  (re-exported via API)
)
from .logging_setup import get_logger
from .sessions import SessionInfo, SessionPhase

_LOG = get_logger("deskmate_agent.runtime_observers")

# ---------------------------------------------------------------------------
# Filesystem adapter (V10 Requirement 10)
# ---------------------------------------------------------------------------


@runtime_checkable
class FilesystemAdapter(Protocol):
    """Read-only filesystem seam for observer testing.

    Requirement 10.1 — exposes ``exists``, ``stat_mtime_ms``, and
    ``read_tail`` only. No write surface; tests must never mutate
    the host disk through this protocol.

    ``stat_mtime_ms`` returns ``None`` for missing files instead of
    raising, so callers can write a single ``if mtime is None``
    short-circuit without a try-block (Requirement 8.2's mtime cache
    relies on a sentinel-friendly return type).

    ``read_tail`` is allowed to raise ``FileNotFoundError`` and other
    ``OSError`` subclasses; observers catch them per Requirement 11.
    """

    def exists(self, path: str) -> bool: ...

    def stat_mtime_ms(self, path: str) -> int | None: ...

    def read_tail(self, path: str, max_bytes: int) -> bytes: ...


class DefaultFilesystemAdapter:
    """Production filesystem adapter backed by ``os.path`` and ``os``.

    Requirement 10.3 — provides the default implementation. The
    negative-``SEEK_END`` trick reads the suffix of a long file in a
    single syscall and falls through to a full read on ``OSError``,
    which covers files shorter than ``max_bytes``.
    """

    def exists(self, path: str) -> bool:
        return os.path.exists(path)

    def stat_mtime_ms(self, path: str) -> int | None:
        try:
            return int(os.stat(path).st_mtime * 1000)
        except OSError:
            # Treat any stat failure (missing file, permission, broken
            # symlink) as "no mtime" so observers fall through to the
            # missing-file branch instead of raising.
            return None

    def read_tail(self, path: str, max_bytes: int) -> bytes:
        # ``rb`` so the caller does the decoding — keeps this layer
        # encoding-agnostic and lets the Aider parser pick its own
        # ``errors="replace"`` policy.
        with open(path, "rb") as fh:
            try:
                fh.seek(-max_bytes, os.SEEK_END)
            except OSError:
                # File is shorter than ``max_bytes`` (or pseudo-FS
                # such as ``/proc`` rejected the negative seek) —
                # fall back to reading from the start.
                fh.seek(0)
            return fh.read()


# ---------------------------------------------------------------------------
# Observer ABC (V10 Requirement 3)
# ---------------------------------------------------------------------------


# Default clock used when the caller does not inject one. Hoisted to
# the module level so introspection tools and tests can reference it
# directly instead of digging through default arg values.
def _default_clock() -> int:
    return int(time.time() * 1000)


class RuntimePhaseObserver(ABC):
    """Abstract base class every runtime phase observer extends.

    Requirements 3.1–3.6:

    * ``start()`` and ``stop()`` are lifecycle hooks the registry calls
      around the first and last tick the observer is interested in.
    * ``targets(statuses)`` returns the subset of currently-discovered
      statuses the observer governs.
    * ``tick(now_ms)`` returns zero or more :class:`AgentEvent`
      instances the registry then forwards to the reducer.
    * ``__init__`` accepts the ``FilesystemAdapter`` seam and an
      optional clock callable; both are stored as read-only attributes
      so tests can inspect (but not mutate) them.
    """

    def __init__(
        self,
        *,
        fs: FilesystemAdapter,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        # Store as protected attributes — subclasses access via
        # ``self._fs`` / ``self._clock`` and the registry never
        # touches them. Read-only by convention; we don't enforce
        # immutability because tests sometimes monkeypatch.
        self._fs: FilesystemAdapter = fs
        self._clock: Callable[[], int] = clock

    @abstractmethod
    def start(self) -> None:
        """Lifecycle hook: invoked once when the observer first
        receives a non-empty ``targets`` subset."""

    @abstractmethod
    def stop(self) -> None:
        """Lifecycle hook: invoked once when the observer transitions
        from "had targets" to "has no targets"."""

    @abstractmethod
    def targets(
        self, statuses: Sequence[AgentRuntimeStatus]
    ) -> list[AgentRuntimeStatus]:
        """Filter the scanner's discovery set down to statuses this
        observer is willing to govern. The registry uses the result to
        drive ``start`` / ``stop`` lifecycle (Requirements 4.5, 4.6).
        """

    @abstractmethod
    def tick(self, now_ms: int) -> list[AgentEvent]:
        """Emit the events the observer believes are due on this tick.

        The list may be empty. The registry forwards events to the
        reducer in returned order (Requirement 4.7).
        """


# ---------------------------------------------------------------------------
# No-op observer (V10 Requirement 13)
# ---------------------------------------------------------------------------


class NoOpRuntimePhaseObserver(RuntimePhaseObserver):
    """Trivial observer used by registry tests as a known-good fixture.

    Requirement 13.1 — implements every abstract method.
    Requirement 13.2 / 13.3 — both ``targets`` and ``tick`` return ``[]``.
    Requirement 13.4 — exposes public ``*_calls`` integer counters that
    increment on each respective method invocation. Tests assert against
    the counters to verify lifecycle ordering without depending on a
    concrete observer (e.g. :class:`AiderTranscriptObserver`).
    """

    def __init__(
        self,
        *,
        fs: FilesystemAdapter | None = None,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        super().__init__(
            fs=fs or DefaultFilesystemAdapter(),
            clock=clock,
        )
        self.start_calls: int = 0
        self.stop_calls: int = 0
        self.targets_calls: int = 0
        self.tick_calls: int = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1

    def targets(
        self, statuses: Sequence[AgentRuntimeStatus]
    ) -> list[AgentRuntimeStatus]:
        self.targets_calls += 1
        return []

    def tick(self, now_ms: int) -> list[AgentEvent]:
        self.tick_calls += 1
        return []


# ---------------------------------------------------------------------------
# Aider transcript observer (V10 Requirements 6, 7, 8, 11)
# ---------------------------------------------------------------------------


# Requirement 7.2 / 7.3 — anything modified within this many ms is
# considered "live" (Aider is generating tokens or running a tool).
# Requirement 7.4 — anything older than this is considered idle.
AIDER_LIVE_THRESHOLD_MS: int = 3_000
AIDER_IDLE_THRESHOLD_MS: int = 30_000

# Requirement 8.3 — bounded tail read so transcripts of arbitrary
# length don't blow the tick budget. 4096 bytes covers the last ~30
# Aider message blocks in practice; large enough that we always see
# at least one fenced block when the file is non-empty.
AIDER_TAIL_BYTES: int = 4096

# Requirement 6.2 — Aider's transcript filename. We only join with the
# workspace root and never walk ancestors, so the observer's behaviour
# stays predictable when nested workspaces share a marker tree.
AIDER_HISTORY_FILENAME: str = ".aider.chat.history.md"


def _last_fenced_block(tail: bytes) -> tuple[str, str] | None:
    """Return ``(info_string, body)`` of the last *closed* fenced
    block in ``tail``, or ``None`` if no closed block exists.

    Requirement 11.5 — an unclosed trailing fence (the file was
    being written when we read the tail) is silently ignored: the
    parser only returns blocks where it observed both the opening
    and the closing ``` ``` ``` lines.

    Decoding uses ``errors="replace"`` so a partial UTF-8 byte
    sequence at the start of the tail (we may have started reading
    inside a multi-byte character) does not raise.
    """
    text = tail.decode("utf-8", errors="replace")
    blocks: list[tuple[str, list[str]]] = []
    info: str | None = None
    body: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.lstrip()
        if stripped.startswith("```"):
            if info is None:
                # Opening fence — capture the info string (language
                # tag) so callers can branch on ``diff`` / ``bash``.
                info = stripped[3:].strip().lower()
                body = []
            else:
                blocks.append((info, body))
                info = None
                body = []
        elif info is not None:
            body.append(raw_line)
    if not blocks:
        return None
    info_str, body_lines = blocks[-1]
    return info_str, "\n".join(body_lines)


def _looks_like_diff(info: str, body: str) -> bool:
    """Sniff a fenced block for diff content even when the info
    string isn't ``diff``. Aider sometimes emits unlabelled fences
    that contain ``--- a/`` / ``+++ b/`` headers; treating them as
    EDITING is closer to user intent than letting them fall through
    to RUNNING_TOOL or THINKING."""
    if info == "diff":
        return True
    return "+++ b/" in body or "--- a/" in body


def _is_shell_info(info: str) -> bool:
    return info in {"bash", "sh", "shell", "zsh"}


def _last_non_empty_line(tail: bytes) -> str:
    """Return the final non-empty line in ``tail`` after a UTF-8
    decode, or the empty string if the tail has no non-empty
    lines. Used by the COMPLETED rule to gate on the user-prompt
    prefix (``> ``)."""
    text = tail.decode("utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if line.strip():
            return line
    return ""


@dataclass(frozen=True)
class _AiderDecision:
    """Internal sentinel describing one of the four phase outcomes
    or "no event". Encoded as a value type so the decision tree is
    a pure function of ``(tail, mtime, now)`` — easier to test."""

    phase: SessionPhase | None
    completed: bool = False  # True only when phase == COMPLETED


def _decide_aider_phase(
    tail: bytes, *, mtime_ms: int, now_ms: int
) -> _AiderDecision | None:
    """Map ``(tail, mtime, now)`` onto an Aider phase decision.

    Implements Requirement 7's strict priority order:
    EDITING > RUNNING_TOOL > THINKING > COMPLETED. Returns ``None``
    when the tail is unparseable or when the mtime falls in the
    3-30 s "no-mans-land" with no diff/shell signal — the spec
    deliberately keeps the existing phase rather than flapping back
    to RUNNING.

    **Validates Requirement 7.5** — at most one event per
    ``(session, tick)`` because exactly one branch returns a
    non-None decision (and the caller emits exactly one event per
    decision).
    """
    block = _last_fenced_block(tail)
    age_ms = now_ms - mtime_ms

    # Requirement 7.1 — EDITING wins outright when the final block
    # looks like a diff. We allow the diff to be older than the live
    # threshold because edits stay visible in the transcript long
    # after the LLM finished writing them; the user wants to see
    # "Aider · editing app.py" until the next phase.
    if block is not None:
        info, body = block
        if _looks_like_diff(info, body):
            return _AiderDecision(phase=SessionPhase.EDITING)

    # Requirement 7.2 — RUNNING_TOOL only fires when both the final
    # block is a shell-style fence AND the file is fresh. A stale
    # bash block means the tool already finished; we shouldn't keep
    # claiming "running tool" forever.
    if block is not None and age_ms <= AIDER_LIVE_THRESHOLD_MS:
        info, _ = block
        if _is_shell_info(info):
            return _AiderDecision(phase=SessionPhase.RUNNING_TOOL)

    # Requirement 7.3 — THINKING covers the broad "live, no specific
    # signal" case: file changed within the last 3 s and the final
    # block (if any) is neither a diff nor a shell fence.
    if age_ms <= AIDER_LIVE_THRESHOLD_MS:
        return _AiderDecision(phase=SessionPhase.THINKING)

    # Requirement 7.4 — COMPLETED fires only when the file has been
    # idle for > 30 s AND the user is not currently typing a new
    # prompt (``> ``-prefixed line). The latter check prevents us
    # from flipping to "completed" while the user is composing a
    # follow-up.
    if age_ms > AIDER_IDLE_THRESHOLD_MS:
        last = _last_non_empty_line(tail).lstrip()
        if not last.startswith("> "):
            return _AiderDecision(
                phase=SessionPhase.COMPLETED, completed=True
            )

    # Requirement 7.6 — 3 < age <= 30 s with no signal: no event,
    # the existing phase stays put.
    return None


class AiderTranscriptObserver(RuntimePhaseObserver):
    """First concrete :class:`RuntimePhaseObserver` — tails
    ``<workspace>/.aider.chat.history.md`` and emits phase events.

    The observer is intentionally minimal: it re-reads the last
    4096 bytes whenever the mtime changes, parses the trailing
    fenced block, and lets :func:`_decide_aider_phase` decide what
    to emit. Per-session state is just a cached mtime so the
    observer can short-circuit unchanged files (Requirement 8.2 —
    locks Property 6).
    """

    def __init__(
        self,
        *,
        fs: FilesystemAdapter | None = None,
        clock: Callable[[], int] = _default_clock,
    ) -> None:
        super().__init__(fs=fs or DefaultFilesystemAdapter(), clock=clock)
        # Per-session mtime cache (Requirement 8.1).
        self._mtime_cache: dict[str, int] = {}
        # Holds the most recent ``targets()`` result keyed by
        # ``effective_session_id`` so ``tick`` knows which sessions
        # to inspect even though it doesn't receive ``statuses``.
        self._last_targets: dict[str, AgentRuntimeStatus] = {}
        # Per-tick warning dedup (Requirement 11.3) — cleared at the
        # top of each ``tick`` so a long-lived I/O error logs once
        # per tick, not once per error.
        self._tick_warned: set[tuple[str, str]] = set()

    def start(self) -> None:
        # No persistent resources to allocate — the registry's
        # lifecycle hooks are sufficient for testability even if
        # the body is empty.
        pass

    def stop(self) -> None:
        # Drop caches so a subsequent ``start`` (e.g. the user
        # quit + relaunched Aider) re-reads the transcript fresh.
        self._mtime_cache.clear()
        self._last_targets.clear()

    def targets(
        self, statuses: Sequence[AgentRuntimeStatus]
    ) -> list[AgentRuntimeStatus]:
        # Requirement 6.1 — only AIDER rows with a non-null
        # workspace are governed; without a workspace the observer
        # has no path to derive.
        selected = [
            s
            for s in statuses
            if s.source == AgentRuntimeSource.AIDER and s.workspace
        ]
        # Requirement 8.4 — sessions that vanish between ticks must
        # have their cache entries dropped so a subsequent
        # rediscovery re-reads the tail rather than skipping on a
        # stale mtime.
        seen_ids = {s.effective_session_id for s in selected}
        for stale in [sid for sid in self._last_targets if sid not in seen_ids]:
            self._mtime_cache.pop(stale, None)
            self._last_targets.pop(stale, None)
        # Snapshot the live set so ``tick`` can iterate without
        # touching the original ``statuses`` reference.
        self._last_targets = {s.effective_session_id: s for s in selected}
        return selected

    def tick(self, now_ms: int) -> list[AgentEvent]:
        # Requirement 11.3 — reset per-tick warning dedup so the
        # next tick's I/O failures still log even if the previous
        # tick already complained.
        self._tick_warned.clear()
        events: list[AgentEvent] = []
        for sid, status in list(self._last_targets.items()):
            event = self._tick_one(sid, status, now_ms)
            if event is not None:
                events.append(event)
        return events

    def _tick_one(
        self,
        sid: str,
        status: AgentRuntimeStatus,
        now_ms: int,
    ) -> AgentEvent | None:
        # Requirement 6.2 — exact path; no ancestor walk.
        path = os.path.join(status.workspace or "", AIDER_HISTORY_FILENAME)
        # Requirement 6.3 — silent skip when the transcript doesn't
        # exist; clear any cached mtime so a later appearance
        # triggers a fresh read.
        if not self._fs.exists(path):
            self._mtime_cache.pop(sid, None)
            return None
        mtime = self._fs.stat_mtime_ms(path)
        if mtime is None:
            self._mtime_cache.pop(sid, None)
            return None
        cached = self._mtime_cache.get(sid)
        if cached is not None and cached == mtime:
            # Requirement 8.2 — mtime unchanged; skip the read and
            # emit nothing. Locks Property 6 (mtime backoff).
            return None
        self._mtime_cache[sid] = mtime
        try:
            tail = self._fs.read_tail(path, AIDER_TAIL_BYTES)
        except FileNotFoundError:
            # Requirement 11.2 — file vanished between exists() and
            # read_tail(). Silent skip; no warning.
            return None
        except OSError as exc:
            # Requirement 11.3 — log one warning per (session, error
            # class) pair per tick so a long-running I/O failure
            # doesn't flood the log.
            key = (sid, type(exc).__name__)
            if key not in self._tick_warned:
                _LOG.warning(
                    "aider_observer.read_failed",
                    session_id=sid,
                    error=type(exc).__name__,
                )
                self._tick_warned.add(key)
            return None
        decision = _decide_aider_phase(
            tail, mtime_ms=mtime, now_ms=now_ms
        )
        if decision is None or decision.phase is None:
            return None
        return _build_aider_event(decision, status, now_ms)


def _build_aider_event(
    decision: _AiderDecision,
    status: AgentRuntimeStatus,
    now_ms: int,
) -> AgentEvent:
    """Build the ``AgentEvent`` payload for a decision. Requirements
    6.4 (session_id from status), 6.5 (source = "aider"), 6.6 (cwd
    = workspace)."""
    base = {
        "session_id": status.effective_session_id,
        "source": "aider",
        "ts_ms": now_ms,
        "cwd": status.workspace,
    }
    if decision.completed:
        return SessionCompleted(**base, failed=False)
    assert decision.phase is not None  # decided in caller
    return SessionActivityUpdated(**base, phase=decision.phase)


# ---------------------------------------------------------------------------
# Registry (V10 Requirement 4 + 5 + 12)
# ---------------------------------------------------------------------------


# Requirement 12.4: maximum consecutive crashes before an observer is
# permanently disabled for the rest of the scanner's lifetime. Hoisted
# to the module level so tests can reference it without monkey-patching
# magic numbers.
ObserverConsecutiveCrashLimit: int = 3


class RuntimePhaseObserverRegistry:
    """Drives observer lifecycle off the existing scanner cadence.

    The registry is the only path through which a passive runtime row
    can leave the default ``RUNNING`` phase (Requirement 5.1 / 5.2);
    observers themselves are forbidden from touching ``SessionStore``,
    ``ApprovalStore``, or ``AgentRuntimeStore`` directly (Requirement
    5.3). All phase mutations therefore funnel through
    :class:`AgentEventReducer` so the existing
    :func:`_preserves_actionable_state` guard keeps protecting the
    ``WAITING_FOR_APPROVAL`` / ``WAITING_FOR_ANSWER`` rows.

    Locks design properties:

    * **Property 2** — events are forwarded in registration order,
      then within an observer in the order ``tick()`` returned.
    * **Property 3** — events targeting a hook-driven session
      (``SessionInfo.kind == HOOK_SESSION``) are silently dropped.
    * **Property 4** — three consecutive exceptions across any of
      ``start`` / ``stop`` / ``targets`` / ``tick`` permanently
      disable the offending observer.
    """

    def __init__(
        self,
        observers: Sequence[RuntimePhaseObserver],
        reducer: AgentEventReducer,
        *,
        session_view: Callable[[str], SessionInfo | None],
    ) -> None:
        # Snapshot the observer iterable so the registry's order is
        # stable even if a caller mutates the original list. Property
        # 2 (event ordering) hinges on the iteration order set here.
        self._observers: list[RuntimePhaseObserver] = list(observers)
        self._reducer = reducer
        # ``session_view`` is a read-only lookup the registry uses to
        # filter out events targeting hook-driven sessions. Observers
        # never see the callable — keeping it on the registry alone
        # is what enforces Requirement 5.3 (no observer touches a
        # store, even read-only).
        self._session_view = session_view
        # ``id(obs)`` keys avoid a new equality contract for the
        # observer ABC. Registry instances are constructed once per
        # scanner so identity is stable for the registry's lifetime.
        self._started: set[int] = set()
        self._crash_count: dict[int, int] = {}
        self._disabled: set[int] = set()

    @property
    def disabled_count(self) -> int:
        """Visible for tests so they can assert observer disabling
        without poking ``_disabled`` directly."""
        return len(self._disabled)

    def is_disabled(self, observer: RuntimePhaseObserver) -> bool:
        return id(observer) in self._disabled

    def notify(
        self,
        statuses: Sequence[AgentRuntimeStatus],
        now_ms: int,
    ) -> None:
        """Run the per-observer lifecycle cascade for one scanner tick.

        Implements Requirement 4.4-4.7 and 12.1-12.5 in a single pass
        so a single caller (``AgentRuntimeScanner.scan_once``) can
        invoke the registry once per tick.
        """
        for obs in self._observers:
            obs_id = id(obs)
            if obs_id in self._disabled:
                # Requirement 12.4 — already disabled, never call
                # again for the rest of the scanner's lifetime.
                continue

            # Requirement 4.4 — call ``targets`` first so the
            # observer can opt out cheaply when no row matches.
            try:
                subset = obs.targets(statuses)
            except Exception as exc:  # noqa: BLE001
                self._record_crash(obs, "targets", exc)
                continue

            if not subset:
                # Requirement 4.6 — observer no longer has work.
                # Tear it down if we previously started it; never
                # call ``tick`` on this observer for this tick.
                if obs_id in self._started:
                    try:
                        obs.stop()
                    except Exception as exc:  # noqa: BLE001
                        self._record_crash(obs, "stop", exc)
                    # Mark un-started regardless of whether ``stop``
                    # raised — keeping a corrupt observer in
                    # ``_started`` would block its own retry path.
                    self._started.discard(obs_id)
                continue

            # Requirement 4.5 — lazy ``start`` on first non-empty
            # subset. Failures here keep the observer un-started
            # so the next eligible tick retries (Requirement 12.3).
            if obs_id not in self._started:
                try:
                    obs.start()
                except Exception as exc:  # noqa: BLE001
                    self._record_crash(obs, "start", exc)
                    continue
                self._started.add(obs_id)

            try:
                events = obs.tick(now_ms)
            except Exception as exc:  # noqa: BLE001
                # Requirement 12.2 — discard any partial event list
                # the observer returned and forward zero events for
                # this observer on this tick.
                self._record_crash(obs, "tick", exc)
                continue

            # A successful tick clears the consecutive-crash counter
            # so a transient hiccup (e.g. a FUSE mount blip) never
            # accumulates toward the disable threshold.
            self._crash_count.pop(obs_id, None)

            for event in events:
                # Requirement 5.6 — never forward events that target
                # a hook-driven session. Hook installers own those
                # rows end-to-end, so a passive observer must not
                # downgrade their phase.
                session = self._session_view(event.session_id)
                if session is not None and (
                    session.kind == AgentRuntimeKind.HOOK_SESSION.value
                ):
                    continue
                self._reducer.apply(event)

    def _record_crash(
        self, obs: RuntimePhaseObserver, where: str, exc: Exception
    ) -> None:
        obs_id = id(obs)
        n = self._crash_count.get(obs_id, 0) + 1
        self._crash_count[obs_id] = n
        # Requirement 12.1 — log a warning carrying the observer
        # class name, the call site, and the exception class name.
        _LOG.warning(
            "runtime_observer.failed",
            observer=type(obs).__name__,
            where=where,
            error=type(exc).__name__,
            consecutive=n,
        )
        if n >= ObserverConsecutiveCrashLimit:
            # Requirement 12.4 — log the disable summary exactly
            # once and add to the disabled set so subsequent ticks
            # short-circuit at the top of ``notify``.
            self._disabled.add(obs_id)
            _LOG.warning(
                "runtime_observer.disabled",
                observer=type(obs).__name__,
                after=n,
            )


__all__ = [
    "AIDER_HISTORY_FILENAME",
    "AIDER_IDLE_THRESHOLD_MS",
    "AIDER_LIVE_THRESHOLD_MS",
    "AIDER_TAIL_BYTES",
    "AiderTranscriptObserver",
    "DefaultFilesystemAdapter",
    "FilesystemAdapter",
    "NoOpRuntimePhaseObserver",
    "ObserverConsecutiveCrashLimit",
    "RuntimePhaseObserver",
    "RuntimePhaseObserverRegistry",
]
