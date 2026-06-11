"""Route typed :class:`InteractionAction` values into :class:`SessionStore`
mutations (V10 L1-F).

The router keeps protocol concerns (validated pydantic actions) separate
from runtime state (in-memory store). It returns a :class:`RouterResult`
so the caller can log / emit follow-up intents without the router owning
a bridge handle.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..logging_setup import get_logger
from ..protocol.actions import ActionTarget, InteractionAction, InteractionKind
from ..protocol.intents import CompanionIntent, IntentKind
from ..protocol.state import Priority
from .info import SessionInfo, SessionPhase, SessionState
from .store import SessionStore

_LOG = get_logger("deskmate_agent.sessions.router")

_ALLOWED_JUMP_SCHEMES = frozenset({
    "codex",
    "file",
    "vscode",
    "cursor",
    "windsurf",
    "vscode-insiders",
})


def _default_clock() -> int:
    return int(time.time() * 1000)


def _default_opener(target: str) -> bool:
    try:
        subprocess.Popen(["open", target])  # noqa: S603,S607
        return True
    except OSError:
        return False


def _default_activator(app_name: str) -> bool:
    script = f'tell application "{app_name}" to activate'
    return _run_osascript(script) is not None


def _run_osascript(script: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _default_workspace_opener(app_name: str, path: str) -> bool:
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["open", "-a", app_name, path],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass(frozen=True)
class TerminalJumpTarget:
    terminal_app: str = ""
    terminal_session_id: str = ""
    terminal_tty: str = ""
    pane_title: str = ""
    tmux_target: str = ""
    tmux_socket_path: str = ""
    cmux_workspace_id: str = ""
    cmux_surface_id: str = ""
    cwd: str = ""

    @property
    def has_precise_locator(self) -> bool:
        return any(
            (
                self.terminal_session_id,
                self.terminal_tty,
                self.pane_title,
                self.tmux_target,
                self.cmux_workspace_id,
                self.cmux_surface_id,
            )
        )


TerminalRouter = Callable[[TerminalJumpTarget], bool]


def _default_terminal_router(target: TerminalJumpTarget) -> bool:
    routed = False
    if _focus_cmux_surface(target):
        routed = True
    if target.tmux_target and _select_tmux_pane(target):
        routed = True
    app_name = _terminal_app_name(target.terminal_app)
    if app_name == "Ghostty" and _focus_ghostty_terminal(target):
        routed = True
    if app_name == "iTerm" and _focus_iterm_session(target):
        routed = True
    if app_name:
        return _default_activator(app_name) or routed
    return routed


def _focus_cmux_surface(target: TerminalJumpTarget) -> bool:
    surface_id = target.cmux_surface_id.strip()
    if not surface_id and _terminal_app_name(target.terminal_app).lower() == "cmux":
        surface_id = target.terminal_session_id.strip()
    if not surface_id:
        return False
    socket_path = _resolve_cmux_socket_path()
    if socket_path is None:
        return False
    request = {
        "jsonrpc": "2.0",
        "method": "surface.focus",
        "params": {"surface_id": surface_id},
        "id": 1,
    }
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(socket_path)
            wire = json.dumps(request, separators=(",", ":")) + "\n"
            sock.sendall(wire.encode("utf-8"))
            return True
    except OSError:
        return False


def _resolve_cmux_socket_path() -> str | None:
    redirected = Path("/tmp/cmux-last-socket-path")
    try:
        if redirected.exists():
            candidate = Path(redirected.read_text(encoding="utf-8").strip())
            if candidate.exists():
                return str(candidate)
    except OSError:
        pass

    candidates = (
        Path.home() / "Library" / "Application Support" / "cmux" / "cmux.sock",
        Path("/tmp/cmux.sock"),
    )
    for candidate in candidates:
        try:
            if candidate.exists():
                return str(candidate)
        except OSError:
            continue
    return None


def _focus_iterm_session(target: TerminalJumpTarget) -> bool:
    session_id = _escape_applescript(target.terminal_session_id.strip())
    tty = _escape_applescript(target.terminal_tty.strip())
    if not session_id and not tty:
        return False
    script = f"""
