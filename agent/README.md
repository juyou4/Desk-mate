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

## Runtime Diagnostics

To see what the passive scanner can currently detect without starting Swift,
run one read-only process scan:

```bash
deskmate runtime scan
deskmate runtime scan --json
```

The output lists detected IDEs, CLI agents, terminals, phase, pid, and any
terminal metadata the scanner could infer. For example, a Codex process
launched inside Ghostty may include `terminal=Ghostty@ttys004`; that metadata
is also written into `SessionInfo.extras` and used by session jump-back.

For deterministic diagnostics, feed captured `ps` output:

```bash
/bin/ps -axo pid=,ppid=,tty=,comm=,args= > /tmp/deskmate-ps.txt
deskmate runtime scan --ps-file /tmp/deskmate-ps.txt --json
```

## Natural-Language Computer Control and Reminders

The reactive chat chain now has a small safety-first control layer before the
LLM / canned reply composer. It recognizes simple commands and executes only
low-risk macOS actions:

```text
open Terminal
打开 terminal
open https://example.com
search for deskmate agent
open /Users/me/project
switch to Cursor
open /Users/me/project in Windsurf
show /Users/me/project in Finder
open Bluetooth settings
open Weather
帮我看天气
copy "hello from Deskmate"
mute
set volume to 35
screenshot
lock screen
sleep mac
quit Terminal
remind me to stretch in 10 minutes
timer for 5 minutes
帮我设置一个 3 分钟倒计时
10分钟后提醒我喝水
what reminders do I have?
cancel reminder <reminder_id>
```

Supported actions:

- Open a known application via `open -a` (`Terminal`, `Finder`, `Safari`,
  `Chrome`, `Cursor`, `Windsurf`, `VSCode`, `Xcode`, `iTerm`, `Ghostty`,
  `Weather`, etc.).
- Focus a known application via AppleScript `activate`.
- Open `http` / `https` URLs.
- Open an existing local file or folder path.
- Open an existing local file or folder path with a known application.
- Reveal an existing local file or folder path in Finder via `open -R`.
- Open allowlisted System Settings panes via `x-apple.systempreferences:`.
- Run a web search by opening Google search results.
- Mute, unmute, or set output volume with fixed AppleScript commands.
- Take a screenshot after explicit approval via fixed `screencapture -x` output
  to `~/Desktop/deskmate-screenshot-<timestamp>.png`.
- Set clipboard text after an explicit approval via a fixed AppleScript command.
- Lock the screen or put the Mac to sleep after an explicit approval.
- Quit a known application after an explicit approval. The request creates an
  approval row/bubble first; the fixed AppleScript quit command runs only after
  the user chooses Allow.
  If the in-memory pending action is lost, the resolver can restore the same
  fixed action from the approval's stored `action_kind` / `target` metadata;
  unknown action kinds are ignored.
- Create a pending reminder/timer from explicit relative times. These reuse the
  existing `ReminderStore` and `ReminderScheduler`, so due reminders appear via
  the same pet bubble / island state path as other reminders.
- List pending reminders and cancel a pending reminder by id. These management
  commands run locally even without an LLM API key.
- Create, list, search, complete, and cancel persistent tasks/todos in
  `tasks.db` from explicit commands such as `add task Polish island`,
  `list tasks`, `search tasks island`, `continue current task`, and
  `complete task <task_id>`. These management commands also run locally without
  an LLM API key. `continue current task` returns the active task, next step,
  and related tool history when `tool_actions.db` is available, preferring
  direct task-id-linked history before keyword matches over task titles, notes,
  and step text.

Anything not recognized falls through to normal chat. The layer deliberately
does not run arbitrary shell commands, type into apps, click UI elements, or
modify files. Higher-risk actions must go through an explicit approval/tool
policy before execution.

## Memory and LLM Tool Calls

When `DESKMATE_LLM_API_KEY` is configured, Deskmate uses an OpenAI-compatible
chat-completions endpoint:

