# Requirements Document

## Introduction

Deskmate's `AgentRuntimeScanner` (`agent/deskmate_agent/agent_runtime.py`)
discovers running IDEs and CLI agents by polling `ps -axo` every two seconds
and produces `AgentRuntimeStatus` rows whose `phase` is hard-wired to
`SessionPhase.RUNNING`. Today only hook-installed agents (Codex CLI,
Claude Code, Cursor) and the Claude JSONL transcript reader populate the
richer `SessionPhase` values (`THINKING`, `EDITING`, `RUNNING_TOOL`,
`TESTING`, `WAITING_FOR_APPROVAL`, `WAITING_FOR_ANSWER`, `COMPLETED`,
`FAILED`) that the island and session list already render. Every other
runtime detected by the passive scanner — Aider, Gemini, Kimi, Qwen,
Factory Droid, CodeBuddy, Qoder, Zed, Trae, Sublime, Fleet, Nova, Neovim,
GitHub Desktop, Warp, VSCode, Windsurf, JetBrains, Xcode — therefore
appears as a forever-`running` row.

This feature delivers three layered pieces:

1. **`AgentRuntimeWorkspace` field on the runtime status.** Pure-function
   workspace-root detection from the existing `cwd` hint, surfaced as a
   new optional field on `AgentRuntimeStatus` so the session list can
   read `Cursor · deskmate` instead of `Cursor`.
2. **`RuntimePhaseObserver` protocol and registry.** A new module that
   lets pluggable observers subscribe to a subset of `AgentRuntimeStatus`
   rows and emit `AgentEvent` payloads into the existing
   `AgentEventReducer`. The registry owns observer lifecycle, ticks
   observers off the scanner's existing 2 s cadence, and is the only
   authorised path for promoting a status off `RUNNING`.
3. **`AiderTranscriptObserver` as the first concrete observer.** Tails
   `.aider.chat.history.md` at the workspace root and maps recent block
   shape and file mtime onto `THINKING`, `EDITING`, `RUNNING_TOOL`, and
   `COMPLETED`. Subsequent observers (Gemini, Kimi, Qwen, …) are
   deferred to follow-up specs that reuse this protocol.

All phase mutations continue to flow through `AgentEventReducer` so the
existing `_preserves_actionable_state` guard keeps applying:
`WAITING_FOR_APPROVAL` and `WAITING_FOR_ANSWER` are never silently
downgraded back to `RUNNING` by a passive observer. Observers receive a
`FilesystemAdapter` seam at construction so tests never touch the host
filesystem.

The `.aider.chat.history.md` reader is a read-only consumer of an
external format. No pretty printer or round-trip property applies because
the observer emits `AgentEvent` values, not reconstructed transcript
text — its outputs are validated against fixture transcripts instead of
a parse → print → parse cycle.

**Out of scope (deferred to follow-up specs):**

- Concrete observers for Gemini, Kimi, Qwen, Factory Droid, CodeBuddy,
  Qoder, Zed, Trae, Sublime, Fleet, Nova, Neovim, GitHub Desktop, Warp,
  VSCode, Windsurf, JetBrains, and Xcode. Only Aider lands here.
- Hook installer support for new agents.
- Swift-side UI changes beyond consuming the existing `phase` field that
  `SessionRow.phaseLabel` already renders.
- Any changes to the `AgentRuntimeKind` or `AgentRuntimeSource` enums.

## Glossary

- **AgentRuntimeScanner**: Existing async poller in
  `agent/deskmate_agent/agent_runtime.py` that classifies `ps` output
  into `AgentRuntimeStatus` rows on a 2 s cadence.
- **AgentRuntimeStatus**: Existing Pydantic row produced by the scanner
  (`source`, `kind`, `process_id`, `command`, `cwd`, `phase`,
  `priority`, `last_seen_ms`).
- **AgentRuntimeSource**: Existing enum identifying the runtime origin
  (`AIDER`, `CURSOR`, `VSCODE`, …).
- **AgentRuntimeWorkspace**: New optional field added to
  `AgentRuntimeStatus` carrying the resolved workspace root for the
  status (string absolute path or `None`).
- **AgentEventReducer**: Existing reducer in
  `agent/deskmate_agent/agent_events.py` and the only authorised
  mutator of `SessionStore` and `ApprovalStore`.
