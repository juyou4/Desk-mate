"""Route typed :class:`InteractionAction` values into :class:`SessionStore`
mutations (V10 L1-F).

The router keeps protocol concerns (validated pydantic actions) separate
from runtime state (in-memory store). It returns a :class:`RouterResult`
so the caller can log / emit follow-up intents without the router owning
a bridge handle.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ..logging_setup import get_logger
from ..protocol.actions import ActionTarget, InteractionAction, InteractionKind
from ..protocol.state import Priority
from .info import SessionPhase, SessionState
from .store import SessionStore

_LOG = get_logger("deskmate_agent.sessions.router")

_ALLOWED_JUMP_SCHEMES = frozenset({"codex", "file", "vscode", "cursor", "windsurf"})


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
    try:
        result = subprocess.run(  # noqa: S603,S607
            ["osascript", "-e", script],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


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


@dataclass
class RouterResult:
    """Outcome of a single :meth:`SessionInteractionRouter.handle` call."""

    handled: bool
    effect: str = ""
    session_id: str | None = None


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
    ) -> None:
        self._store = store
        self._clock = clock
        self._opener = opener
        self._activator = activator
        self._workspace_opener = workspace_opener
        self._handlers: dict[
            InteractionKind, Callable[[InteractionAction], RouterResult]
        ] = {
            InteractionKind.SESSION_JUMP: self._handle_jump,
            InteractionKind.QUESTION_ANSWER: self._handle_question_answer,
        }

    def handle(self, action: InteractionAction) -> RouterResult:
        if action.target is not ActionTarget.SESSION:
            return RouterResult(handled=False, effect="wrong_target")
        handler = self._handlers.get(action.kind)
        if handler is None:
            return RouterResult(handled=False, effect="unknown_kind")
        result = handler(action)
        _LOG.info(
            "sessions.router_handled",
            kind=action.kind.value,
            handled=result.handled,
            effect=result.effect,
            session_id=result.session_id,
        )
        return result

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_jump(self, action: InteractionAction) -> RouterResult:
        sid = action.payload.get("session_id")
        if not isinstance(sid, str) or not sid:
            return RouterResult(handled=False, effect="missing_session_id")
        existing = self._store.get(sid)
        if existing is None:
            return RouterResult(
                handled=True, effect="session.jump.unknown_id", session_id=sid
            )
        jump_target = self._url_jump_target(existing)
        if jump_target is not None and self._opener(jump_target):
            self._store.touch(sid, self._clock(), new_state=SessionState.ACTIVE)
            return RouterResult(
                handled=True, effect="session.jump.opened", session_id=sid
            )
        workspace_target = self._workspace_target(existing)
        if workspace_target is not None:
            path, apps = workspace_target
            for app_name in apps:
                if self._workspace_opener(app_name, path):
                    self._store.touch(sid, self._clock(), new_state=SessionState.ACTIVE)
                    return RouterResult(
                        handled=True,
                    effect="session.jump.workspace_opened",
                    session_id=sid,
                )
        local_target = self._local_path_target(existing)
        if local_target is not None and self._opener(local_target):
            self._store.touch(sid, self._clock(), new_state=SessionState.ACTIVE)
            return RouterResult(
                handled=True, effect="session.jump.opened", session_id=sid
            )
        activation_target = self._activation_target(existing)
        if activation_target is not None and self._activator(activation_target):
            self._store.touch(sid, self._clock(), new_state=SessionState.ACTIVE)
            return RouterResult(
                handled=True, effect="session.jump.activated", session_id=sid
            )
        self._store.touch(sid, self._clock(), new_state=SessionState.ACTIVE)
        return RouterResult(
            handled=True, effect="session.jump.accepted", session_id=sid
        )

    def _handle_question_answer(self, action: InteractionAction) -> RouterResult:
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
        existing = self._store.get(sid)
        if existing is None:
            return RouterResult(
                handled=True,
                effect="session.question_answer.unknown_id",
                session_id=sid,
            )
        now_ms = self._clock()
        extras = {**(existing.extras or {})}
        extras["last_answer"] = answer.strip()
        extras["last_answer_at_ms"] = str(now_ms)
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
    def _activation_target(info: object) -> str | None:
        source = getattr(info, "source", None)
        kind = getattr(info, "kind", None)
        candidates = _activation_candidates(
            str(source) if source is not None else "",
            str(kind) if kind is not None else "",
        )
        return candidates[0] if candidates else None


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
    if normalized == "codex":
        return ["Codex", "Terminal", "iTerm", "Ghostty", "Warp"]
    if normalized in {"claude_code", "terminal", "opencode"} or kind == "cli_agent":
        return ["Terminal", "iTerm", "Ghostty", "Warp"]
    return []


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
    if normalized in {"terminal", "opencode"} or kind == "cli_agent":
        return ["Terminal", "iTerm", "Ghostty", "Warp"]
    return []


__all__ = [
    "RouterResult",
    "SessionInteractionRouter",
    "_activation_candidates",
    "_workspace_candidates",
]
