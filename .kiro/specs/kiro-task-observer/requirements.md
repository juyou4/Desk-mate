# Requirements Document

## Introduction

The `runtime-phase-observers` framework (delivered, commit `9fbcbba`)
introduced `RuntimePhaseObserver`, `RuntimePhaseObserverRegistry`, and
the first concrete observer `AiderTranscriptObserver`. That framework
assumes **one passive runtime row produces at most one session row**:
Aider has a single `.aider.chat.history.md` per workspace, so each
discovered Aider process maps to exactly one `SessionInfo`.

Kiro IDE breaks that assumption. A single `Kiro.app` process can hold
**N concurrent specs in flight**, each backed by an independent
metadata file under
`~/.kiro/tasks/<workspace-hash>/<spec-name>.meta.json`. The user wants
each in-flight spec presented as its own row in the menu-bar session
list, so a Kiro observer must emit **multiple `AgentEvent` instances
per tick** — one per recently-modified meta file — even though the
passive scanner only sees one Kiro process.

This feature delivers two layered pieces:

1. **`AgentRuntimeSource.KIRO` enum value plus a `_RUNTIME_PATTERNS`
   row.** The passive scanner gains a classifier so the running
   `Kiro.app` process produces an `AgentRuntimeStatus` with
   `source == AgentRuntimeSource.KIRO`. This is the trigger signal
   for the observer.
2. **`KiroTaskObserver` as the second concrete observer.** Activates
   only when the scanner reports at least one `KIRO` status row. When
   active, scans `~/.kiro/tasks/` for spec meta files, parses each
   recently-modified file, picks the most-recently-updated task in
   that file, and emits one `AgentEvent` per spec keyed by a
   synthetic `session_id` of the form
   `"runtime-kiro-<workspace-hash>-<spec-name>"`.

`KiroTaskObserver` is the framework's first observer with cardinality
greater than its input statuses — it produces a fan-out of synthetic
sessions from a single passive row. The existing
`RuntimePhaseObserver` protocol already permits this because the
registry routes events by `session_id`, not by the
`AgentRuntimeStatus` set the observer received. The observer is
read-only over the metadata files and emits all phase mutations
through `AgentEventReducer`, so the existing
`_preserves_actionable_state` guard continues to protect
`WAITING_FOR_APPROVAL` and `WAITING_FOR_ANSWER` rows.

The `<spec-name>.meta.json` reader is a read-only consumer of an
external format. No pretty-printer or round-trip property applies
because the observer emits `AgentEvent` values, not reconstructed
JSON — its outputs are validated against fixture meta files instead
of a parse → print → parse cycle.

**Out of scope (deferred to follow-up specs):**

- Hook installer for Kiro. This is a passive observer just like
  `AiderTranscriptObserver`.
- Reading Kiro's chat transcripts, steering files, or per-execution
  history beyond the latest `updatedAt` per task.
- Differentiating `THINKING` vs `EDITING` based on `tasks.md` content
  or recent file edits — V1 maps `in_progress` → `THINKING`
  unconditionally.
- Per-task progress aggregation (e.g. "12/30 tasks done"). V1 emits
  one phase per spec, derived from the single most-recently-updated
  task in the meta file.
- Detection of failed task executions. The Kiro meta-file format does
  not document a stable failure sentinel; V1 treats any unknown or
  missing `executionStatus` as `RUNNING`. Mapping to
  `SessionPhase.FAILED` is deferred until the failure sentinel
  stabilises.
- Swift-side changes. The new `kiro` source label is rendered by the
  existing `SessionRow.sourceLabel` default-branch fallback that
  underscore-splits and title-cases unknown sources to produce
  `"Kiro"`.

## Glossary

- **AgentRuntimeScanner**: Existing async poller in
  `agent/deskmate_agent/agent_runtime.py` that classifies `ps`
  output into `AgentRuntimeStatus` rows on a 2 s cadence.
- **AgentRuntimeStatus**: Existing Pydantic row produced by the
  scanner.