```bash
DESKMATE_LLM_API_KEY=sk-...
DESKMATE_LLM_BASE_URL=https://api.openai.com/v1
DESKMATE_LLM_MODEL=gpt-4o-mini
DESKMATE_LLM_STREAMING=0   # optional: use non-streaming path for tool-call tests
DESKMATE_LLM_TOOL_TIMEOUT_S=8  # optional: per-tool timeout; 0/off disables it
DESKMATE_LLM_TOOL_ROUND_LIMIT=3  # optional: chained tool-call rounds; clamped to 1-5
```

For DeepSeek-compatible local testing, point the same variables at the DeepSeek
endpoint without committing the key:

```bash
DESKMATE_LLM_BASE_URL=https://api.deepseek.com
DESKMATE_LLM_MODEL=deepseek-v4-flash
DESKMATE_LLM_STREAMING=1
```

The chat/tool path has persisted memory/context layers:

- `chat.db`: persistent recent chat transcript, including assistant tool calls
  and tool-result messages. This keeps short conversation context across agent
  restarts, and can be searched on demand when relevant context falls outside
  the rolling prompt window. It also maintains a bounded `chat_summaries`
  extractive summary, which is injected only when it covers older turns not
  already present in the current prompt window.
- `profile.db` key `memories.facts`: durable user facts/preferences. These are
  injected as a bounded system context on both streaming and non-streaming
  requests, so remembered facts are available even when the model does not call
  a recall tool.
- Memory suggestions from ordinary conversation are approval-gated. A small
  deterministic extractor proposes candidates for clear stable-preference
  phrases such as `My favorite editor is Cursor` or `I usually use Ghostty for
  terminal work`; the LLM can also create a pending `memory_suggestion`
  approval. In both cases the fact is written to `profile.db` only after the
  user chooses Allow. If the key already exists with a different value, the
  approval is presented as an update and keeps the previous value in metadata.
- `tasks.db`: persistent user-visible Deskmate task/todo ledger. These records
  are separate from tool-call lifecycles: a task can stay open across many chat
  turns and many tool calls. Explicit user requests can create, list, search,
  and update these high-level work items through allowlisted tools. Task
  candidates inferred from ordinary conversation are approval-gated and are
  written only after the user chooses Allow.
  The LLM prompt receives both the active task list and a focused task summary:
  the in-progress task wins, otherwise the newest open task wins, with current
  step, next pending step, and step-completion progress included as read-only
  continuity context.
  When the user explicitly asks to continue/resume the current task, the prompt
  also receives a bounded read-only `deskmate_task_context` snapshot with task
  steps plus related tool-task lifecycles, tool-call results, and tool lessons.
  Active tasks also feed a low-priority stale-task nudge watcher. By default,
  tasks that have not changed for 4 hours can produce one pet bubble plus a
  transient island notification, with a 6 hour per-task cooldown. This watcher
  only reads `tasks.db` and emits reminders; it never executes the task or
  calls tools automatically.
- `tool_actions.db`: persistent Deskmate-owned tool-call audit log. It records
  completed, failed, and duplicate tool calls with sanitized arguments and
  structured summaries, so later turns can recall what action was attempted
  without re-running it. Secret-like fields such as tokens, passwords, cookies,
  and clipboard payloads are redacted before persistence. Each record keeps
  `action`, `target`, `outcome`, and `needs_user` summary fields; a bounded
  summary of recent entries is injected into the LLM prompt as read-only
  context on both streaming and non-streaming paths.
  `tool_lessons` derives durable, searchable lessons from non-duplicate tool
  calls, grouped by conversation/tool/target/status. These lessons let later
  turns remember that a similar local action succeeded, failed, or required
  user approval without replaying the original action.
  `tool_tasks` groups all tool calls from one user turn into a recoverable task
  lifecycle with status, action counts, failure counts, and a compact summary;
  recent tasks are also injected as read-only context. On startup, stale
  `running` tasks from a previous interrupted process are finalized as failed
  metadata only; Deskmate never replays old tool calls automatically.
  Approval-gated memory/task resolutions and deterministic task commands are
  recorded here too, so a later turn can tell whether a suggested durable write
  was just approved, skipped, or updated, and whether the user just started,
  paused, advanced, completed, or failed to match a Deskmate task. When a
  deterministic task command resolves to exactly one durable task, its audit
  row is linked to that task id so resume/task-context views can gather the
  relevant command history. `deskmate_task_context` reads those direct
  task-id-linked action and lesson rows before falling back to keyword search
  over task titles, notes, and step text.