- **SessionPhase**: Existing enum
  (`WAITING_FOR_APPROVAL`, `WAITING_FOR_ANSWER`, `THINKING`, `EDITING`,
  `RUNNING_TOOL`, `TESTING`, `RUNNING`, `FAILED`, `COMPLETED`).
- **RuntimePhaseObserver**: New abstract base class in a new module
  `agent/deskmate_agent/runtime_observers.py` that declares
  `start()`, `stop()`, `targets(statuses)`, and `tick(now_ms)`.
- **RuntimePhaseObserverRegistry**: New holder in the same module that
  the `AgentRuntimeScanner` invokes after status discovery on each
  scan tick.
- **FilesystemAdapter**: New read-only seam injected into observers
  through their constructor. Exposes `exists(path)`,
  `stat_mtime_ms(path)`, and `read_tail(path, max_bytes)`.
- **WorkspaceRootDetector**: New pure function that walks ancestors of a
  given `cwd` looking for a WorkspaceRootMarker.
- **WorkspaceRootMarker**: One of the filenames `.git`, `pyproject.toml`,
  `package.json`, `Cargo.toml`, `Package.swift` that identifies a
  project root. The list is exhaustive for V1.
- **AiderTranscriptObserver**: First concrete `RuntimePhaseObserver`,
  targeting statuses whose `source == AgentRuntimeSource.AIDER`.
- **AiderHistoryFile**: The file `.aider.chat.history.md` that Aider
  writes at the workspace root.
- **AiderUserBlockPrefix**: A line beginning with `> ` inside an
  AiderHistoryFile, marking a user prompt block.
- **AiderEditBlock**: A fenced code block in an AiderHistoryFile whose
  fence info string is `diff` or whose body contains `+++ b/` or
  `--- a/` diff headers.
- **AiderShellBlock**: A fenced code block in an AiderHistoryFile whose
  fence info string is `bash`, `sh`, or `shell`.
- **AiderTranscriptTail**: The last 4096 bytes of an AiderHistoryFile,
  read via the FilesystemAdapter, used for block classification.
- **TickBudgetMs**: The combined wall-clock budget of one
  `AgentRuntimeScanner.scan_once` call plus all
  RuntimePhaseObserverRegistry work, set to 50 ms.
- **ObserverConsecutiveCrashLimit**: The number of consecutive
  exceptions an observer may raise before the
  RuntimePhaseObserverRegistry permanently disables it, set to 3.

## Requirements

### Requirement 1: Workspace root detection from cwd

**User Story:** As a Deskmate user, I want session titles to identify
which project an IDE is editing, so that I can disambiguate two Cursor
windows working on different repositories.

#### Acceptance Criteria

1. WHEN the WorkspaceRootDetector is invoked with a non-null `cwd`, THE
   WorkspaceRootDetector SHALL walk the ancestors of `cwd` and SHALL
   return the deepest ancestor directory (including `cwd` itself) that
   contains at least one WorkspaceRootMarker.
2. WHEN no ancestor of `cwd` contains a WorkspaceRootMarker, THE
   WorkspaceRootDetector SHALL return `cwd` itself as the workspace
   root.
3. WHEN the WorkspaceRootDetector is invoked with `cwd` equal to
   `None`, THE WorkspaceRootDetector SHALL return `None`.
4. THE WorkspaceRootDetector SHALL recognise exactly the marker
   filenames `.git`, `pyproject.toml`, `package.json`, `Cargo.toml`,
   and `Package.swift`.
5. IF the WorkspaceRootDetector encounters an `OSError` while testing
   for a WorkspaceRootMarker on an ancestor, THEN THE
   WorkspaceRootDetector SHALL stop the walk and SHALL return the
   deepest ancestor it has already classified, defaulting to `cwd`
   when no ancestor has been classified.
6. THE WorkspaceRootDetector SHALL stop walking at the filesystem root
   and SHALL NOT cross into any directory whose parent equals itself.

### Requirement 2: AgentRuntimeWorkspace field on status

**User Story:** As a Deskmate maintainer, I want the workspace root
exposed as a structured field on `AgentRuntimeStatus`, so that
downstream consumers do not have to re-parse the display name.

#### Acceptance Criteria

1. THE AgentRuntimeStatus SHALL define a new optional field
   `workspace` of type `str | None` defaulting to `None`.
2. WHEN the AgentRuntimeScanner builds an AgentRuntimeStatus from a
   classified process row, THE AgentRuntimeScanner SHALL invoke the
   WorkspaceRootDetector with the row's `cwd` and SHALL assign its
   result to the AgentRuntimeWorkspace field.