tell application "iTerm"
    if not (it is running) then return ""
    activate
    repeat with aWindow in windows
        repeat with aTab in tabs of aWindow
            repeat with aSession in sessions of aTab
                set matched to false
                if "{session_id}" is not "" and (id of aSession as text) is "{session_id}" then
                    set matched to true
                end if
                if not matched and "{tty}" is not "" and (tty of aSession as text) is "{tty}" then
                    set matched to true
                end if
                if matched then
                    select aWindow
                    tell aWindow to select aTab
                    select aSession
                    return "matched"
                end if
            end repeat
        end repeat
    end repeat
end tell
return ""
"""
    return _run_osascript(script) == "matched"


def _focus_ghostty_terminal(target: TerminalJumpTarget) -> bool:
    session_id = _escape_applescript(target.terminal_session_id.strip())
    cwd = _escape_applescript(target.cwd.strip())
    pane_title = _escape_applescript(target.pane_title.strip())
    if not session_id and not cwd and not pane_title:
        return False
    script = f"""
tell application "Ghostty"
    if not (it is running) then return ""
    activate

    set targetWindow to missing value
    set targetTab to missing value
    set targetTerminal to missing value

    repeat with aWindow in windows
        repeat with aTab in tabs of aWindow
            repeat with aTerminal in terminals of aTab
                if "{session_id}" is not "" and (id of aTerminal as text) is "{session_id}" then
                    set targetWindow to aWindow
                    set targetTab to aTab
                    set targetTerminal to aTerminal
                    exit repeat
                end if
            end repeat

            if targetTerminal is not missing value then
                exit repeat
            end if
        end repeat

        if targetTerminal is not missing value then
            exit repeat
        end if
    end repeat

    if targetTerminal is missing value and "{cwd}" is not "" then
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aTerminal in terminals of aTab
                    if (working directory of aTerminal as text) is "{cwd}" then
                        set targetWindow to aWindow
                        set targetTab to aTab
                        set targetTerminal to aTerminal
                        exit repeat
                    end if
                end repeat

                if targetTerminal is not missing value then
                    exit repeat
                end if
            end repeat

            if targetTerminal is not missing value then
                exit repeat
            end if
        end repeat
    end if

    if targetTerminal is missing value and "{pane_title}" is not "" then
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aTerminal in terminals of aTab
                    if (name of aTerminal as text) contains "{pane_title}" then
                        set targetWindow to aWindow
                        set targetTab to aTab
                        set targetTerminal to aTerminal
                        exit repeat
                    end if
                end repeat

                if targetTerminal is not missing value then
                    exit repeat
                end if
            end repeat

            if targetTerminal is not missing value then
                exit repeat
            end if
        end repeat
    end if

    if targetTerminal is missing value then return ""

    if "{session_id}" is "" then
        if targetWindow is not missing value then
            activate window targetWindow
            delay 0.04
        end if

        if targetTab is not missing value then
            select tab targetTab
            delay 0.04
        end if

        focus targetTerminal
        delay 0.08
        return "matched"
    end if

    repeat 3 times
        if targetWindow is not missing value then
            activate window targetWindow
            delay 0.04
        end if

        if targetTab is not missing value then
            select tab targetTab
            delay 0.04
        end if

        focus targetTerminal
        delay 0.08

        try
            if (id of focused terminal of selected tab of front window as text) is "{session_id}" then
                return "matched"
            end if
        end try
    end repeat
