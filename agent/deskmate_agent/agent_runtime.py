"""Passive runtime discovery for IDEs and local agent CLIs.

V1 deliberately avoids installing hooks or touching tool configuration.
It observes local processes and turns them into lightweight runtime
sessions so the island/session list can show that Codex, Claude Code,
Cursor, Windsurf, VSCode, etc. are alive even before a richer hook event
arrives.

Design notes
~~~~~~~~~~~~

* **Table-driven classifier.** Every supported runtime is a row in
  :data:`_RUNTIME_PATTERNS`. Adding a new agent / IDE is a one-line
  edit instead of a `if` ladder. The order of the table is the
  match priority — more-specific patterns must come before
  generic fallbacks (e.g. ``"node"`` interpreters running an
  agent CLI script).
* **Substring + executable matching.** The legacy logic only looked
  at the executable basename, so a Homebrew shim like
  ``node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js``
  fell through. We now also match against the full args line so an
  interpreter spawning an agent's bundled JS / Python entry point
  still classifies correctly.
* **Electron helper dedupe.** Cursor / VSCode / Windsurf each spawn
  4-8 ``Helper (Renderer)`` / ``Helper (GPU)`` children that should
  fold into the parent application. We keep the topmost match per
  ``(source, root pid)`` group and drop the rest, so the session
  list shows one row per running IDE instead of a renderer swarm.
* **Best-effort workspace hint.** Cursor / VSCode / Windsurf are
  often launched with the workspace path either as the last
  positional arg or as ``--folder-uri file://...``. We pluck the
  hint into ``cwd`` so the menu-bar "Jump to session" path can
  open the folder; ``None`` is the safe default when nothing
  obvious is in the args.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:  # pragma: no cover — type-only imports for hints
    # Imported lazily to avoid the runtime cycle:
    # ``runtime_observers`` imports :class:`AgentRuntimeStatus` from
    # this module, while this module wants to type-hint the
    # observer-pipeline classes for ``make_default_registry`` and
    # the scanner ctor seam (Requirement 4.8 wiring).
    from .agent_events import AgentEventReducer
    from .runtime_observers import (
        FilesystemAdapter,
        RuntimePhaseObserverRegistry,
    )

from pydantic import BaseModel, ConfigDict, Field

from .logging_setup import get_logger
from .protocol.state import Priority
from .sessions import SessionInfo, SessionPhase, SessionState, SessionStore

_LOG = get_logger("deskmate_agent.agent_runtime")


class AgentRuntimeKind(StrEnum):
    GUI_IDE = "gui_ide"
    CLI_AGENT = "cli_agent"
    HOOK_SESSION = "hook_session"


class AgentRuntimeSource(StrEnum):
    """Stable identifier for the runtime that produced a session row.

    Kept as a flat enum so Swift / Python share one wire string.
    Adding a new value requires:

    1. A row in :data:`_RUNTIME_PATTERNS` (Python detects the
       process).
    2. (Optional) A pretty label in
       :py:meth:`SessionRow.sourceLabel` Swift switch — the default
       branch already PrettyPrints unknown sources so this is only
       needed when the auto-derived label is awkward.
    """

    # CLI agents
    CODEX = "codex"
    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    AIDER = "aider"
    GEMINI = "gemini"
    KIMI = "kimi"
    QWEN = "qwen"
    FACTORY_DROID = "factory_droid"
    CODEBUDDY = "codebuddy"
    QODER = "qoder"

    # GUI IDEs / editors
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    VSCODE = "vscode"
    XCODE = "xcode"
    JETBRAINS = "jetbrains"
    ZED = "zed"
    TRAE = "trae"
    SUBLIME = "sublime"
    FLEET = "fleet"
    NOVA = "nova"
    NEOVIM = "neovim"
    GITHUB_DESKTOP = "github_desktop"

    # Terminals (often host CLI agents)
    TERMINAL = "terminal"
    ITERM = "iterm"
    GHOSTTY = "ghostty"
    WEZTERM = "wezterm"
    KITTY = "kitty"
    WARP = "warp"

    # V10 kiro-task-observer Requirement 1.1 — Kiro IDE source.
    # Appended at the end of the enum so wire-format ordering of
    # existing values is unaffected.
    KIRO = "kiro"

    UNKNOWN = "unknown"


class AgentRuntimeStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    source: AgentRuntimeSource = AgentRuntimeSource.UNKNOWN
    kind: AgentRuntimeKind
    process_id: int | None = None
    parent_pid: int | None = None
    display_name: str = ""
    command: str = ""
    cwd: str | None = None
    # V10 runtime-phase-observers Requirement 2.1 — resolved
    # workspace root (output of :func:`detect_workspace_root` over
    # ``cwd``). ``None`` when ``cwd`` is missing or no marker file is
    # found in any ancestor. Surfaced on the wire so observers and
    # the session-list title formatter share a single derivation
    # path.
    workspace: str | None = None
    bundle_id: str | None = None
    window_title: str | None = None
    session_id: str | None = None
    phase: SessionPhase = SessionPhase.RUNNING
    priority: Priority = Priority.P2
    last_seen_ms: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def effective_session_id(self) -> str:
        if self.session_id:
            return self.session_id
        prefix = self.source.value
        if self.process_id is not None:
            return f"runtime-{prefix}-{self.process_id}"
        return f"runtime-{prefix}-{self.kind.value}"


@dataclass(frozen=True)
class ProcessRow:
    pid: int
    ppid: int
    comm: str
    args: str
    tty: str = ""


# ---------------------------------------------------------------------------
# Workspace root detection (V10 runtime-phase-observers Requirement 1)
# ---------------------------------------------------------------------------


# Exhaustive marker list for V1 — Requirement 1.4. Add new entries
# only when the absence is causing user-visible misclassifications;
# the design deliberately keeps this small so a stray ``.git`` deep
# inside ``node_modules`` cannot pin the workspace to the wrong root.
_WORKSPACE_MARKERS: tuple[str, ...] = (
    ".git",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "Package.swift",
)


def detect_workspace_root(
    cwd: str | None,
    *,
    fs_exists: Callable[[str], bool] = os.path.exists,
) -> str | None:
    """Walk ancestors of ``cwd`` and return the deepest one carrying a
    workspace marker, falling back to ``cwd`` itself when no ancestor
    matches (V10 runtime-phase-observers Requirement 1.1 / 1.2).

    "Deepest" means closest to ``cwd``: by walking parent-ward and
    short-circuiting on the first match we automatically pick the
    nearest ancestor, which keeps a monorepo's sub-project taking
    precedence over an outer ``.git`` (Requirement 1.1).

    The walk halts at the filesystem root (``parent == current``,
    Requirement 1.6) and aborts gracefully on ``OSError`` so a
    permission glitch on one ancestor does not crash the scanner
    (Requirement 1.5).
    """
    # Requirement 1.3: ``None`` in → ``None`` out so callers can
    # safely thread an optional ``cwd`` through without a sentinel.
    if not cwd:
        return None

    current = os.path.normpath(cwd)
    deepest_match: str | None = None
    while True:
        # Requirement 1.5: any OSError aborts the walk and falls
        # back to whatever match we have already accepted (or ``cwd``
        # if no match yet).
        try:
            for marker in _WORKSPACE_MARKERS:
                if fs_exists(os.path.join(current, marker)):
                    # Closest-to-cwd wins (Requirement 1.1) — short-
                    # circuit so we never overwrite a deeper match
                    # with a shallower one further up the tree.
                    return current
        except OSError:
            return deepest_match or cwd
        parent = os.path.dirname(current)
        # Requirement 1.6: terminate at filesystem root so we never
        # recurse forever on a synthetic / circular path.
        if parent == current:
            break
        current = parent
    # Requirement 1.2: nothing matched — fall back to the original
    # ``cwd`` so downstream consumers always have *some* workspace
    # hint when ``cwd`` is set.
    return deepest_match or cwd


@dataclass(frozen=True)
class _RuntimePattern:
    """Single declarative entry in the classifier table.

    Match semantics:

    * If both ``executables`` and ``arg_needles`` are set,
      matching is **either / or** by default — the exec or the
      args may identify the runtime. This is what lets us list
      ``Cursor.app`` as either an exec match (``cursor``) or an
      args match (``cursor.app``) and accept both.
    * Some rules genuinely need **both** to match, e.g.
      ``node`` running ``@anthropic-ai/claude-code/cli.js`` —
      neither half is unique on its own. Those rows set
      ``require_all=True`` so we only fire when the executable
      *and* an arg needle agree.
    * ``avoid_needles`` is always a hard reject regardless of
      ``require_all`` — used to keep ``code`` from picking up
      ``code helper`` in the same regex pass before the dedupe
      stage runs.
    * ``executables`` and ``arg_needles`` are case-insensitive.
    """

    source: AgentRuntimeSource
    kind: AgentRuntimeKind
    display_name: str
    executables: tuple[str, ...] = ()
    arg_needles: tuple[str, ...] = ()
    avoid_needles: tuple[str, ...] = ()
    bundle_id: str | None = None
    helper_needles: tuple[str, ...] = ()
    """Args / comm substrings that mean *this is a renderer/helper*
    spawned by the main app process. Helpers are dropped during
    the dedupe pass — we only keep one session per IDE."""
    require_all: bool = False
    """When ``True``, the row only matches when BOTH ``executables``
    and ``arg_needles`` produce a hit. Reserved for interpreter-
    based runners (``node`` / ``python`` / ``bun`` shimming an
    agent CLI) where neither field alone is unique."""


# Table of supported runtimes. Order matters — patterns higher up
# win over lower ones. We deliberately list the more specific
# matchers (full bundle paths, npm shims) before the generic
# ``executable in {...}`` rules.
_RUNTIME_PATTERNS: tuple[_RuntimePattern, ...] = (
    # --- CLI agents ---------------------------------------------------------
    # Claude Code: Anthropic ships an npm-installed CLI that runs as
    # ``node <prefix>/@anthropic-ai/claude-code/cli.js``. Match both
    # the npm-resolved binary and the underlying interpreter path.
    _RuntimePattern(
        source=AgentRuntimeSource.CLAUDE_CODE,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Claude Code CLI",
        executables=("claude", "claude-code"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.CLAUDE_CODE,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Claude Code CLI",
        executables=("node", "bun", "deno"),
        arg_needles=("@anthropic-ai/claude-code", "claude-code/cli", "claude-code/dist"),
        require_all=True,
    ),
    # Codex CLI: official npm wrapper + the Rust rewrite that ships
    # in the Codex desktop app.
    _RuntimePattern(
        source=AgentRuntimeSource.CODEX,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Codex CLI",
        executables=("codex", "codex-rs", "codex-cli"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.CODEX,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Codex CLI",
        executables=("node", "bun"),
        arg_needles=("@openai/codex", "codex/cli.js", "openai-codex"),
        require_all=True,
    ),
    # OpenCode: SST's terminal coding agent.
    _RuntimePattern(
        source=AgentRuntimeSource.OPENCODE,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="OpenCode CLI",
        executables=("opencode",),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.OPENCODE,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="OpenCode CLI",
        executables=("node", "bun"),
        arg_needles=("@sst/opencode", "opencode/dist", "opencode/cli"),
        require_all=True,
    ),
    # Aider — Python-based, frequently launched as ``aider`` or via
    # ``python -m aider``. The interpreter rule keeps us honest when
    # the user uses pipx/poetry-managed envs.
    _RuntimePattern(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Aider",
        executables=("aider",),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.AIDER,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Aider",
        executables=("python", "python3", "python3.11", "python3.12"),
        arg_needles=("-m aider", "/aider/main.py", "/aider-chat/"),
        require_all=True,
    ),
    # Gemini CLI (Google).
    _RuntimePattern(
        source=AgentRuntimeSource.GEMINI,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Gemini CLI",
        executables=("gemini",),
        # ``gemini`` alone is too ambiguous (could be unrelated
        # ``gemini-protocol`` browser); require an arg hint.
        arg_needles=("--model", "--prompt", "google-gemini", "ai-cli"),
        require_all=True,
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.GEMINI,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Gemini CLI",
        executables=("node", "bun"),
        arg_needles=("@google/gemini-cli", "gemini-cli/dist"),
        require_all=True,
    ),
    # Moonshot Kimi CLI.
    _RuntimePattern(
        source=AgentRuntimeSource.KIMI,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Kimi CLI",
        executables=("kimi", "kimi-cli"),
    ),
    # Qwen Code (Alibaba).
    _RuntimePattern(
        source=AgentRuntimeSource.QWEN,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Qwen Code CLI",
        executables=("qwen", "qwen-code"),
    ),
    # Factory.ai Droid CLI — its launcher is ``droid``.
    _RuntimePattern(
        source=AgentRuntimeSource.FACTORY_DROID,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Factory Droid",
        executables=("droid", "factory-droid"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.FACTORY_DROID,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Factory Droid",
        executables=("factory",),
        # Plain ``factory`` is ambiguous, require an args hint.
        arg_needles=("droid", "factory.ai", "@factoryai/", "factory-cli"),
        require_all=True,
    ),
    # Tencent CodeBuddy.
    _RuntimePattern(
        source=AgentRuntimeSource.CODEBUDDY,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="CodeBuddy CLI",
        executables=("codebuddy", "codebuddy-cli"),
    ),
    # Qoder CLI.
    _RuntimePattern(
        source=AgentRuntimeSource.QODER,
        kind=AgentRuntimeKind.CLI_AGENT,
        display_name="Qoder CLI",
        executables=("qoder", "qoder-cli"),
    ),
    # --- GUI IDEs -----------------------------------------------------------
    _RuntimePattern(
        source=AgentRuntimeSource.CURSOR,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Cursor",
        executables=("cursor",),
        arg_needles=("cursor.app",),
        avoid_needles=("cursor helper",),
        bundle_id="com.todesktop.230313mzl4w4u92",
        helper_needles=("cursor helper", "(renderer)", "(gpu)", "crashpad_handler"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.WINDSURF,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Windsurf",
        executables=("windsurf",),
        arg_needles=("windsurf.app",),
        avoid_needles=("windsurf helper",),
        bundle_id="com.exafunction.windsurf",
        helper_needles=("windsurf helper", "(renderer)", "(gpu)", "crashpad_handler"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.VSCODE,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="VSCode",
        executables=("code",),
        arg_needles=("visual studio code.app", "vscode"),
        avoid_needles=("code helper", "code - insiders helper"),
        bundle_id="com.microsoft.VSCode",
        helper_needles=("code helper", "(renderer)", "(gpu)", "crashpad_handler"),
    ),
    _RuntimePattern(
        # The Electron launcher binary inside VSCode.app is named
        # ``Electron``. ``electron`` alone is way too generic
        # (Discord, Slack, Notion all ship Electron) so we only
        # match when the args confirm we're inside VSCode's app
        # bundle. Helper subprocesses get filtered out via
        # ``helper_needles``.
        source=AgentRuntimeSource.VSCODE,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="VSCode",
        executables=("electron",),
        arg_needles=("visual studio code.app", "/code helper"),
        avoid_needles=("code helper", "code - insiders helper"),
        bundle_id="com.microsoft.VSCode",
        helper_needles=("code helper", "(renderer)", "(gpu)", "crashpad_handler"),
        require_all=True,
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.XCODE,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Xcode",
        executables=("xcode",),
        arg_needles=("xcode.app/contents/macos/xcode",),
        bundle_id="com.apple.dt.Xcode",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.JETBRAINS,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="JetBrains IDE",
        # JetBrains ships every IDE as a separate launcher binary.
        # ``studio`` / ``android studio`` are ambiguous — they
        # appear in the disambiguation table below requiring an
        # args hint.
        executables=(
            "idea",
            "intellij",
            "pycharm",
            "webstorm",
            "goland",
            "rubymine",
            "clion",
            "rustrover",
            "phpstorm",
            "datagrip",
            "androidstudio",
        ),
        arg_needles=("jetbrains", "intellij", "pycharm", "webstorm", "goland", "rubymine"),
        bundle_id=None,
    ),
    _RuntimePattern(
        # ``studio`` alone matches Android Studio's launcher but
        # is also a very generic word. Require args to confirm
        # we're inside an Android Studio bundle.
        source=AgentRuntimeSource.JETBRAINS,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Android Studio",
        executables=("studio", "android studio"),
        arg_needles=("android studio.app", "android-studio"),
        bundle_id=None,
        require_all=True,
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.FLEET,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Fleet",
        executables=("fleet",),
        arg_needles=("jetbrains/fleet", "fleet.app"),
        bundle_id="com.jetbrains.fleet",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.ZED,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Zed",
        executables=("zed", "zed-editor"),
        arg_needles=("zed.app",),
        bundle_id="dev.zed.Zed",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.TRAE,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Trae",
        executables=("trae",),
        arg_needles=("trae.app",),
        bundle_id="com.bytedance.trae",
        helper_needles=("trae helper", "(renderer)", "(gpu)"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.SUBLIME,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Sublime Text",
        executables=("sublime_text", "subl"),
        arg_needles=("sublime text.app",),
        bundle_id="com.sublimetext.4",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.NOVA,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Nova",
        executables=("nova",),
        arg_needles=("nova.app",),
        bundle_id="com.panic.Nova",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.NEOVIM,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Neovim",
        executables=("nvim", "neovide", "vimr"),
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.GITHUB_DESKTOP,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="GitHub Desktop",
        executables=("github desktop",),
        arg_needles=("github desktop.app",),
        bundle_id="com.github.GitHubClient",
        helper_needles=("github desktop helper", "(renderer)", "(gpu)"),
    ),
    # --- Terminals ----------------------------------------------------------
    _RuntimePattern(
        source=AgentRuntimeSource.TERMINAL,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Terminal",
        executables=("terminal",),
        arg_needles=("terminal.app",),
        bundle_id="com.apple.Terminal",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.ITERM,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="iTerm",
        executables=("iterm2", "iterm"),
        arg_needles=("iterm.app", "iterm2.app"),
        bundle_id="com.googlecode.iterm2",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.GHOSTTY,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Ghostty",
        executables=("ghostty",),
        arg_needles=("ghostty.app",),
        bundle_id="com.mitchellh.ghostty",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.WEZTERM,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="WezTerm",
        executables=("wezterm-gui", "wezterm"),
        arg_needles=("wezterm.app",),
        bundle_id="com.github.wez.wezterm",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.KITTY,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="kitty",
        executables=("kitty",),
        arg_needles=("kitty.app",),
        bundle_id="net.kovidgoyal.kitty",
    ),
    _RuntimePattern(
        source=AgentRuntimeSource.WARP,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Warp",
        executables=("warp",),
        arg_needles=("warp.app",),
        bundle_id="dev.warp.Warp-Stable",
    ),
    _RuntimePattern(
        # Warp's actual binary inside Warp.app is named ``stable``.
        # Plain ``stable`` is wildly generic, so we require both
        # the exec and the args to match.
        source=AgentRuntimeSource.WARP,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Warp",
        executables=("stable",),
        arg_needles=("warp.app",),
        bundle_id="dev.warp.Warp-Stable",
        require_all=True,
    ),
    # V10 kiro-task-observer Requirement 1.1-1.6 — Kiro IDE.
    # ``Kiro.app`` ships an Electron renderer swarm; the helper
    # needles dedupe ``Kiro Helper (Renderer)`` / ``(GPU)`` /
    # ``crashpad_handler`` so the scanner emits exactly one KIRO
    # row per running app instance. The row is appended at the
    # end of the table so existing matches keep their priority.
    _RuntimePattern(
        source=AgentRuntimeSource.KIRO,
        kind=AgentRuntimeKind.GUI_IDE,
        display_name="Kiro",
        executables=("kiro",),
        arg_needles=("kiro.app",),
        bundle_id="com.kiro.kiro",
        helper_needles=("kiro helper", "(renderer)", "(gpu)", "crashpad_handler"),
    ),
)


# Default rank for ordering session rows. Phase first, priority
# second; CLI agents outrank GUI IDEs at the same phase since
# they are typically the foreground actor.
_PHASE_RANK: dict[SessionPhase, int] = {
    SessionPhase.WAITING_FOR_APPROVAL: 0,
    SessionPhase.WAITING_FOR_ANSWER: 1,
    SessionPhase.FAILED: 2,
    SessionPhase.RUNNING_TOOL: 3,
    SessionPhase.EDITING: 4,
    SessionPhase.TESTING: 5,
    SessionPhase.THINKING: 6,
    SessionPhase.RUNNING: 7,
    SessionPhase.COMPLETED: 8,
}

_PRIORITY_RANK: dict[Priority, int] = {
    Priority.P0: 0,
    Priority.P1: 1,
    Priority.P2: 2,
    Priority.P3: 3,
}


# Regex used to pull a workspace path out of VSCode-family args.
# Matches both ``--folder-uri file:///foo/bar`` and
# ``--folder-uri=file:///foo/bar`` plus a bare positional path.
_FOLDER_URI_RE = re.compile(
    r"--folder-uri[=\s]+(file://[^\s]+)",
    re.IGNORECASE,
)
_FILE_URI_RE = re.compile(r"\bfile://([^\s]+)")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class AgentRuntimeStore:
    def __init__(self, *, ttl_ms: int = 10_000) -> None:
        self._by_key: dict[str, AgentRuntimeStatus] = {}
        self._ttl_ms = max(1, ttl_ms)

    def upsert_many(self, statuses: list[AgentRuntimeStatus]) -> bool:
        changed = False
        seen_keys: set[str] = set()
        for status in statuses:
            key = self._key(status)
            seen_keys.add(key)
            if self._by_key.get(key) != status:
                self._by_key[key] = status
                changed = True
        return changed

    def expire(self, now_ms: int) -> list[AgentRuntimeStatus]:
        expired: list[AgentRuntimeStatus] = []
        cutoff = now_ms - self._ttl_ms
        for key, status in list(self._by_key.items()):
            if status.last_seen_ms < cutoff:
                expired.append(status)
                del self._by_key[key]
        return expired

    def list(self) -> list[AgentRuntimeStatus]:
        items = list(self._by_key.values())
        items.sort(
            key=lambda s: (
                _phase_rank(s.phase),
                _priority_rank(s.priority),
                -s.last_seen_ms,
                s.display_name,
            )
        )
        return items

    @staticmethod
    def _key(status: AgentRuntimeStatus) -> str:
        return status.effective_session_id


PsProvider = Callable[[], str | bytes]
Clock = Callable[[], int]


def _default_clock() -> int:
    return int(time.time() * 1000)


def _default_ps_provider() -> str:
    output = subprocess.check_output(  # noqa: S603
        ["/bin/ps", "-axo", "pid=,ppid=,tty=,comm=,args="],
        stderr=subprocess.DEVNULL,
    )
    return decode_ps_output(output)


def decode_ps_output(output: str | bytes) -> str:
    """Decode ``ps`` output without letting one bad argv byte kill scanning."""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def scan_runtime_statuses(
    *,
    ps_provider: PsProvider = _default_ps_provider,
    clock: Clock = _default_clock,
) -> list[AgentRuntimeStatus]:
    """Run one read-only runtime discovery pass.

    The resident scanner and CLI diagnostics share this seam so
    ``deskmate runtime scan`` reports the same statuses the island will
    receive on the next polling tick.
    """
    rows = parse_ps_output(decode_ps_output(ps_provider()))
    return discover_runtime_statuses(rows, now_ms=clock())


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class AgentRuntimeScanner:
    def __init__(
        self,
        store: AgentRuntimeStore,
        session_store: SessionStore,
        *,
        ps_provider: PsProvider = _default_ps_provider,
        clock: Clock = _default_clock,
        poll_interval_s: float = 2.0,
        # V10 runtime-phase-observers Requirement 4.8 — the registry
        # is constructed alongside the scanner (typically by
        # ``make_default_registry``) and threaded in through this
        # optional kwarg so existing call sites that don't yet wire
        # the framework keep working unchanged. ``None`` = no
        # observer pipeline; ``scan_once`` short-circuits.
        registry: RuntimePhaseObserverRegistry | None = None,
    ) -> None:
        self._store = store
        self._sessions = session_store
        self._ps_provider = ps_provider
        self._clock = clock
        self._poll = poll_interval_s
        self._registry = registry
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="agent-runtime-scanner")

    async def stop(self) -> None:
        self._stopping = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    async def scan_once(self) -> bool:
        try:
            now_ms = self._clock()
            statuses = scan_runtime_statuses(
                ps_provider=self._ps_provider,
                clock=lambda: now_ms,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("agent_runtime.ps_failed", error=str(exc))
            return False
        changed = self._store.upsert_many(statuses)
        expired = self._store.expire(now_ms)
        if statuses:
            for status in statuses:
                self._upsert_session(status)
        for status in expired:
            self._remove_session(status)
        # V10 runtime-phase-observers Requirement 4.3 — drive the
        # observer registry off the same ``now_ms`` we already pass
        # to ``AgentRuntimeStore.expire`` so phase derivations are
        # tick-aligned with discovery. Guarded so call sites that
        # opt out of the framework (no registry passed) keep their
        # old fast path.
        if self._registry is not None:
            self._registry.notify(statuses, now_ms)
        return changed or bool(expired)

    async def _run(self) -> None:
        while not self._stopping:
            await self.scan_once()
            await asyncio.sleep(self._poll)

    def _upsert_session(self, status: AgentRuntimeStatus) -> None:
        sid = status.effective_session_id
        existing = self._sessions.get(sid)
        if existing is not None and _is_hook_session(existing):
            return
        # V10 runtime-phase-observers Requirement 2.3 — when the
        # detected workspace root has a non-empty basename, fold it
        # into the session title so two Cursor windows on different
        # repos don't collide as identical "Cursor" rows. We
        # ``normpath`` first so a trailing slash doesn't strip
        # ``basename`` to the empty string.
        title = status.display_name or status.source.value
        if status.workspace:
            leaf = os.path.basename(os.path.normpath(status.workspace))
            if leaf:
                # Requirement 2.3 — exact format pinned by the test
                # suite ("<source label> · <basename(workspace)>").
                title = f"{status.display_name or status.source.value} · {leaf}"
            # Requirement 2.5 — empty basename (e.g. workspace == "/")
            # falls through to the bare ``display_name`` set above.

        # R11: Track first observation time for codex processes and
        # set phase_source = "unobserved" after 30s without hook/app-server events.
        extras: dict[str, Any] = {
            "runtime_source": status.source.value,
            "runtime_kind": status.kind.value,
            "command": status.command,
        }
        for key in (
            "terminal_app",
            "terminal_tty",
            "terminal_pid",
            "terminal_source",
            "tty",
        ):
            value = status.raw.get(key)
            if value is not None and str(value).strip():
                extras[key] = str(value)
        phase_source: str | None = existing.phase_source if existing else None

        if status.source == AgentRuntimeSource.CODEX:
            if existing is None:
                # First observation — record timestamp
                extras["first_observed_ms"] = str(status.last_seen_ms)
            else:
                # Carry forward existing extras
                prev_extras = existing.extras or {}
                first_observed = prev_extras.get("first_observed_ms")
                if first_observed:
                    extras["first_observed_ms"] = first_observed
                # Check if 30s have passed without hook/app-server events
                # (only transition to unobserved if phase_source is still None)
                if (
                    phase_source is None
                    and first_observed
                    and status.last_seen_ms - int(first_observed) >= 30_000
                ):
                    phase_source = "unobserved"

        self._sessions.upsert(
            SessionInfo(
                session_id=sid,
                title=title,
                summary=_summary_for(status),
                state=SessionState.ACTIVE,
                priority=status.priority,
                created_at_ms=existing.created_at_ms if existing else status.last_seen_ms,
                updated_at_ms=status.last_seen_ms,
                phase=status.phase,
                phase_source=phase_source,
                cwd=status.cwd,
                source=status.source.value,
                kind=status.kind.value,
                process_id=status.process_id,
                extras=extras,
            )
        )

    def _remove_session(self, status: AgentRuntimeStatus) -> None:
        sid = status.effective_session_id
        existing = self._sessions.get(sid)
        if existing is not None and not _is_hook_session(existing):
            # R11.7: When a codex process exits while phase_source is
            # "unobserved", clear phase_source so subscribers (e.g.
            # IslandOverlay) observe the cleared state before removal.
            if existing.phase_source == "unobserved":
                self._sessions.upsert(
                    existing.model_copy(update={"phase_source": None})
                )
            self._sessions.remove(sid)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_ps_output(text: str) -> list[ProcessRow]:
    rows: list[ProcessRow] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=4)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if len(parts) >= 5 and _looks_like_tty(parts[2]):
            tty = parts[2]
            comm = parts[3]
            args = parts[4]
        else:
            # Backward-compatible parser for older tests/providers
            # using ``pid ppid comm args`` with no tty column.
            tty = ""
            comm = parts[2]
            args = " ".join(parts[3:]) if len(parts) > 3 else comm
        rows.append(ProcessRow(pid=pid, ppid=ppid, comm=comm, args=args, tty=tty))
    return rows


def _looks_like_tty(value: str) -> bool:
    if value in {"?", "??", "console"}:
        return True
    return value.startswith(("tty", "ttys", "pts/"))


def discover_runtime_statuses(
    rows: Sequence[ProcessRow], *, now_ms: int
) -> list[AgentRuntimeStatus]:
    """Classify ``rows`` and return one status per logical runtime.

    The same IDE often shows up as 4-8 helper processes (Cursor /
    VSCode renderers, GPU subprocesses, crashpad handlers). We
    dedupe by ``(source, top-level pid)``: the first match wins
    and helper-shaped descendants get folded under it. CLI agents
    are deduped by ``(source, pid)`` directly because they don't
    fork renderers.
    """

    terminal_by_pid = _terminal_hosts_by_pid(rows)
    classified: list[tuple[ProcessRow, _RuntimePattern, AgentRuntimeStatus]] = []
    for row in rows:
        match = _classify(row)
        if match is None:
            continue
        pattern = match
        status = _build_status(
            row,
            pattern,
            now_ms=now_ms,
            terminal_host=terminal_by_pid.get(row.ppid),
        )
        classified.append((row, pattern, status))

    return _dedupe_renderers(classified)


def _classify(row: ProcessRow) -> _RuntimePattern | None:
    """Return the first ``_RuntimePattern`` that matches ``row``,
    or ``None`` if nothing matches. See ``_RuntimePattern`` for the
    matching semantics; this function is the actual implementation.
    """

    executable = Path(row.comm).name.lower()
    args_lower = row.args.lower()

    for pattern in _RUNTIME_PATTERNS:
        # Helper subprocesses are never the primary match — let
        # the parent application win and dedupe will collapse the
        # rest.
        if pattern.helper_needles and any(
            needle.lower() in args_lower for needle in pattern.helper_needles
        ):
            continue
        # avoid_needles is a hard reject, regardless of require_all.
        if pattern.avoid_needles and any(
            needle.lower() in args_lower for needle in pattern.avoid_needles
        ):
            continue

        exec_hit = bool(pattern.executables) and executable in pattern.executables
        args_hit = bool(pattern.arg_needles) and any(
            needle.lower() in args_lower for needle in pattern.arg_needles
        )

        if pattern.require_all:
            # Interpreter rules: need both halves to agree.
            if exec_hit and args_hit:
                return pattern
            continue

        # Default: either signal is sufficient. A pattern with both
        # fields set accepts a process that matches either of them,
        # which is what lets the dedicated ``cursor`` binary OR the
        # ``Cursor.app`` args path both classify as Cursor.
        if exec_hit or args_hit:
            return pattern
    return None


def _build_status(
    row: ProcessRow,
    pattern: _RuntimePattern,
    *,
    now_ms: int,
    terminal_host: AgentRuntimeStatus | None = None,
) -> AgentRuntimeStatus:
    cwd_hint = (
        _extract_workspace_hint(row.args)
        if pattern.kind is AgentRuntimeKind.GUI_IDE
        else None
    )
    # V10 runtime-phase-observers Requirement 2.2 — derive the
    # workspace root from whatever ``cwd`` we managed to extract so
    # downstream consumers (session title formatter, observers like
    # :class:`AiderTranscriptObserver`) share a single derivation
    # path. ``detect_workspace_root`` returns ``None`` for missing
    # ``cwd`` and falls back to the original path when no marker is
    # found, so the call is safe regardless of GUI/CLI kind.
    workspace_hint = detect_workspace_root(cwd_hint)
    raw: dict[str, Any] = {"comm": row.comm}
    if row.tty:
        raw["tty"] = row.tty
    if terminal_host is not None and pattern.kind is AgentRuntimeKind.CLI_AGENT:
        raw["terminal_app"] = terminal_host.display_name or terminal_host.source.value
        if row.tty:
            raw["terminal_tty"] = row.tty
        raw["terminal_pid"] = terminal_host.process_id
        raw["terminal_source"] = terminal_host.source.value

    return AgentRuntimeStatus(
        source=pattern.source,
        kind=pattern.kind,
        process_id=row.pid,
        parent_pid=row.ppid,
        display_name=pattern.display_name,
        command=row.args,
        bundle_id=pattern.bundle_id,
        cwd=cwd_hint,
        workspace=workspace_hint,
        phase=SessionPhase.RUNNING,
        priority=_priority_for(pattern.kind),
        last_seen_ms=now_ms,
        raw=raw,
    )


_TERMINAL_SOURCES = frozenset({
    AgentRuntimeSource.TERMINAL,
    AgentRuntimeSource.ITERM,
    AgentRuntimeSource.GHOSTTY,
    AgentRuntimeSource.WEZTERM,
    AgentRuntimeSource.KITTY,
    AgentRuntimeSource.WARP,
})


def _terminal_hosts_by_pid(rows: Sequence[ProcessRow]) -> dict[int, AgentRuntimeStatus]:
    hosts: dict[int, AgentRuntimeStatus] = {}
    for row in rows:
        pattern = _classify(row)
        if pattern is None or pattern.source not in _TERMINAL_SOURCES:
            continue
        hosts[row.pid] = _build_status(row, pattern, now_ms=0)
    return hosts


def _priority_for(kind: AgentRuntimeKind) -> Priority:
    # CLI agents are the active driver, GUI IDEs are background
    # context. Mirrors the pre-rewrite values so existing tests
    # locking the priority continue to pass.
    if kind is AgentRuntimeKind.CLI_AGENT:
        return Priority.P1
    return Priority.P3


def _dedupe_renderers(
    classified: Iterable[tuple[ProcessRow, _RuntimePattern, AgentRuntimeStatus]],
) -> list[AgentRuntimeStatus]:
    """Collapse Electron renderer / helper subprocess swarms.

    Algorithm:

    1. Group classified rows by ``(source, root_pid)`` where
       ``root_pid`` is the topmost ancestor pid we've seen in the
       same group. Helper / renderer rows that managed to slip
       past the helper_needles filter are now folded into the
       earliest-pid group with the same source.
    2. The lowest-pid row in each group survives. We pick the
       lowest pid because Electron's main process is forked
       before its helpers, so it carries the smaller pid.

    This keeps GUI IDEs at one row per app while still letting
    multiple distinct CLI invocations of the same agent each
    produce their own row (their parent pids diverge).
    """

    by_group: dict[tuple[AgentRuntimeSource, int], list[
        tuple[ProcessRow, _RuntimePattern, AgentRuntimeStatus]
    ]] = {}
    for row, pattern, status in classified:
        if pattern.kind is AgentRuntimeKind.GUI_IDE:
            # GUI IDEs collapse onto the source — all renderers
            # must merge into one row regardless of their pid.
            key = (pattern.source, 0)
        else:
            # CLI agents keep one row per (source, pid). Two
            # ``codex`` instances in two terminals are two
            # distinct sessions.
            key = (pattern.source, row.pid)
        by_group.setdefault(key, []).append((row, pattern, status))

    out: list[AgentRuntimeStatus] = []
    for group in by_group.values():
        # Lowest pid wins so we keep the Electron main process
        # rather than a helper that briefly sat closer to the
        # top of the table.
        group.sort(key=lambda entry: entry[0].pid)
        out.append(group[0][2])
    return out


def _extract_workspace_hint(args: str) -> str | None:
    """Best-effort workspace path extraction from VSCode-family args.

    Looks for either:

    * ``--folder-uri file:///abs/path`` (Code, Cursor, Windsurf
      all emit this when launched from the dock with a
      remembered workspace).
    * Any bare ``file://`` URI in the args.

    Returns ``None`` when nothing recognisable is present —
    callers should treat this as best-effort and not assume
    every GUI row will have a cwd. URL-decoding handles the
    common case of spaces in folder names being percent-escaped.
    """

    match = _FOLDER_URI_RE.search(args) or _FILE_URI_RE.search(args)
    if match is None:
        return None
    raw = match.group(1) if match.lastindex == 1 else match.group(0)
    parsed = urlparse(raw if raw.startswith("file://") else f"file://{raw}")
    path = unquote(parsed.path)
    return path or None


def _summary_for(status: AgentRuntimeStatus) -> str:
    if status.kind is AgentRuntimeKind.CLI_AGENT:
        return "Detected running local agent process."
    return "Detected running IDE process."


def _is_hook_session(session: SessionInfo) -> bool:
    if getattr(session, "kind", None) == AgentRuntimeKind.HOOK_SESSION.value:
        return True
    extras = session.extras or {}
    return "hook_source" in extras


# ---------------------------------------------------------------------------
# Default registry factory (V10 Requirement 4.8)
# ---------------------------------------------------------------------------


def make_default_registry(
    reducer: AgentEventReducer,
    session_store: SessionStore,
    *,
    fs: FilesystemAdapter | None = None,
) -> RuntimePhaseObserverRegistry:
    """Build the registry the application is supposed to wire into
    :class:`AgentRuntimeScanner`. Lives here (next to the scanner)
    rather than in :mod:`app` so authority over the observer pipeline
    stays with the runtime layer per Requirement 4.8 — ``App._build_app``
    only needs to call this factory.

    Imports of the observer module are deferred so the runtime layer
    keeps its current import surface when the framework is not in
    use (e.g. embedded test harnesses constructing a scanner without
    a reducer).
    """
    # Local imports — avoid a top-level cycle with ``runtime_observers``
    # (which imports :class:`AgentRuntimeStatus` from this module).
    from .runtime_observers import (  # noqa: PLC0415 — V10 deferred import
        AiderTranscriptObserver,
        DefaultFilesystemAdapter,
        KiroTaskObserver,
        RuntimePhaseObserverRegistry,
    )
    # V10 kiro-task-observer Requirement 15.1 — KiroTaskObserver
    # appended after AiderTranscriptObserver. Both observers share
    # the same ``_fs`` instance per Requirement 15.2 so test
    # adapters wire through to both at once.
    _fs: FilesystemAdapter = fs or DefaultFilesystemAdapter()
    observers = [
        AiderTranscriptObserver(fs=_fs),
        KiroTaskObserver(fs=_fs),
    ]
    return RuntimePhaseObserverRegistry(
        observers=observers,
        reducer=reducer,
        session_view=session_store.get,
    )


def _phase_rank(phase: SessionPhase) -> int:
    return _PHASE_RANK.get(phase, 9)


def _priority_rank(priority: Priority) -> int:
    return _PRIORITY_RANK.get(priority, 9)


__all__ = [
    "AgentRuntimeKind",
    "AgentRuntimeScanner",
    "AgentRuntimeSource",
    "AgentRuntimeStatus",
    "AgentRuntimeStore",
    "ProcessRow",
    "decode_ps_output",
    "detect_workspace_root",
    "discover_runtime_statuses",
    "make_default_registry",
    "parse_ps_output",
    "scan_runtime_statuses",
]