3. WHEN the AgentRuntimeScanner upserts a `SessionInfo` from an
   AgentRuntimeStatus whose AgentRuntimeWorkspace is non-null, THE
   AgentRuntimeScanner SHALL set the SessionInfo `title` to
   `"<source label> · <basename(workspace)>"` where `<source label>`
   is the existing `display_name` for the runtime pattern.
4. WHEN the AgentRuntimeStatus AgentRuntimeWorkspace is `None`, THE
   AgentRuntimeScanner SHALL set the SessionInfo `title` to the bare
   `display_name` of the runtime pattern.
5. IF `basename(workspace)` is the empty string, THEN THE
   AgentRuntimeScanner SHALL set the SessionInfo `title` to the bare
   `display_name` of the runtime pattern.
6. THE AgentRuntimeScanner SHALL NOT modify the existing
   `AgentRuntimeSource` enum values, the existing
   `AgentRuntimeKind` enum values, or any field of `_RuntimePattern`
   in service of this requirement.

### Requirement 3: `RuntimePhaseObserver` protocol surface

**User Story:** As a Deskmate maintainer, I want a single typed
contract for phase observers, so that adding the next runtime
(Gemini, Kimi, Qwen) is a table-driven extension rather than a fork
of the scanner.

#### Acceptance Criteria

1. THE RuntimePhaseObserver SHALL be defined as an abstract base class
   in a new module `agent/deskmate_agent/runtime_observers.py`.
2. THE RuntimePhaseObserver SHALL declare an abstract method `start()`
   that takes no arguments and returns `None`.
3. THE RuntimePhaseObserver SHALL declare an abstract method `stop()`
   that takes no arguments and returns `None`.
4. THE RuntimePhaseObserver SHALL declare an abstract method
   `targets(statuses)` that accepts a `Sequence[AgentRuntimeStatus]`
   and returns a `list[AgentRuntimeStatus]` containing the subset of
   inputs the observer intends to govern.
5. THE RuntimePhaseObserver SHALL declare an abstract method
   `tick(now_ms)` that accepts the scanner's current monotonic
   timestamp in milliseconds and returns a `list[AgentEvent]`.
6. THE RuntimePhaseObserver SHALL accept a FilesystemAdapter and a
   clock callable through its constructor and SHALL retain those
   dependencies as read-only attributes.
7. THE runtime_observers module SHALL be importable without importing
   `asyncio.create_task`, `subprocess`, or any third-party package
   not already declared in `agent/pyproject.toml`.

### Requirement 4: `RuntimePhaseObserverRegistry` integration with the scanner

**User Story:** As a Deskmate maintainer, I want the scanner to drive
observer ticks, so that observers do not need their own asyncio tasks
for V1.

#### Acceptance Criteria

1. THE RuntimePhaseObserverRegistry SHALL accept a list of
   `RuntimePhaseObserver` instances at construction.
2. THE RuntimePhaseObserverRegistry SHALL accept an
   `AgentEventReducer` reference at construction.
3. WHEN `AgentRuntimeScanner.scan_once` completes status discovery
   for a tick, THE RuntimePhaseObserverRegistry SHALL be invoked once
   with the discovered statuses and the same `now_ms` value the
   scanner already passes to `AgentRuntimeStore.expire`.
4. WHEN the RuntimePhaseObserverRegistry is invoked for a tick, THE
   RuntimePhaseObserverRegistry SHALL call `targets(statuses)` on
   every registered observer.
5. WHEN an observer's `targets(statuses)` returns at least one
   AgentRuntimeStatus and the observer has not yet been started, THE
   RuntimePhaseObserverRegistry SHALL call `start()` on that
   observer before calling `tick(now_ms)` on it.
6. WHEN an observer's `targets(statuses)` returns an empty list and
   the observer has previously been started, THE
   RuntimePhaseObserverRegistry SHALL call `stop()` on that observer
   and SHALL NOT call `tick(now_ms)` on it for that tick.
7. WHEN `tick(now_ms)` returns one or more `AgentEvent` instances,
   THE RuntimePhaseObserverRegistry SHALL forward each event to
   `AgentEventReducer.apply` in the order returned by the observer.
8. THE RuntimePhaseObserverRegistry SHALL be constructed by the same
   code path that constructs the AgentRuntimeScanner (the runtime
   wiring in `agent/deskmate_agent/agent_runtime.py`), and SHALL NOT
   be constructed by `App._build_app()`.