- **AgentRuntimeSource**: Existing enum identifying the runtime
  origin (`AIDER`, `CURSOR`, `VSCODE`, …). Gains a new value `KIRO`
  in this feature.
- **AgentRuntimeKind**: Existing enum classifying a runtime as
  `CLI_AGENT`, `GUI_IDE`, `TERMINAL`, or `HOOK_SESSION`.
- **AgentEventReducer**: Existing reducer in
  `agent/deskmate_agent/agent_events.py` and the only authorised
  mutator of `SessionStore` and `ApprovalStore`.
- **SessionPhase**: Existing enum
  (`WAITING_FOR_APPROVAL`, `WAITING_FOR_ANSWER`, `THINKING`,
  `EDITING`, `RUNNING_TOOL`, `TESTING`, `RUNNING`, `FAILED`,
  `COMPLETED`).
- **RuntimePhaseObserver**: Existing abstract base class in
  `agent/deskmate_agent/runtime_observers.py` (delivered by the
  `runtime-phase-observers` spec).
- **RuntimePhaseObserverRegistry**: Existing registry in the same
  module that drives observer ticks off the scanner cadence.
- **FilesystemAdapter**: Existing read-only seam exposing
  `exists(path)`, `stat_mtime_ms(path)`, and
  `read_tail(path, max_bytes)`. Extended in this feature with one
  additional read method declared in Requirement 10.
- **AiderTranscriptObserver**: First concrete `RuntimePhaseObserver`
  shipped in the `runtime-phase-observers` spec. Sibling to the
  `KiroTaskObserver` introduced here.
- **TickBudgetMs**: The combined wall-clock budget of one
  `AgentRuntimeScanner.scan_once` call plus all
  RuntimePhaseObserverRegistry work, set to 50 ms.
- **ObserverConsecutiveCrashLimit**: Existing constant set to 3.
  `KiroTaskObserver` inherits this lifecycle contract from the
  registry.
- **KiroTaskObserver**: New concrete `RuntimePhaseObserver`
  introduced in this feature.