Explicit memory/task commands run locally before the LLM / canned reply path,
so they work even without an API key:

```text
remember my favorite editor is Cursor
what do you remember?
你记得我什么
what do you remember about editor
forget favorite editor
忘记 favorite editor
what did we discuss about bluebird
之前聊过 bluebird 吗
add task Polish island task lane notes: Keep it compact
todo: Verify Codex hook demo
list tasks
search tasks island
complete task <task_id>
cancel task <task_id>
```

These deterministic commands only write durable facts to `profile.db` or read
the current conversation's `chat.db` rows, or update explicit user-visible
tasks in `tasks.db`. They do not execute tools, read files, run shell commands,
or search other conversations.

For local diagnostics without starting Swift or touching the bridge, inspect
the persisted stores directly:

```bash
deskmate memory summary
deskmate memory summary --json
deskmate memory task <task_id>
deskmate memory task <task_id> --json
deskmate memory task-context <task_id>
deskmate memory task-context <task_id> --json
deskmate memory tool-task <tool_task_id>
deskmate memory tool-task <tool_task_id> --json
```

`memory task-context` uses the same direct task-id first, title/notes/step
keyword fallback strategy as the runtime task-context tool, so local diagnostics
show the same recovery evidence the agent can see.

For scriptable task management without starting Swift, use the task CLI:

```bash
deskmate task add "Polish island task lane" --notes "Keep it compact"
deskmate task list
deskmate task list --status all --json
deskmate task search island
deskmate task update <task_id> --status in_progress --notes "Working now"
deskmate task done <task_id>
deskmate task cancel <task_id>
```

Available LLM tools:

- `deskmate_schedule_reminder`: creates a reminder through the existing
  `ReminderStore` / scheduler path.
- `deskmate_list_reminders`: lists current local reminders by status without
  creating, firing, or cancelling anything.
- `deskmate_cancel_reminder`: cancels a local reminder by `reminder_id` after
  an explicit user request to cancel a reminder.
- `deskmate_computer_action`: routes a concise command through the same
  safety-first computer-control parser listed above. Sensitive actions still
  create approvals.
- `deskmate_remember_fact`: stores a durable fact/preference into
  `profile.db` after an explicit user request to remember it.
- `deskmate_suggest_memory`: creates an approval-gated durable memory candidate
  from ordinary conversation. It does not write `profile.db` until approved.
- `deskmate_recall_memory`: searches durable facts by keyword and returns a
  tool-result message to the model.
- `deskmate_list_memories`: lists current durable facts without a keyword. Use
  it for questions like `what do you remember about me?`.
- `deskmate_forget_memory`: deletes durable facts by key/keyword after an
  explicit user forget/remove request.
- `deskmate_search_chat_memory`: searches the current conversation's persisted
  chat transcript by keyword and returns scoped user/assistant/tool messages to
  the model.
- `deskmate_create_task`: creates a persistent user-visible task/todo item in
  `tasks.db` after an explicit user request to add or track a task.
- `deskmate_suggest_task`: creates an approval-gated persistent task/todo
  candidate from ordinary conversation. It does not write `tasks.db` until
  approved.
- `deskmate_list_tasks`: lists persistent tasks by status. Defaults to active
  `open` / `in_progress` items.
- `deskmate_search_tasks`: searches persistent tasks by id, title, or notes.
- `deskmate_update_task`: updates a persistent task's title, notes, or status
  (`open`, `in_progress`, `done`, `cancelled`).
- `deskmate_recent_tool_actions`: reads or filters recent Deskmate-owned
  tool-call results from `tool_actions.db`, including drill-down by
  `tool_task_id`. This is read-only; it never executes a new action.