### Requirement 5: Observer authority boundary

**User Story:** As a Deskmate maintainer, I want observers to be the
only component that promotes a passive runtime row off `RUNNING`, so
that phase derivation stays decoupled from process discovery and
hook-driven sessions remain the source of truth for hook-installed
agents.

#### Acceptance Criteria

1. THE AgentRuntimeScanner SHALL set `phase` to `SessionPhase.RUNNING`
   on every newly-discovered AgentRuntimeStatus and SHALL NOT assign
   any other SessionPhase value to that status.
2. THE RuntimePhaseObserver SHALL emit phase changes only by returning
   `AgentEvent` instances from `tick(now_ms)`.
3. THE RuntimePhaseObserver SHALL NOT call any method, getter, or
   property accessor on `SessionStore`, `ApprovalStore`, or
   `AgentRuntimeStore`, and SHALL NOT receive any reference to those
   stores through its constructor or attributes.
4. WHEN an observer emits a `SessionActivityUpdated` event with
   `phase` set to `SessionPhase.RUNNING` for a session whose existing
   phase in `SessionStore` is `WAITING_FOR_APPROVAL` or
   `WAITING_FOR_ANSWER`, THE AgentEventReducer SHALL preserve the
   existing phase per the existing `_preserves_actionable_state`
   guard.
5. WHEN the RuntimePhaseObserverRegistry forwards observer events to
   the AgentEventReducer, THE RuntimePhaseObserverRegistry SHALL NOT
   inspect or mutate `SessionStore` or `ApprovalStore` directly.
6. WHERE a `SessionInfo` carries `kind == AgentRuntimeKind.HOOK_SESSION`,
   THE RuntimePhaseObserverRegistry SHALL skip forwarding any
   AgentEvent whose `session_id` matches that SessionInfo, so
   hook-driven sessions remain governed exclusively by their hook
   pipeline.

### Requirement 6: Aider transcript observer file targeting

**User Story:** As a Deskmate user, I want the session list to show
what my running Aider session is doing, so that I do not have to
switch terminals to check whether it is still working.

#### Acceptance Criteria

1. WHEN `targets(statuses)` is called on the AiderTranscriptObserver,
   THE AiderTranscriptObserver SHALL return every AgentRuntimeStatus
   whose `source` equals `AgentRuntimeSource.AIDER` and whose
   AgentRuntimeWorkspace is non-null.
2. THE AiderTranscriptObserver SHALL resolve the AiderHistoryFile path
   as `<status.workspace>/.aider.chat.history.md` and SHALL NOT
   search ancestors of the workspace.
3. WHEN `tick(now_ms)` runs and `FilesystemAdapter.exists(path)`
   returns `False` for a target's AiderHistoryFile, THE
   AiderTranscriptObserver SHALL emit no `AgentEvent` for that
   target.
4. THE AiderTranscriptObserver SHALL set the `session_id` of every
   emitted `AgentEvent` to the `effective_session_id` of the
   originating AgentRuntimeStatus.
5. THE AiderTranscriptObserver SHALL set the `source` of every
   emitted `AgentEvent` to `"aider"`.
6. THE AiderTranscriptObserver SHALL set the `cwd` of every emitted
   `AgentEvent` to the originating AgentRuntimeStatus
   AgentRuntimeWorkspace.

### Requirement 7: Aider transcript observer phase mapping

**User Story:** As a Deskmate user, I want my Aider sessions to surface
THINKING, EDITING, RUNNING_TOOL, and COMPLETED in the island, so that
the same vocabulary that already works for Claude Code applies to
Aider.

#### Acceptance Criteria

1. WHEN the AiderTranscriptTail contains an AiderEditBlock as its
   final fenced code block, THE AiderTranscriptObserver SHALL emit a
   `SessionActivityUpdated` event with
   `phase = SessionPhase.EDITING`.
2. WHEN the AiderTranscriptTail contains an AiderShellBlock as its
   final fenced code block and
   `now_ms - stat_mtime_ms(AiderHistoryFile) <= 3000`, THE
   AiderTranscriptObserver SHALL emit a `SessionActivityUpdated`
   event with `phase = SessionPhase.RUNNING_TOOL`.