end tell
return ""
"""
    return _run_osascript(script) == "matched"


def _escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _select_tmux_pane(target: TerminalJumpTarget) -> bool:
    tmux = _resolve_tmux_path()
    if tmux is None:
        return False
    args = [tmux]
    if target.tmux_socket_path:
        args.extend(["-S", target.tmux_socket_path])
    args.extend(["select-pane", "-t", target.tmux_target])
    try:
        result = subprocess.run(  # noqa: S603
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _resolve_tmux_path() -> str | None:
    for candidate in (
        "/opt/homebrew/bin/tmux",
        "/usr/local/bin/tmux",
        "/usr/bin/tmux",
        "/bin/tmux",
    ):
        path = Path(candidate)
        if path.exists():
            return str(path)
    return None


def _terminal_app_name(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    mapping = {
        "terminal": "Terminal",
        "terminal.app": "Terminal",
        "iterm": "iTerm",
        "iterm2": "iTerm",
        "iterm.app": "iTerm",
        "iterm2.app": "iTerm",
        "ghostty": "Ghostty",
        "ghostty.app": "Ghostty",
        "warp": "Warp",
        "warp.app": "Warp",
        "wezterm": "WezTerm",
        "wezterm.app": "WezTerm",
        "cmux": "cmux",
    }
    return mapping.get(normalized, value.strip())


@dataclass
class RouterResult:
    """Outcome of a single :meth:`SessionInteractionRouter.handle` call."""

    handled: bool
    effect: str = ""
    session_id: str | None = None
    details: dict[str, str] | None = None


class SessionInteractionRouter:
    """Dispatch ``InteractionAction`` values whose ``target`` is SESSION."""

    def __init__(
        self,
        store: SessionStore,
        *,
        clock: Callable[[], int] = _default_clock,
        opener: Callable[[str], bool] = _default_opener,
        activator: Callable[[str], bool] = _default_activator,
        workspace_opener: Callable[[str, str], bool] = _default_workspace_opener,
        terminal_router: TerminalRouter = _default_terminal_router,
        intent_sink: Callable[[CompanionIntent], Awaitable[None]] | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._opener = opener
        self._activator = activator
        self._workspace_opener = workspace_opener
        self._terminal_router = terminal_router
        self._intent_sink = intent_sink
        self._handlers: dict[
            InteractionKind, Callable[[InteractionAction], Awaitable[RouterResult]]
        ] = {
            InteractionKind.SESSION_JUMP: self._handle_jump,
            InteractionKind.QUESTION_ANSWER: self._handle_question_answer,
        }

    async def handle(self, action: InteractionAction) -> RouterResult:
        if action.target is not ActionTarget.SESSION:
            return RouterResult(handled=False, effect="wrong_target")
        handler = self._handlers.get(action.kind)
        if handler is None:
            return RouterResult(handled=False, effect="unknown_kind")
        result = await handler(action)
        _LOG.info(
            "sessions.router_handled",
            kind=action.kind.value,
            handled=result.handled,
            effect=result.effect,
            session_id=result.session_id,
            details=result.details or {},
        )
        return result

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_jump(self, action: InteractionAction) -> RouterResult:
        sid = action.payload.get("session_id")
        if not isinstance(sid, str) or not sid:
            return RouterResult(handled=False, effect="missing_session_id")
        existing = self._store.get(sid)
        if existing is None:
            return RouterResult(
                handled=True,
                effect="session.jump.unknown_id",
                session_id=sid,
                details={"route": "unknown", "detail": "Session no longer exists."},
            )
        attempts: list[str] = []
        jump_target = self._url_jump_target(existing)
        if jump_target is not None:
            attempts.append("url:available")
            if self._opener(jump_target):
                attempts.append("url:opened")
                return self._finish_jump(
                    existing,
                    effect="session.jump.opened",
                    route="url",
                    detail="Opened allowlisted jump URL.",
                    attempts=attempts,
                )
            attempts.append("url:open_failed")
        elif _has_text(getattr(existing, "jump_url", None)):
            attempts.append("url:rejected_or_missing")
        else:
            attempts.append("url:missing")
        terminal_target = self._terminal_target(existing)
        if terminal_target is not None and terminal_target.has_precise_locator:
            attempts.append(
                "terminal:available:"
                + _jump_detail_token(_describe_terminal_target(terminal_target))
            )
            if self._terminal_router(terminal_target):
                attempts.append("terminal:routed")
                return self._finish_jump(
                    existing,
                    effect="session.jump.terminal_routed",
                    route="terminal",
                    detail=f"Routed to {_terminal_app_name(terminal_target.terminal_app) or 'terminal'} using "
                    f"{_terminal_locator_summary(terminal_target)}.",
                    attempts=attempts,
                )
            attempts.append("terminal:route_failed")
        elif terminal_target is not None:
            attempts.append("terminal:missing_precise_locator")
        else:
            attempts.append("terminal:missing")
        workspace_target = self._workspace_target(existing)
        if workspace_target is not None:
            path, apps = workspace_target
            for app_name in apps:
                attempts.append(f"workspace:{_jump_detail_token(app_name)}")
                if self._workspace_opener(app_name, path):
                    attempts.append(f"workspace:{_jump_detail_token(app_name)}:opened")
                    return self._finish_jump(
                        existing,
                        effect="session.jump.workspace_opened",
                        route="workspace",
                        detail=f"Opened workspace in {app_name}.",
                        attempts=attempts,
                    )
                attempts.append(f"workspace:{_jump_detail_token(app_name)}:failed")
        else:
            attempts.append("workspace:missing")
        local_target = self._local_path_target(existing)
        if local_target is not None and self._opener(local_target):
            attempts.append("local_path:opened")
            return self._finish_jump(
                existing,
                effect="session.jump.opened",
                route="local_path",
                detail="Opened local working directory.",
                attempts=attempts,
            )
        if local_target is not None:
            attempts.append("local_path:open_failed")
        else:
            attempts.append("local_path:missing")
        for activation_target in self._activation_targets(existing):
            attempts.append(f"activation:{_jump_detail_token(activation_target)}")
            if self._activator(activation_target):
                attempts.append(
                    f"activation:{_jump_detail_token(activation_target)}:activated"
                )
                return self._finish_jump(
                    existing,
                    effect="session.jump.activated",
                    route="activation",
                    detail=f"Activated {activation_target}.",
                    attempts=attempts,
                )
            attempts.append(f"activation:{_jump_detail_token(activation_target)}:failed")
        return self._finish_jump(
            existing,
            effect="session.jump.accepted",
            route="none",
            detail="No exact jump target succeeded.",
            attempts=attempts,
        )

    def _finish_jump(
        self,
        info: SessionInfo,
        *,
        effect: str,
        route: str,
        detail: str,
        attempts: list[str],
    ) -> RouterResult:
        now_ms = self._clock()
        attempts_text = "; ".join(attempts)
        extras = {**(info.extras or {})}
        extras.update({
            "last_jump_effect": effect,
            "last_jump_route": route,
            "last_jump_detail": detail,
            "last_jump_attempts": attempts_text,
            "last_jump_at_ms": str(now_ms),
        })
        self._store.upsert(
            info.model_copy(
                update={
                    "state": SessionState.ACTIVE,
                    "updated_at_ms": now_ms,
                    "extras": extras,
                }
            )
        )
        return RouterResult(
            handled=True,
            effect=effect,
            session_id=info.session_id,
            details={
                "route": route,
                "detail": detail,
                "attempts": attempts_text,
            },
        )

    async def _handle_question_answer(self, action: InteractionAction) -> RouterResult:
        sid = action.payload.get("session_id")
        if not isinstance(sid, str) or not sid:
            return RouterResult(handled=False, effect="missing_session_id")
        answer = action.payload.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return RouterResult(
                handled=False,
                effect="missing_answer",
                session_id=sid,
            )
        # R2.6: reject answers exceeding 4096 characters
        if len(answer.strip()) > 4096:
            return RouterResult(
                handled=False,
                effect="answer_too_long",
                session_id=sid,
            )
        existing = self._store.get(sid)
        if existing is None:
            return RouterResult(
                handled=True,
                effect="session.question_answer.unknown_id",
                session_id=sid,
            )
        # R2.5: phase guard — only act if session is waiting for answer
        if existing.phase != SessionPhase.WAITING_FOR_ANSWER:
            return RouterResult(
                handled=True,
                effect="session.question_answer.phase_mismatch",
                session_id=sid,
            )
        now_ms = self._clock()
        extras = {**(existing.extras or {})}
        extras["last_answer"] = answer.strip()
        extras["last_answer_at_ms"] = str(now_ms)
        # R2.3: persist last_answer before phase write
        # R2.2: transition phase to RUNNING
        self._store.upsert(
            existing.model_copy(
                update={
                    "summary": f"User answered: {answer.strip()}",
                    "state": SessionState.ACTIVE,
                    "priority": Priority.P1,
                    "updated_at_ms": now_ms,
                    "phase": SessionPhase.RUNNING,
                    "extras": extras,
                }
            )
        )
        # R2.1: emit dismiss_island with last_question_surface_id
        surface_id = existing.last_question_surface_id
        if surface_id and self._intent_sink is not None:
            await self._intent_sink(CompanionIntent(
                kind=IntentKind.DISMISS_ISLAND,
                payload={"id": surface_id},
            ))
        return RouterResult(
            handled=True,
            effect="session.question_answer.accepted",
            session_id=sid,
        )

    @staticmethod
    def _url_jump_target(info: object) -> str | None:
        jump_url = getattr(info, "jump_url", None)
        if isinstance(jump_url, str) and jump_url.strip():
            target = jump_url.strip()
            parsed = urlparse(target)
            if parsed.scheme in _ALLOWED_JUMP_SCHEMES:
                if parsed.scheme == "file":
                    local = Path(parsed.path).expanduser()
                    return str(local) if local.exists() else None
                return target
        return None

    @staticmethod
    def _local_path_target(info: object) -> str | None:
        cwd = getattr(info, "cwd", None)
        if isinstance(cwd, str) and cwd.strip():
            path = Path(cwd).expanduser()
            if path.exists():
                return str(path)
        return None

    @staticmethod
    def _workspace_target(info: object) -> tuple[str, list[str]] | None:
        cwd = getattr(info, "cwd", None)
        if not isinstance(cwd, str) or not cwd.strip():
            return None
        path = Path(cwd).expanduser()
        if not path.exists():
            return None
        source = getattr(info, "source", None)
        kind = getattr(info, "kind", None)
        apps = _workspace_candidates(
            str(source) if source is not None else "",
            str(kind) if kind is not None else "",
        )
        if not apps:
            return None
        return str(path), apps

    @staticmethod
    def _activation_targets(info: object) -> list[str]:
        source = getattr(info, "source", None)
        kind = getattr(info, "kind", None)
        return _activation_candidates(
            str(source) if source is not None else "",
            str(kind) if kind is not None else "",
        )

    @staticmethod
    def _terminal_target(info: object) -> TerminalJumpTarget | None:
        extras = getattr(info, "extras", None)
        extras = extras if isinstance(extras, dict) else {}
        cwd = getattr(info, "cwd", None)
        target = TerminalJumpTarget(
            terminal_app=_extra_text(extras, "terminal_app", "terminalApp", "terminal"),
            terminal_session_id=_extra_text(
                extras,
                "terminal_session_id",
                "terminalSessionID",
                "terminal_session",
            ),
            terminal_tty=_extra_text(extras, "terminal_tty", "tty"),
            pane_title=_extra_text(extras, "pane_title", "paneTitle", "window_title"),
            tmux_target=_extra_text(extras, "tmux_target", "tmuxTarget"),
            tmux_socket_path=_extra_text(extras, "tmux_socket_path", "tmuxSocketPath"),
            cmux_workspace_id=_extra_text(extras, "cmux_workspace_id", "cmuxWorkspaceId"),
            cmux_surface_id=_extra_text(extras, "cmux_surface_id", "cmuxSurfaceId"),
            cwd=cwd if isinstance(cwd, str) else "",
        )
        if not any(target.__dict__.values()):
            return None
        return target


def _workspace_candidates(source: str, kind: str = "") -> list[str]:
    normalized = source.strip().lower().replace("-", "_")
    if normalized == "cursor":
        return ["Cursor"]
    if normalized == "windsurf":
        return ["Windsurf"]
    if normalized == "vscode":
        return ["Visual Studio Code"]
    if normalized == "xcode":
        return ["Xcode"]
    if normalized == "jetbrains":
        return ["IntelliJ IDEA", "PyCharm", "WebStorm", "GoLand"]
    if normalized == "zed":
        return ["Zed"]
    if normalized == "trae":
        return ["Trae"]
    if normalized == "sublime":
        return ["Sublime Text"]
    if normalized == "nova":
        return ["Nova"]
    if normalized == "codex":
        return ["Codex", "Terminal", "iTerm", "Ghostty", "Warp"]
    if normalized in {
        "claude_code",
        "terminal",
        "opencode",
        "aider",
        "gemini",
        "kimi",
        "qwen",
        "factory_droid",
        "codebuddy",
        "qoder",
        "neovim",
        "warp",
    } or kind == "cli_agent":
        return ["Terminal", "iTerm", "Ghostty", "Warp", "WezTerm", "kitty"]
    return []


def _extra_text(extras: dict, *keys: str) -> str:
    for key in keys:
        value = extras.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _jump_detail_token(value: str) -> str:
    token = " ".join(value.strip().split())
    return token.replace(";", ",")[:120] or "unknown"


def _describe_terminal_target(target: TerminalJumpTarget) -> str:
    app = _terminal_app_name(target.terminal_app) or "terminal"
    return f"{app} {_terminal_locator_summary(target)}"


def _terminal_locator_summary(target: TerminalJumpTarget) -> str:
    locators: list[str] = []
    if target.terminal_session_id:
        locators.append("session_id")
    if target.terminal_tty:
        locators.append("tty")
    if target.pane_title:
        locators.append("pane_title")
    if target.tmux_target:
        locators.append("tmux")
    if target.cmux_surface_id:
        locators.append("cmux_surface")
    elif target.cmux_workspace_id:
        locators.append("cmux_workspace")
    return "+".join(locators) or "fallback"


def _activation_candidates(source: str, kind: str = "") -> list[str]:
    normalized = source.strip().lower().replace("-", "_")
    if normalized == "codex":
        return ["Codex", "Terminal", "iTerm", "Ghostty"]
    if normalized == "claude_code":
        return ["Terminal", "iTerm", "Ghostty", "Warp"]
    if normalized == "cursor":
        return ["Cursor"]
    if normalized == "windsurf":
        return ["Windsurf"]
    if normalized == "vscode":
        return ["Visual Studio Code"]
    if normalized == "xcode":
        return ["Xcode"]
    if normalized == "jetbrains":
        return ["IntelliJ IDEA", "PyCharm", "WebStorm", "GoLand"]
    if normalized == "zed":
        return ["Zed"]
    if normalized == "trae":
        return ["Trae"]
    if normalized == "sublime":
        return ["Sublime Text"]
    if normalized == "nova":
        return ["Nova"]
    if normalized == "warp":
        return ["Warp", "Terminal", "iTerm", "Ghostty"]
    if normalized in {
        "terminal",
        "opencode",
        "aider",
        "gemini",
        "kimi",
        "qwen",
        "factory_droid",
        "codebuddy",
        "qoder",
        "neovim",
    } or kind == "cli_agent":
        return ["Terminal", "iTerm", "Ghostty", "Warp", "WezTerm", "kitty"]
    return []


__all__ = [
    "RouterResult",
    "SessionInteractionRouter",
    "TerminalJumpTarget",
    "_activation_candidates",
    "_workspace_candidates",
]
