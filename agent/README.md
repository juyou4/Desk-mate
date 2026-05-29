# deskmate-agent

Python core for Deskmate, V10 unified baseline. It owns the bridge server,
memory stores, proactive/reactive routing, typed interaction handling,
reminders, approvals, sessions, skills, character-pack discovery, and
performance/degradation plumbing.

## Install (editable, with dev deps)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

Add `'.[runtime]'` when running the optional LLM path:

```bash
pip install -e '.[dev,runtime]'
```

## Test

```bash
pytest
ruff check .
```

From the repo root, run the quick performance smoke with the project venv:

```bash
./scripts/perf_smoke.sh --duration-s 60
```

## Hook Ingest

External agent hooks should write through the CLI file queue instead of the
Swift IPC socket:

```bash
echo '{"session_id":"s1","event":"session.started","title":"Codex demo"}' \
  | deskmate hook ingest --source codex
```

The running Python agent watches `~/.deskmate/hook-events/`, normalizes each
event into a session/approval update, then pushes the usual intents and
`state.snapshot` back to Swift.

Internally, source payloads flow through a shared reducer:

```text
source payload -> HookEvent -> AgentEvent -> AgentEventReducer -> stores
```

This keeps Codex, Claude, Cursor, and future app-server/transcript inputs on
the same session-state path.

If the `deskmate` console script is not installed in the active virtualenv,
use the module entrypoint:

```bash
echo '{"session_id":"s1","event":"session.started","title":"Codex demo"}' \
  | python -m deskmate_agent.cli hook ingest --source codex
```

## Hook Installers

Hook installation is opt-in and reversible. Deskmate only manages entries it
marks as `Managed by Deskmate`; user hooks are preserved.

```bash
deskmate hook install --source codex
deskmate hook status --source codex
deskmate hook uninstall --source codex

deskmate hook install --source claude
deskmate hook install --source cursor
```

Codex uses `~/.codex/config.toml` for `[features].codex_hooks = true` and
`~/.codex/hooks.json` for managed hook entries. Claude uses
`~/.claude/settings.json`. Cursor uses `~/.cursor/hooks.json`.

For nonstandard installs or tests:

```bash
deskmate hook install --source cursor --config /tmp/hooks.json
deskmate hook install --source codex --command "python -m deskmate_agent.cli hook ingest --source codex"
```

## Codex.app app-server

When launched with the standard module entrypoint, the resident agent also
tries to attach to Codex.app's local `app-server` transport:

```bash
python -m deskmate_agent
```

This integration is read-only from Deskmate's side: it starts the bundled
`codex app-server --listen stdio://`, lists loaded threads, and converts
Codex thread/turn notifications into the same `AgentEventReducer` path used
by hook events. It creates island sessions for loaded/active Codex threads,
marks waiting approval/input as actionable, and sets jump targets such as
`codex://threads/<thread-id>`.

Disable it for debugging or deterministic local runs:

```bash
DESKMATE_CODEX_APP_SERVER=0 python -m deskmate_agent
```

If Codex.app is not installed or the detected `codex` binary does not support
`app-server`, startup stays best-effort and the hook queue / passive runtime
scanner continue to work.

## Layout

```
deskmate_agent/
├── __init__.py
├── logging_setup.py      # structlog + trace_id ContextVar (V10 L3)
├── app.py                # top-level runtime composition
├── dispatcher.py         # reactive vs proactive chain split
├── bridge/               # UDS framing, batching, heartbeat
├── memory/               # aiosqlite session/profile/coding stores
├── proactive/            # rule prefilter, cooldown, nudges
├── skills/               # metadata registry + first-party skills
└── protocol/             # single source of truth for IPC types (V10 L1)
    ├── envelope.py
    ├── actions.py        # InteractionAction (L1-F / I8)
    ├── intents.py        # CompanionIntent (L1-C)
    ├── state.py          # DomainState / Surface states (L1-A/B, I1/I5)
    └── character_pack.py # CharacterPackManifest (L1-D / I4)
tests/
└── test_*.py
```

## Why isn't my Codex session updating?

The island shows a Codex session row as soon as the agent detects the
`codex` process, but **phase changes** (thinking → running tool → editing →
completed) require an event source. Without one, the session stays at
`RUNNING` with a desaturated `?` chip after ~30 seconds.

Two ways to get live phase updates:

### Option A: Install hooks (recommended)

```bash
deskmate hook install --source codex
deskmate hook status --source codex   # verify: "installed"
```

This writes managed entries into `~/.codex/config.toml` and
`~/.codex/hooks.json`. Every Codex lifecycle event (SessionStart,
UserPromptSubmit, PreToolUse, PostToolUse, Stop) flows through the
file queue at `~/.deskmate/hook-events/` and the island updates in
real time.

To remove:

```bash
deskmate hook uninstall --source codex
```

### Option B: Codex.app app-server

If you have Codex.app installed (`/Applications/Codex.app`), the agent
automatically connects to its local `app-server` transport on startup.
No manual setup needed — thread/turn notifications arrive via stdio
JSON-RPC.

Disable for debugging:

```bash
DESKMATE_CODEX_APP_SERVER=0 python -m deskmate_agent
```

### Verifying

After installing hooks or confirming app-server is connected, run a
Codex task that triggers a tool call. The island should transition
from the desaturated `RUNNING ?` chip to coloured phase labels
(THINKING → RUNNING_TOOL → COMPLETED).

If it still doesn't update, check the agent logs for
`hooks.consume_failed` or `codex_app_server.start_failed` warnings.