3. WHEN `now_ms - stat_mtime_ms(AiderHistoryFile) <= 3000` and the
   AiderTranscriptTail does not satisfy criteria 7.1 or 7.2, THE
   AiderTranscriptObserver SHALL emit a `SessionActivityUpdated`
   event with `phase = SessionPhase.THINKING`.
4. WHEN `now_ms - stat_mtime_ms(AiderHistoryFile) > 30000` and the
   AiderTranscriptTail's final non-empty line does not begin with
   the AiderUserBlockPrefix, THE AiderTranscriptObserver SHALL emit
   a `SessionCompleted` event with `failed = False`.
5. WHEN multiple of the conditions in 7.1, 7.2, 7.3, and 7.4 hold
   for a single target on the same tick, THE
   AiderTranscriptObserver SHALL apply exactly the highest-priority
   rule in the order EDITING (7.1), RUNNING_TOOL (7.2), THINKING
   (7.3), COMPLETED (7.4) and SHALL emit at most one `AgentEvent`
   for that target.
6. WHILE
   `3000 < now_ms - stat_mtime_ms(AiderHistoryFile) <= 30000`
   and none of 7.1, 7.2, or 7.4 hold, THE AiderTranscriptObserver
   SHALL emit no `AgentEvent` for that target.

### Requirement 8: Aider transcript observer mtime backoff and tail size

**User Story:** As a Deskmate maintainer, I want the observer to skip
work when the transcript has not changed and to read a bounded
suffix when it has, so that the 2 s scanner cadence does not pay
for unchanged files or for arbitrarily long transcripts.

#### Acceptance Criteria

1. THE AiderTranscriptObserver SHALL cache the AiderHistoryFile mtime
   per target session between ticks.
2. WHEN `tick(now_ms)` runs and
   `FilesystemAdapter.stat_mtime_ms(AiderHistoryFile)` equals the
   cached mtime for that session, THE AiderTranscriptObserver SHALL
   NOT call `FilesystemAdapter.read_tail` and SHALL emit no
   `AgentEvent` for that session.
3. WHEN the AiderTranscriptObserver reads the AiderHistoryFile, THE
   AiderTranscriptObserver SHALL invoke
   `FilesystemAdapter.read_tail(path, max_bytes=4096)` and SHALL
   classify the AiderTranscriptTail using only the bytes returned.
4. WHEN `targets(statuses)` no longer returns a previously-targeted
   session, THE AiderTranscriptObserver SHALL drop the cached mtime
   for that session before the next tick.

### Requirement 9: Tick budget

**User Story:** As a Deskmate user, I want the menu-bar app to stay
responsive, so that adding observers does not introduce visible
latency.

#### Acceptance Criteria

1. THE combined wall-clock cost of one `AgentRuntimeScanner.scan_once`
   call and all RuntimePhaseObserverRegistry work invoked from it
   SHALL NOT exceed TickBudgetMs (50 ms) when measured against a
   fixture of 32 AgentRuntimeStatus rows on a 2020-or-later Apple
   Silicon Mac.
2. WHEN the AiderTranscriptObserver runs `tick(now_ms)` for a single
   target whose AiderHistoryFile has not changed since the previous
   tick, THE AiderTranscriptObserver SHALL perform exactly one
   FilesystemAdapter call (`stat_mtime_ms`) and SHALL perform no
   `read_tail` call.
3. THE AiderTranscriptObserver SHALL NOT spawn any thread, process,
   or asyncio task during `start()`, `stop()`, `targets(statuses)`,
   or `tick(now_ms)`.

### Requirement 10: Filesystem adapter seam for testability

**User Story:** As a Deskmate maintainer, I want observer tests to
run without touching the host filesystem, so that scenarios are
deterministic and parallel-safe.

#### Acceptance Criteria

1. THE FilesystemAdapter SHALL expose `exists(path)`,
   `stat_mtime_ms(path)`, and `read_tail(path, max_bytes)` and SHALL
   NOT expose any write operation.
2. THE AiderTranscriptObserver SHALL perform every filesystem access
   through the FilesystemAdapter passed to its constructor.
3. THE runtime_observers module SHALL provide a default
   FilesystemAdapter implementation backed by `pathlib.Path` and
   `os.stat` for production use.
4. WHEN a test passes a fake FilesystemAdapter to the
   AiderTranscriptObserver, THE AiderTranscriptObserver SHALL invoke
   only that adapter's methods for filesystem reads.

### Requirement 11: Degradation on missing or corrupt transcript