- **KiroTasksRoot**: The directory `~/.kiro/tasks` (with `~` resolved
  to the running user's home directory).
- **KiroWorkspaceHashDir**: A direct child directory of
  KiroTasksRoot whose name is treated as opaque by the observer.
  Per-workspace identity is the directory name as a literal string;
  the observer does not parse or validate the hash.
- **KiroSpecMetaFile**: A file under a KiroWorkspaceHashDir whose
  name ends in `.meta.json`. The basename minus that suffix is the
  KiroSpecName.
- **KiroSpecName**: The `KiroSpecMetaFile` basename with the
  `.meta.json` suffix removed.
- **KiroSpecMetaPayload**: The JSON object decoded from a
  KiroSpecMetaFile, expected to contain a top-level `tasks` object
  whose values are KiroTaskRecord entries.
- **KiroTaskRecord**: A value within `KiroSpecMetaPayload.tasks`,
  expected to carry `updatedAt` (integer milliseconds since epoch),
  optional `executionStatus` (string), optional `executionHistory`
  (list), and `specUri` (string).
- **KiroExecutionStatus**: The string value of
  `KiroTaskRecord.executionStatus`. Recognised values for V1 are
  `"succeed"`, `"in_progress"`, and `"queued"`. Any other value or
  a missing key is treated as the literal sentinel
  `KiroExecutionStatusUnknown`.
- **KiroLatestTaskRecord**: For a given KiroSpecMetaPayload, the
  KiroTaskRecord whose `updatedAt` is numerically maximal among all
  task records in that payload. Ties are broken by the task key in
  ascending lexicographic order.
- **KiroSpecUri**: The `specUri` field of a KiroTaskRecord, expected
  to be an RFC 3986 URI with scheme `file:` whose path, after
  `urllib.parse.unquote`, ends in
  `/.kiro/specs/<spec>/tasks.md`.
- **KiroResolvedWorkspace**: The absolute filesystem path obtained
  by URL-decoding KiroSpecUri, stripping the `file://` scheme, and
  truncating at the parent of the `.kiro` segment that owns the
  matched `tasks.md` ancestor.
- **KiroSyntheticSessionId**: The string
  `"runtime-kiro-<workspace-hash>-<spec-name>"` where
  `<workspace-hash>` is the KiroWorkspaceHashDir directory name and
  `<spec-name>` is the KiroSpecName.
- **KiroSpecTitle**: The string `"Kiro · <spec-name>"` where
  `<spec-name>` is the KiroSpecName. The middle character is U+00B7
  (MIDDLE DOT) to match the existing `"Cursor · deskmate"`
  convention.
- **KiroSpecPhase**: The `SessionPhase` value the observer assigns
  to a single KiroSpecMetaFile on a given tick, derived from the
  KiroLatestTaskRecord per Requirement 7 and overridden by the
  idle rule in Requirement 8.
- **KiroIdleThresholdMs**: The minimum number of milliseconds since
  the KiroLatestTaskRecord's `updatedAt` that causes the observer
  to override KiroSpecPhase to `SessionPhase.COMPLETED` regardless
  of `executionStatus`. Set to 1 800 000 ms (30 minutes).
- **KiroMaxScannedHashDirs**: The maximum number of
  KiroWorkspaceHashDir entries the observer inspects per tick, set
  to 32. When more than 32 hash directories exist, the 32 with the
  largest directory `mtime` are scanned and the remainder are
  skipped for the tick.
- **KiroMetaFileMaxBytes**: The maximum number of bytes the observer
  reads from a KiroSpecMetaFile per tick, set to 65 536. Files
  larger than this are treated as corrupt for the tick.

## Requirements

### Requirement 1: Add `AgentRuntimeSource.KIRO` and a `_RUNTIME_PATTERNS` row

**User Story:** As a Deskmate user, I want a running Kiro IDE to be
discovered by the passive scanner, so that the framework has a signal
to enable the spec-level observer.

#### Acceptance Criteria

1. THE AgentRuntimeSource enum SHALL define a new value `KIRO` whose
   wire string is `"kiro"`.
2. THE `_RUNTIME_PATTERNS` table SHALL include at least one row whose
   `source` equals `AgentRuntimeSource.KIRO` and whose `kind` equals
   `AgentRuntimeKind.GUI_IDE`.
3. WHEN the AgentRuntimeScanner observes a process whose executable
   path or arg list contains the substring `Kiro.app`, THE
   AgentRuntimeScanner SHALL classify that process under
   `AgentRuntimeSource.KIRO`.
4. THE Kiro `_RuntimePattern` row SHALL set `display_name` to the
   string `"Kiro"`.
5. THE Kiro `_RuntimePattern` row SHALL match the foreground
   `Kiro.app` process and SHALL NOT match Kiro helper subprocesses
   whose argv contains the substring `Kiro Helper`.
6. THE Kiro `_RuntimePattern` row SHALL NOT modify, remove, or
   reorder any existing row in `_RUNTIME_PATTERNS`.

### Requirement 2: `KiroTaskObserver` declaration

**User Story:** As a Deskmate maintainer, I want a sibling observer to
`AiderTranscriptObserver` that handles Kiro spec metadata, so that
the framework's pluggability is exercised by a second concrete
observer.

#### Acceptance Criteria

1. THE runtime_observers module SHALL define a class
   `KiroTaskObserver` that subclasses `RuntimePhaseObserver`.
2. THE KiroTaskObserver SHALL accept a FilesystemAdapter and a clock
   callable through its constructor and SHALL retain those
   dependencies as read-only attributes.
3. THE KiroTaskObserver SHALL accept the KiroTasksRoot directory path
   through its constructor, defaulting to `~/.kiro/tasks` resolved
   against the running user's home directory.
4. THE KiroTaskObserver SHALL implement every abstract method
   declared by `RuntimePhaseObserver` (`start`, `stop`, `targets`,
   `tick`).
5. THE KiroTaskObserver SHALL NOT spawn any thread, process, or
   asyncio task during `start`, `stop`, `targets`, or `tick`.
6. THE KiroTaskObserver SHALL NOT call any method, getter, or
   property accessor on `SessionStore`, `ApprovalStore`, or
   `AgentRuntimeStore`, and SHALL NOT receive any reference to those
   stores through its constructor or attributes.

### Requirement 3: Activation gating from KIRO discovery

**User Story:** As a Deskmate user, I want the observer to skip all
filesystem work when no Kiro IDE is running, so that the menu-bar
agent does not pay the cost of scanning a directory the user is not
using.

#### Acceptance Criteria

1. WHEN `targets(statuses)` is called and at least one input
   AgentRuntimeStatus has `source == AgentRuntimeSource.KIRO`, THE
   KiroTaskObserver SHALL return a single-element list containing
   exactly one synthetic AgentRuntimeStatus whose `source` equals
   `AgentRuntimeSource.KIRO` and whose `effective_session_id` is
   the wire string `"runtime-kiro-pipeline"`.
2. WHEN `targets(statuses)` is called and no input
   AgentRuntimeStatus has `source == AgentRuntimeSource.KIRO`, THE
   KiroTaskObserver SHALL return an empty list.
3. WHEN the KiroTaskObserver returns an empty list from
   `targets(statuses)` after having returned a non-empty list on a
   previous tick, THE RuntimePhaseObserverRegistry SHALL call
   `stop()` on the KiroTaskObserver per its existing lifecycle
   contract.
4. WHEN `tick(now_ms)` runs after `targets(statuses)` returned an
   empty list for the current tick, THE KiroTaskObserver SHALL emit
   no AgentEvent and SHALL perform no FilesystemAdapter call.
5. THE synthetic AgentRuntimeStatus returned by `targets(statuses)`
   SHALL be constructed by the observer only as a marker for the
   registry's lifecycle hooks and SHALL NOT be forwarded to
   `AgentEventReducer.apply` by the observer.

### Requirement 4: Hash-directory scanning with bound

**User Story:** As a Deskmate maintainer, I want the observer's
filesystem scan to be bounded, so that an unexpectedly large
KiroTasksRoot does not blow the tick budget.

#### Acceptance Criteria

1. WHEN `tick(now_ms)` runs and the observer is active, THE
   KiroTaskObserver SHALL list the direct child directories of
   KiroTasksRoot through the FilesystemAdapter and SHALL treat each
   as a candidate KiroWorkspaceHashDir.
2. THE KiroTaskObserver SHALL inspect at most KiroMaxScannedHashDirs
   (32) KiroWorkspaceHashDir entries per tick.
3. WHEN more than KiroMaxScannedHashDirs candidate directories exist
   under KiroTasksRoot, THE KiroTaskObserver SHALL select the
   subset of 32 entries whose directory `mtime` is largest, breaking
   ties by directory name in ascending lexicographic order, and
   SHALL skip the remainder for that tick.
4. WHEN a candidate KiroWorkspaceHashDir cannot be listed (the
   FilesystemAdapter raises `OSError` or returns a non-existent
   path), THE KiroTaskObserver SHALL skip that directory for the
   tick and SHALL emit no AgentEvent for any spec that would have
   originated from it.
5. WHEN the KiroTasksRoot directory itself does not exist, THE
   KiroTaskObserver SHALL emit no AgentEvent for the tick.

### Requirement 5: Spec meta file enumeration and JSON parsing

**User Story:** As a Deskmate user, I want every recently-modified
spec to surface as a session row, so that I can see all the work my
Kiro instance has in flight without switching windows.

#### Acceptance Criteria

1. WHEN scanning a KiroWorkspaceHashDir, THE KiroTaskObserver SHALL
   enumerate every direct child whose name ends in `.meta.json` and
   SHALL treat each as a candidate KiroSpecMetaFile.
2. WHEN reading a KiroSpecMetaFile, THE KiroTaskObserver SHALL invoke
   the FilesystemAdapter's bytes-reading method declared in
   Requirement 10 with `max_bytes` equal to KiroMetaFileMaxBytes
   (65 536) and SHALL classify a file whose returned size equals
   KiroMetaFileMaxBytes as corrupt for the tick.
3. WHEN a KiroSpecMetaFile's bytes cannot be decoded as UTF-8 or
   parsed as JSON, THE KiroTaskObserver SHALL emit no AgentEvent
   for that file and SHALL log one warning per
   `(KiroWorkspaceHashDir, KiroSpecName, error class)` tuple per
   tick.
4. WHEN the parsed KiroSpecMetaPayload does not contain a top-level
   `tasks` object whose value is a JSON object, THE
   KiroTaskObserver SHALL emit no AgentEvent for that file.
5. WHEN the `tasks` object contains zero entries, THE
   KiroTaskObserver SHALL emit no AgentEvent for that file.
6. WHEN a KiroTaskRecord lacks an `updatedAt` integer field, THE
   KiroTaskObserver SHALL exclude that record from the
   KiroLatestTaskRecord computation and SHALL include the remaining
   records.

### Requirement 6: Workspace path recovery from `specUri`

**User Story:** As a Deskmate user, I want the menu-bar Jump-to-session
action to open the correct workspace folder, so that I land in the
project the Kiro spec belongs to.

#### Acceptance Criteria

1. WHEN computing KiroResolvedWorkspace from a KiroSpecUri, THE
   KiroTaskObserver SHALL invoke `urllib.parse.unquote` on the URI
   and SHALL strip a leading `file://` scheme prefix from the
   result.
2. WHEN the unquoted path contains the substring
   `/.kiro/specs/<spec>/tasks.md` (with `<spec>` matching the
   KiroSpecName), THE KiroTaskObserver SHALL set
   KiroResolvedWorkspace to the substring of the path up to and
   excluding the `/.kiro/specs/` segment.
3. WHEN the unquoted path does not contain the substring
   `/.kiro/specs/`, THE KiroTaskObserver SHALL emit no AgentEvent
   for that spec.
4. WHEN the KiroSpecUri field is missing from every KiroTaskRecord
   in a KiroSpecMetaFile, THE KiroTaskObserver SHALL emit no
   AgentEvent for that file.
5. WHEN multiple KiroTaskRecord entries in a KiroSpecMetaFile carry
   non-empty KiroSpecUri values that resolve to different
   KiroResolvedWorkspace strings, THE KiroTaskObserver SHALL use
   the KiroResolvedWorkspace derived from the
   KiroLatestTaskRecord's KiroSpecUri.
6. THE KiroTaskObserver SHALL set the emitted AgentEvent's `cwd`
   field to KiroResolvedWorkspace.

### Requirement 7: Per-task `executionStatus` mapping

**User Story:** As a Deskmate user, I want the spec row to reflect
whether Kiro is actively working, has queued the task, or has
finished, so that the island colour and label match what I would see
in Kiro itself.

#### Acceptance Criteria

1. WHEN the KiroLatestTaskRecord's `executionStatus` equals the
   string `"succeed"`, THE KiroTaskObserver SHALL set KiroSpecPhase
   to `SessionPhase.COMPLETED` for that file before applying
   Requirement 8.
2. WHEN the KiroLatestTaskRecord's `executionStatus` equals the
   string `"in_progress"`, THE KiroTaskObserver SHALL set
   KiroSpecPhase to `SessionPhase.THINKING` for that file before
   applying Requirement 8.
3. WHEN the KiroLatestTaskRecord's `executionStatus` equals the
   string `"queued"`, THE KiroTaskObserver SHALL set KiroSpecPhase
   to `SessionPhase.RUNNING` for that file before applying
   Requirement 8.
4. WHEN the KiroLatestTaskRecord's `executionStatus` field is
   missing, THE KiroTaskObserver SHALL set KiroSpecPhase to
   `SessionPhase.RUNNING` for that file before applying
   Requirement 8.
5. WHEN the KiroLatestTaskRecord's `executionStatus` value is a
   string not equal to any of `"succeed"`, `"in_progress"`,
   `"queued"`, THE KiroTaskObserver SHALL set KiroSpecPhase to
   `SessionPhase.RUNNING` for that file before applying
   Requirement 8 and SHALL log one warning per
   `(KiroSyntheticSessionId, executionStatus value)` tuple per
   tick.

### Requirement 8: Idle threshold override

**User Story:** As a Deskmate user, I want a spec the user has not
touched for half an hour to roll over to COMPLETED, so that stale
in-flight specs do not stay pinned to THINKING in the session list.

#### Acceptance Criteria

1. WHEN `now_ms - KiroLatestTaskRecord.updatedAt > KiroIdleThresholdMs`
   (1 800 000 ms), THE KiroTaskObserver SHALL override KiroSpecPhase
   to `SessionPhase.COMPLETED` regardless of the value computed by
   Requirement 7.
2. WHEN
   `now_ms - KiroLatestTaskRecord.updatedAt <= KiroIdleThresholdMs`,
   THE KiroTaskObserver SHALL leave KiroSpecPhase equal to the value
   computed by Requirement 7.
3. WHEN KiroLatestTaskRecord.updatedAt is greater than `now_ms` (the
   meta file carries a future timestamp), THE KiroTaskObserver SHALL
   leave KiroSpecPhase equal to the value computed by Requirement 7.

### Requirement 9: Synthetic session emission cardinality

**User Story:** As a Deskmate user, I want every active spec to surface
as its own session row even though they share one Kiro process, so
that I can disambiguate the work happening in different specs.

#### Acceptance Criteria

1. WHEN `tick(now_ms)` produces an AgentEvent for a KiroSpecMetaFile,
   THE KiroTaskObserver SHALL set the event's `session_id` to
   KiroSyntheticSessionId for that
   `(KiroWorkspaceHashDir, KiroSpecName)` pair.
2. THE KiroTaskObserver SHALL set the `source` of every emitted
   AgentEvent to the wire string `"kiro"`.
3. WHEN the KiroSpecPhase derived for a KiroSpecMetaFile equals
   `SessionPhase.COMPLETED`, THE KiroTaskObserver SHALL emit a
   `SessionCompleted` event with `failed = False` for that file.
4. WHEN the KiroSpecPhase derived for a KiroSpecMetaFile equals any
   value other than `SessionPhase.COMPLETED`, THE KiroTaskObserver
   SHALL emit a `SessionActivityUpdated` event with `phase` equal
   to KiroSpecPhase for that file.
5. WHEN multiple KiroSpecMetaFile entries are eligible for emission
   on a single tick, THE KiroTaskObserver SHALL return one
   AgentEvent per eligible file in the list returned by `tick`.
6. THE KiroTaskObserver SHALL set the emitted AgentEvent's `ts_ms`
   field to `now_ms` for every emitted event on a tick.
7. THE KiroTaskObserver SHALL set the title of the underlying
   session by populating event metadata such that the
   AgentEventReducer-driven `SessionInfo.title` for
   KiroSyntheticSessionId becomes KiroSpecTitle.

### Requirement 10: FilesystemAdapter extension and exclusive use

**User Story:** As a Deskmate maintainer, I want every Kiro filesystem
read to flow through the existing FilesystemAdapter seam, so that
tests stay deterministic and the observer never touches the host
filesystem directly.

#### Acceptance Criteria

1. THE FilesystemAdapter SHALL declare a method
   `list_dir(path) -> list[str]` that returns the names of the
   direct children of `path` and SHALL declare a method
   `read_bytes(path, max_bytes) -> bytes` that returns at most
   `max_bytes` bytes from `path`.
2. THE default FilesystemAdapter implementation SHALL back
   `list_dir` with `os.listdir` and SHALL back `read_bytes` with
   a single `open(path, "rb").read(max_bytes)` call.
3. THE KiroTaskObserver SHALL perform every filesystem access
   through the FilesystemAdapter passed to its constructor and
   SHALL NOT invoke `os`, `pathlib`, `open`, or any other module
   for filesystem I/O.
4. WHEN a test passes a fake FilesystemAdapter to the
   KiroTaskObserver, THE KiroTaskObserver SHALL invoke only that
   adapter's methods for filesystem reads.
5. THE FilesystemAdapter `list_dir` and `read_bytes` methods SHALL
   NOT expose any write operation.

### Requirement 11: Mtime backoff per spec meta file

**User Story:** As a Deskmate maintainer, I want unchanged meta files
to short-circuit JSON parsing on every tick, so that a workspace with
many idle specs does not pay 32 × parse cost on each 2 s scanner
cycle.

#### Acceptance Criteria

1. THE KiroTaskObserver SHALL maintain a per-spec cache keyed by
   `(KiroWorkspaceHashDir, KiroSpecName)` storing the last observed
   `stat_mtime_ms` value of the KiroSpecMetaFile.
2. WHEN `tick(now_ms)` runs and the FilesystemAdapter reports a
   `stat_mtime_ms` for a KiroSpecMetaFile equal to the cached value
   for that key, THE KiroTaskObserver SHALL NOT call `read_bytes`
   for that file and SHALL emit no AgentEvent for that file unless
   the idle override in Requirement 11.4 applies.
3. WHEN `tick(now_ms)` runs and the FilesystemAdapter reports a
   `stat_mtime_ms` for a KiroSpecMetaFile different from the
   cached value, THE KiroTaskObserver SHALL update the cache to
   the new value and SHALL proceed with parsing per
   Requirement 5.
4. WHEN the previously emitted KiroSpecPhase for a key is not
   `SessionPhase.COMPLETED` and
   `now_ms - KiroLatestTaskRecord.updatedAt` crosses
   KiroIdleThresholdMs since the previous tick, THE
   KiroTaskObserver SHALL re-parse the cached file and emit a
   `SessionCompleted` event for that key on the current tick even
   if the file's `stat_mtime_ms` has not changed.
5. WHEN a previously cached `(KiroWorkspaceHashDir, KiroSpecName)`
   pair is no longer enumerated by `list_dir` on a tick, THE
   KiroTaskObserver SHALL drop the cache entry for that key
   before the next tick.

### Requirement 12: Tick budget compliance

**User Story:** As a Deskmate user, I want adding the Kiro observer
to keep the menu-bar agent responsive, so that scanning multiple
specs does not introduce visible latency.

#### Acceptance Criteria

1. THE combined wall-clock cost of one
   `AgentRuntimeScanner.scan_once` call and all
   RuntimePhaseObserverRegistry work invoked from it (including
   the KiroTaskObserver) SHALL NOT exceed TickBudgetMs (50 ms)
   when measured against a fixture of 32 KiroWorkspaceHashDir
   entries each containing one 4 KiB KiroSpecMetaFile on a 2020-or-
   later Apple Silicon Mac.
2. WHEN no KiroWorkspaceHashDir's KiroSpecMetaFile has changed
   `stat_mtime_ms` since the previous tick, THE KiroTaskObserver
   SHALL perform exactly one `list_dir` call per scanned
   KiroWorkspaceHashDir plus one `stat_mtime_ms` call per
   enumerated KiroSpecMetaFile and SHALL perform zero `read_bytes`
   calls.
3. THE KiroTaskObserver SHALL NOT recurse below
   KiroWorkspaceHashDir directories. The observer reads only files
   whose path is exactly `<KiroTasksRoot>/<hash>/<name>.meta.json`.

### Requirement 13: Degradation on missing or corrupt metadata

**User Story:** As a Deskmate user, I want a half-written or vanished
meta file to leave the row at its last-known phase instead of
flipping to FAILED, so that transient I/O does not generate noise.

#### Acceptance Criteria

1. IF the FilesystemAdapter raises `FileNotFoundError` while reading
   a KiroSpecMetaFile, THEN THE KiroTaskObserver SHALL emit no
   AgentEvent for that file and SHALL drop the cache entry for
   that key.
2. IF the FilesystemAdapter raises an `OSError` other than
   `FileNotFoundError` while reading a KiroSpecMetaFile, THEN THE
   KiroTaskObserver SHALL emit no AgentEvent for that file and
   SHALL log one warning per
   `(KiroSyntheticSessionId, error class)` tuple per tick.
3. IF the KiroSpecMetaPayload cannot be parsed, THEN THE
   KiroTaskObserver SHALL emit no AgentEvent for that file per
   Requirement 5.3 and SHALL leave the mtime cache entry for that
   file populated so the same byte sequence is not re-parsed on
   subsequent ticks.
4. IF the KiroSpecUri cannot be resolved to a KiroResolvedWorkspace,
   THEN THE KiroTaskObserver SHALL emit no AgentEvent for that
   file and SHALL log one warning per
   `(KiroWorkspaceHashDir, KiroSpecName)` tuple per tick.

### Requirement 14: Observer crash isolation inheritance

**User Story:** As a Deskmate maintainer, I want a buggy
KiroTaskObserver to not take down the runtime scanner, so that the
rest of the session list keeps updating.

#### Acceptance Criteria

1. IF KiroTaskObserver `start`, `stop`, `targets`, or `tick` raises
   any `Exception`, THEN THE RuntimePhaseObserverRegistry SHALL
   apply the existing crash-isolation contract (catch, log one
   warning, discard partial output) per the
   `runtime-phase-observers` framework.
2. WHEN the KiroTaskObserver raises ObserverConsecutiveCrashLimit
   (3) consecutive exceptions across ticks, THE
   RuntimePhaseObserverRegistry SHALL stop calling the
   KiroTaskObserver for the remainder of the scanner's lifetime
   per the existing contract.
3. THE KiroTaskObserver SHALL NOT introduce any new exception
   handler outside its own module that catches exceptions on
   behalf of `RuntimePhaseObserverRegistry`.

### Requirement 15: Default registry wiring

**User Story:** As a Deskmate maintainer, I want the new observer
registered through the existing `make_default_registry` factory, so
that no application bootstrap code needs to be touched.

#### Acceptance Criteria

1. THE `make_default_registry` factory in
   `agent/deskmate_agent/agent_runtime.py` SHALL append a
   KiroTaskObserver instance to its observers list immediately
   after the existing AiderTranscriptObserver instance.
2. THE KiroTaskObserver instance constructed by
   `make_default_registry` SHALL receive the same FilesystemAdapter
   instance that AiderTranscriptObserver receives.
3. THE `make_default_registry` factory SHALL NOT add any constructor
   parameter beyond those it already exposes
   (`reducer`, `session_store`, `fs`).
4. THE `App._build_app` call site SHALL NOT be modified by this
   feature.

### Requirement 16: Test suite preservation

**User Story:** As a Deskmate maintainer, I want the existing test
baselines to keep passing, so that the new observer lands without
silent regressions.

#### Acceptance Criteria

1. THE existing pytest suite under `agent/tests/` SHALL continue to
   collect and pass at the pre-feature count, plus any new tests
   added for this feature.
2. THE existing Swift smoke suite under
   `DeskmateApp/Sources/DeskmateCoreSmoke` SHALL continue to pass
   at the pre-feature count.
3. THE feature SHALL NOT add any third-party runtime dependency to
   the `[project] dependencies` section of `agent/pyproject.toml` or
   to `DeskmateApp/Package.swift`. THE feature MAY add a development-
   only dependency to the existing `[project.optional-dependencies]
   dev` section of `agent/pyproject.toml` provided that dependency
   is not imported from any module under `agent/deskmate_agent/`. The
   `urllib.parse.unquote` import SHALL be sourced from the Python
   standard library.
4. THE feature SHALL NOT modify any file under
   `DeskmateApp/Sources/DeskmateMenuBarApp/`,
   `DeskmateApp/Sources/DeskmateShellApp/`, or
   `DeskmateApp/Sources/DeskmateCore/`.