- `deskmate_recent_tool_tasks`: reads or keyword-searches recent
  Deskmate-owned multi-step tool task lifecycles from `tool_actions.db`. This
  is read-only; it never executes a new action.
- `deskmate_task_context`: reads a persistent task together with related
  tool-task lifecycles, tool-call results, and tool lessons. This is read-only
  and is the main resume-work context view for tracked tasks.
- `deskmate_recent_tool_lessons`: reads or keyword-searches durable lessons
  derived from previous Deskmate-owned tool calls. This is read-only and helps
  avoid repeating known failures.
- `deskmate_tool_task_details`: reads one persisted tool task plus its
  associated tool-call action summaries by `task_id`. This is read-only; it
  never executes a new action.

The tool surface is deliberately high-level and allowlisted. There is no LLM
tool for arbitrary shell commands, raw filesystem edits, unrestricted UI
clicking, or free-form AppleScript. The execution layer also enforces local
policy before running a tool: direct durable memory writes require explicit
user wording such as `remember` / `记住`, and durable memory deletion requires
explicit forget/remove wording. Ordinary preference-like conversation should
use `deskmate_suggest_memory`, which creates an approval instead of writing
`profile.db` directly. Direct task creation/update similarly requires explicit
task wording such as `add task`, `todo:`, or `complete task`; inferred todos
should use `deskmate_suggest_task`, which creates an approval instead of
writing `tasks.db` directly.

LLM tool calls on both streaming and non-streaming paths are observable as
Deskmate agent sessions. Each tool call emits `running_tool` then `completed`
/ `failed` events through the shared `AgentEventReducer`, using session ids
like `deskmate-tools-default`. The island/session list therefore sees
Deskmate's own tool work on the same path as Codex/Claude hook activity.
The session extras also carry `tool_task_id`, `tool_task_status`, and
`tool_task_summary`, so the island can show task-level progress instead of
only a single tool result.
Within one user turn, the LLM may chain a bounded number of tool-call rounds
(default `3`, max `5`), so it can do flows such as recall memory first and then
schedule a reminder from the recalled value. When the limit is reached, the
composer asks the model to summarize existing tool results instead of calling
another tool.

## External Island Modules

External agents can register a compact/live-activity display module without
opening the Swift IPC socket:

```bash
deskmate island module register kiro.spec \
  --kind live_activity \
  --title KIRO \
  --activity-prefix kiro-spec- \
  --subtitle '{detail}' \
  --image k.circle \
  --priority 80
```

The CLI writes a spec into `~/.deskmate/module-registrations/`. The resident
agent forwards it as a typed `register_module` intent and replays the latest
registration for each id when Swift reconnects.

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

## Codex Local Transcripts

The resident agent also reads recent Codex rollout JSONL files from
`~/.codex/sessions/**/*.jsonl` as a read-only fallback. This is not a
replacement for hooks or app-server notifications; it is a recovery path so
Deskmate can restore visible Codex sessions after startup or reconnect.

Transcript rows are normalized through the same `AgentEventReducer` path and
preserve:

- `last_user` / `last_assistant`
- `tool_name` and shell command when a function call is present
- coarse phase (`running_tool`, `testing`, `editing`, `completed`, `failed`)
- `cwd` and `codex://threads/<thread-id>` jump target

This watcher is enabled by default when using `python -m deskmate_agent`.
Disable it for deterministic debugging:

```bash
DESKMATE_CODEX_TRANSCRIPTS=0 python -m deskmate_agent
```

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

First check passive detection:

```bash
deskmate runtime scan
```

Then, after installing hooks or confirming app-server is connected, run a
Codex task that triggers a tool call. The island should transition from the
desaturated `RUNNING ?` chip to coloured phase labels such as `THINKING`,
`RUNNING_TOOL`, `TESTING`, and `COMPLETED`.

If it still doesn't update, check the agent logs for
`hooks.consume_failed` or `codex_app_server.start_failed` warnings.