**User Story:** As a Deskmate user, I want a half-written or vanished
transcript to leave the row at `RUNNING` instead of flipping to
FAILED, so that transient I/O does not generate noise.

#### Acceptance Criteria

1. IF `FilesystemAdapter.exists(path)` returns `False`, THEN THE
   AiderTranscriptObserver SHALL emit no `AgentEvent` for that
   target.
2. IF `FilesystemAdapter.read_tail(path, max_bytes)` raises
   `FileNotFoundError`, THEN THE AiderTranscriptObserver SHALL emit
   no `AgentEvent` for that target.
3. IF `FilesystemAdapter.read_tail(path, max_bytes)` raises an
   `OSError` other than `FileNotFoundError`, THEN THE
   AiderTranscriptObserver SHALL emit no `AgentEvent` for that
   target and SHALL log one warning per
   `(session_id, error class)` pair per scanner tick.
4. IF the AiderTranscriptTail cannot be split into at least one
   block by the observer's parser, THEN THE
   AiderTranscriptObserver SHALL emit no `AgentEvent` for that
   target.
5. WHEN the AiderTranscriptTail contains an unclosed code fence,
   THE AiderTranscriptObserver SHALL parse all complete blocks
   before the malformed fence and SHALL ignore the malformed tail.

### Requirement 12: Observer crash isolation

**User Story:** As a Deskmate maintainer, I want a buggy observer to
not take down the runtime scanner, so that the rest of the session
list keeps updating.

#### Acceptance Criteria

1. IF an observer's `start()`, `stop()`, `targets(statuses)`, or
   `tick(now_ms)` raises any `Exception`, THEN THE
   RuntimePhaseObserverRegistry SHALL catch the exception and SHALL
   log one warning that includes the observer class name and the
   exception class name.
2. IF an observer raises during `tick(now_ms)`, THEN THE
   RuntimePhaseObserverRegistry SHALL discard any partial event list
   the observer returned and SHALL forward zero events to the
   AgentEventReducer for that observer on that tick.
3. IF an observer raises during `start()`, THEN THE
   RuntimePhaseObserverRegistry SHALL leave that observer marked as
   not-started so that the next tick with matching targets retries
   `start()`.
4. WHEN an observer raises ObserverConsecutiveCrashLimit (3)
   consecutive exceptions across ticks, THE
   RuntimePhaseObserverRegistry SHALL stop calling that observer for
   the remainder of the scanner's lifetime and SHALL log one warning
   recording the disable.
5. WHEN any observer call raises, THE AgentRuntimeScanner SHALL
   complete `scan_once` and continue its scan loop on the next
   poll interval.

### Requirement 13: No-op smoke observer for tests

**User Story:** As a Deskmate maintainer, I want a trivial observer
shipped in the module so that registry tests have a known-good
fixture, so that registry behaviour is verifiable without depending
on the AiderTranscriptObserver.

#### Acceptance Criteria

1. THE runtime_observers module SHALL export a class
   `NoOpRuntimePhaseObserver` that implements every
   RuntimePhaseObserver abstract method.
2. THE NoOpRuntimePhaseObserver SHALL return an empty list from
   `targets(statuses)` regardless of input.
3. THE NoOpRuntimePhaseObserver SHALL return an empty list from
   `tick(now_ms)` regardless of input.
4. THE NoOpRuntimePhaseObserver SHALL increment publicly-readable
   integer counter attributes named `start_calls`, `stop_calls`,
   `targets_calls`, and `tick_calls` on each respective method
   invocation.

### Requirement 14: Test suite preservation

**User Story:** As a Deskmate maintainer, I want the existing test
baselines to keep passing, so that the new framework lands without
silent regressions.

#### Acceptance Criteria

1. THE existing pytest suite under `agent/tests/` SHALL continue to
   collect and pass at the pre-feature count of 625 tests, plus any
   new tests added for this feature.
2. THE existing Swift smoke suite under
   `DeskmateApp/Sources/DeskmateCoreSmoke` SHALL continue to pass
   at the pre-feature count of 282 cases, plus any new cases added
   for this feature.
3. THE feature SHALL NOT add any third-party dependency to
   `agent/pyproject.toml` or to `DeskmateApp/Package.swift`.
4. THE feature SHALL NOT modify any file under
   `DeskmateApp/Sources/DeskmateMenuBarApp/` or
   `DeskmateApp/Sources/DeskmateShellApp/`.
