"""Small, safe computer-control composer.

This is the first natural-language control layer for Deskmate. It only
handles low-risk macOS actions:

- open a known application
- focus a known application
- open a URL
- open a local file/folder path
- open a local file/folder path with a known application
- reveal a local file/folder path in Finder
- open a known System Settings pane
- open the Weather app for local weather checks
- set clipboard text after approval
- lock or sleep the Mac after approval
- adjust output volume or mute state
- take a screenshot after approval
- perform a web search

It deliberately does not execute arbitrary shell commands. Unknown input
returns ``None`` so the caller can fall back to the normal chat composer.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlparse

from ..approvals import Approval, ApprovalDecision, ApprovalStore
from ..dispatcher import ReplyComposer, StreamingReplyComposer
from ..protocol.state import Priority

Opener = Callable[[list[str]], bool]
Clock = Callable[[], int]


@dataclass(frozen=True)
class ComputerAction:
    kind: str
    target: str
    display: str
    requires_approval: bool = False
    app: str | None = None


_RESTORABLE_APPROVAL_ACTION_KINDS: frozenset[str] = frozenset(
    {
        "quit_app",
        "set_clipboard",
        "lock_screen",
        "sleep_mac",
        "screenshot",
    }
)


_APP_ALIASES: dict[str, str] = {
    "activity monitor": "Activity Monitor",
    "app store": "App Store",
    "calendar": "Calendar",
    "chrome": "Google Chrome",
    "cursor": "Cursor",
    "finder": "Finder",
    "ghostty": "Ghostty",
    "iterm": "iTerm",
    "iterm2": "iTerm",
    "mail": "Mail",
    "messages": "Messages",
    "notes": "Notes",
    "safari": "Safari",
    "settings": "System Settings",
    "system settings": "System Settings",
    "terminal": "Terminal",
    "weather": "Weather",
    "天气": "Weather",
    "天气 app": "Weather",
    "天气应用": "Weather",
    "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code",
    "windsurf": "Windsurf",
    "xcode": "Xcode",
}

_SYSTEM_SETTINGS_PANES: dict[str, str] = {
    "accessibility": "x-apple.systempreferences:com.apple.Accessibility-Settings.extension",
    "appearance": "x-apple.systempreferences:com.apple.Appearance-Settings.extension",
    "battery": "x-apple.systempreferences:com.apple.Battery-Settings.extension",
    "bluetooth": "x-apple.systempreferences:com.apple.BluetoothSettings",
    "display": "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    "displays": "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    "keyboard": "x-apple.systempreferences:com.apple.Keyboard-Settings.extension",
    "network": "x-apple.systempreferences:com.apple.Network-Settings.extension",
    "privacy": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    "privacy security": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    "security": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    "sound": "x-apple.systempreferences:com.apple.Sound-Settings.extension",
    "trackpad": "x-apple.systempreferences:com.apple.Trackpad-Settings.extension",
    "wi-fi": "x-apple.systempreferences:com.apple.Wi-Fi-Settings.extension",
    "wifi": "x-apple.systempreferences:com.apple.Wi-Fi-Settings.extension",
    "辅助功能": "x-apple.systempreferences:com.apple.Accessibility-Settings.extension",
    "电池": "x-apple.systempreferences:com.apple.Battery-Settings.extension",
    "蓝牙": "x-apple.systempreferences:com.apple.BluetoothSettings",
    "显示器": "x-apple.systempreferences:com.apple.Displays-Settings.extension",
    "键盘": "x-apple.systempreferences:com.apple.Keyboard-Settings.extension",
    "网络": "x-apple.systempreferences:com.apple.Network-Settings.extension",
    "隐私": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    "安全": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    "声音": "x-apple.systempreferences:com.apple.Sound-Settings.extension",
    "触控板": "x-apple.systempreferences:com.apple.Trackpad-Settings.extension",
    "无线": "x-apple.systempreferences:com.apple.Wi-Fi-Settings.extension",
}

_OPEN_APP_PATTERNS = (
    re.compile(r"^(?:open|launch|start)\s+(?:the\s+)?(?:app\s+)?(?P<name>.+)$", re.I),
    re.compile(r"^(?:打开|启动|开启)\s*(?P<name>.+)$", re.I),
)
_OPEN_SETTINGS_PANE_PATTERNS = (
    re.compile(
        r"^(?:open|show)\s+(?P<pane>.+?)\s+(?:settings|preferences)$",
        re.I,
    ),
    re.compile(r"^(?:打开|显示)\s*(?P<pane>.+?)\s*(?:设置|偏好设置)$", re.I),
)
_OPEN_WEATHER_PATTERNS = (
    re.compile(
        r"^(?:weather|show weather|open weather|open weather app|"
        r"what(?:'s| is) the weather)\??$",
        re.I,
    ),
    re.compile(
        r"^(?:帮我)?(?:看|查|打开)?(?:一下)?(?:天气|天气预报)(?:app|应用)?(?:怎么样)?\??$",
        re.I,
    ),
)
_FOCUS_APP_PATTERNS = (
    re.compile(r"^(?:focus|activate)\s+(?:the\s+)?(?:app\s+)?(?P<name>.+)$", re.I),
    re.compile(r"^(?:switch to|go to)\s+(?P<name>.+)$", re.I),
    re.compile(r"^(?:切换到|聚焦|激活)\s*(?P<name>.+)$", re.I),
)
_QUIT_APP_PATTERNS = (
    re.compile(r"^(?:quit|close|exit)\s+(?:the\s+)?(?:app\s+)?(?P<name>.+)$", re.I),
    re.compile(r"^(?:退出|关闭)\s*(?P<name>.+)$", re.I),
)
_OPEN_URL_PATTERNS = (
    re.compile(r"^(?:open|go to|visit)\s+(?P<target>https?://\S+)$", re.I),
    re.compile(r"^(?:打开|访问)\s*(?P<target>https?://\S+)$", re.I),
)
_OPEN_PATH_PATTERNS = (
    re.compile(r"^(?:open|show)\s+(?P<target>~?/[^?]+)$", re.I),
    re.compile(r"^(?:打开|显示)\s*(?P<target>~?/[^？]+)$", re.I),
)
_OPEN_PATH_IN_APP_PATTERNS = (
    re.compile(
        r"^(?:open|show)\s+(?P<target>~?/[^?]+?)\s+(?:in|with)\s+(?P<name>.+)$",
        re.I,
    ),
    re.compile(r"^用\s*(?P<name>.+?)\s*打开\s*(?P<target>~?/[^？]+)$", re.I),
)
_REVEAL_PATH_PATTERNS = (
    re.compile(
        r"^(?:reveal|show)\s+(?P<target>~?/[^?]+?)\s+(?:in\s+)?finder$",
        re.I,
    ),
    re.compile(r"^(?:reveal in finder|show in finder)\s+(?P<target>~?/[^?]+)$", re.I),
    re.compile(r"^在\s*finder\s*(?:中)?显示\s*(?P<target>~?/[^？]+)$", re.I),
)
_SEARCH_PATTERNS = (
    re.compile(r"^(?:search|google|look up)\s+(?:for\s+)?(?P<query>.+)$", re.I),
    re.compile(r"^(?:搜索|查一下|查找)\s*(?P<query>.+)$", re.I),
)
_SET_CLIPBOARD_PATTERNS = (
    re.compile(
        r"^(?:copy|set clipboard to|put on clipboard)\s+(?P<text>.+)$",
        re.I,
    ),
    re.compile(r"^(?:复制|复制到剪贴板|设置剪贴板为)\s*(?P<text>.+)$", re.I),
)
_LOCK_SCREEN_PATTERNS = (
    re.compile(r"^(?:lock|lock screen|lock my mac|lock this mac)$", re.I),
    re.compile(r"^(?:锁屏|锁定屏幕|锁定电脑|锁定这台电脑)$", re.I),
)
_SLEEP_MAC_PATTERNS = (
    re.compile(r"^(?:sleep|sleep mac|put mac to sleep|put this mac to sleep)$", re.I),
    re.compile(r"^(?:睡眠|让电脑睡眠|让 mac 睡眠|让mac睡眠)$", re.I),
)
_MUTE_PATTERNS = (
    re.compile(r"^(?:mute|mute volume|mute sound)$", re.I),
    re.compile(r"^(?:静音|关闭声音)$", re.I),
)
_UNMUTE_PATTERNS = (
    re.compile(r"^(?:unmute|unmute volume|unmute sound)$", re.I),
    re.compile(r"^(?:取消静音|打开声音)$", re.I),
)
_SET_VOLUME_PATTERNS = (
    re.compile(r"^(?:set volume to|volume to)\s*(?P<level>\d{1,3})%?$", re.I),
    re.compile(r"^(?:设置音量为|音量设为|音量到)\s*(?P<level>\d{1,3})%?$", re.I),
)
_SCREENSHOT_PATTERNS = (
    re.compile(r"^(?:take screenshot|take a screenshot|screenshot)$", re.I),
    re.compile(r"^(?:截图|截屏|屏幕截图)$", re.I),
)


def computer_control_composer(
    *,
    opener: Opener | None = None,
    approval_store: ApprovalStore | None = None,
    pending_actions: PendingComputerActionStore | None = None,
    clock: Clock | None = None,
    fallback: ReplyComposer | None = None,
) -> ReplyComposer:
    """Return a composer that executes safe local actions when recognized."""
    effective_opener = opener or _default_opener

    async def compose(text: str) -> str | None:
        action = parse_computer_action(text)
        if action is None:
            return await fallback(text) if fallback is not None else None
        if action.requires_approval:
            if approval_store is None or pending_actions is None:
                return f"I need approval before I can {action.display}."
            approval_id = pending_actions.add(action)
            approval_store.add(
                _approval_for_action(
                    action,
                    approval_id=approval_id,
                    now_ms=(clock or _default_clock)(),
                )
            )
            return f"I need your approval to {action.display}."
        if _run_action(action, opener=effective_opener):
            return _success_message(action)
        return _failure_message(action)

    return compose


def computer_control_streaming_composer(
    *,
    opener: Opener | None = None,
    approval_store: ApprovalStore | None = None,
    pending_actions: PendingComputerActionStore | None = None,
    clock: Clock | None = None,
    fallback: StreamingReplyComposer | None = None,
) -> StreamingReplyComposer:
    """Streaming wrapper variant used when LLM streaming is enabled."""
    effective_opener = opener or _default_opener

    async def compose(text: str) -> AsyncIterator[str]:
        action = parse_computer_action(text)
        if action is not None:
            if action.requires_approval:
                if approval_store is None or pending_actions is None:
                    yield f"I need approval before I can {action.display}."
                    return
                approval_id = pending_actions.add(action)
                approval_store.add(
                    _approval_for_action(
                        action,
                        approval_id=approval_id,
                        now_ms=(clock or _default_clock)(),
                    )
                )
                yield f"I need your approval to {action.display}."
                return
            if _run_action(action, opener=effective_opener):
                yield _success_message(action)
            else:
                yield _failure_message(action)
            return
        if fallback is None:
            return
        async for chunk in fallback(text):
            yield chunk

    return compose


def parse_computer_action(text: str) -> ComputerAction | None:
    stripped = " ".join(text.strip().split())
    if not stripped:
        return None

    for pattern in _LOCK_SCREEN_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction(
                "lock_screen",
                "screen",
                "lock the screen",
                requires_approval=True,
            )

    for pattern in _SLEEP_MAC_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction(
                "sleep_mac",
                "mac",
                "put this Mac to sleep",
                requires_approval=True,
            )

    for pattern in _SCREENSHOT_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction(
                "screenshot",
                _default_screenshot_path(),
                "take a screenshot",
                requires_approval=True,
            )

    for pattern in _MUTE_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction("mute_volume", "muted", "mute volume")

    for pattern in _UNMUTE_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction("unmute_volume", "unmuted", "unmute volume")

    for pattern in _SET_VOLUME_PATTERNS:
        match = pattern.match(stripped)
        if match:
            level = int(match.group("level"))
            if 0 <= level <= 100:
                return ComputerAction("set_volume", str(level), f"set volume to {level}%")
            return None

    for pattern in _SET_CLIPBOARD_PATTERNS:
        match = pattern.match(stripped)
        if match:
            clipboard_text = _clean_clipboard_text(match.group("text"))
            if clipboard_text:
                return ComputerAction(
                    "set_clipboard",
                    clipboard_text,
                    "set the clipboard",
                    requires_approval=True,
                )
            return None

    for pattern in _OPEN_SETTINGS_PANE_PATTERNS:
        match = pattern.match(stripped)
        if match:
            pane = _clean_settings_pane(match.group("pane"))
            target = _SYSTEM_SETTINGS_PANES.get(pane)
            if target:
                return ComputerAction("settings_pane", target, f"{pane} settings")
            return None

    for pattern in _OPEN_URL_PATTERNS:
        match = pattern.match(stripped)
        if match:
            target = _clean_target(match.group("target"))
            if _is_safe_url(target):
                return ComputerAction("url", target, target)
            return None

    for pattern in _OPEN_WEATHER_PATTERNS:
        if pattern.match(stripped):
            return ComputerAction(
                "app",
                "Weather",
                "Weather for your local forecast",
            )

    for pattern in _REVEAL_PATH_PATTERNS:
        match = pattern.match(stripped)
        if match:
            target = _clean_target(match.group("target"))
            path = Path(target).expanduser()
            if path.exists():
                return ComputerAction("reveal_path", str(path), str(path))
            return None

    for pattern in _OPEN_PATH_IN_APP_PATTERNS:
        match = pattern.match(stripped)
        if match:
            target = _clean_target(match.group("target"))
            app = _resolve_app(_clean_target(match.group("name")))
            path = Path(target).expanduser()
            if app and path.exists():
                return ComputerAction(
                    "path_in_app",
                    str(path),
                    f"{path} in {app}",
                    app=app,
                )
            return None

    for pattern in _OPEN_PATH_PATTERNS:
        match = pattern.match(stripped)
        if match:
            target = _clean_target(match.group("target"))
            path = Path(target).expanduser()
            if path.exists():
                return ComputerAction("path", str(path), str(path))
            return None

    for pattern in _SEARCH_PATTERNS:
        match = pattern.match(stripped)
        if match:
            query = _clean_target(match.group("query"))
            if query:
                url = f"https://www.google.com/search?q={quote_plus(query)}"
                return ComputerAction("url", url, f"search: {query}")
            return None

    for pattern in _FOCUS_APP_PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        raw_name = _clean_target(match.group("name"))
        app = _resolve_app(raw_name)
        if app:
            return ComputerAction("focus_app", app, f"focus {app}")

    for pattern in _QUIT_APP_PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        raw_name = _clean_target(match.group("name"))
        app = _resolve_app(raw_name)
        if app:
            return ComputerAction(
                "quit_app",
                app,
                f"quit {app}",
                requires_approval=True,
            )

    for pattern in _OPEN_APP_PATTERNS:
        match = pattern.match(stripped)
        if not match:
            continue
        raw_name = _clean_target(match.group("name"))
        app = _resolve_app(raw_name)
        if app:
            return ComputerAction("app", app, app)

    return None


def _run_action(action: ComputerAction, *, opener: Opener) -> bool:
    if action.kind == "app":
        return opener(["open", "-a", action.target])
    if action.kind == "focus_app":
        return opener(
            ["osascript", "-e", f'tell application "{action.target}" to activate']
        )
    if action.kind in {"url", "path"}:
        return opener(["open", action.target])
    if action.kind == "settings_pane":
        return opener(["open", action.target])
    if action.kind == "path_in_app":
        if not action.app:
            return False
        return opener(["open", "-a", action.app, action.target])
    if action.kind == "reveal_path":
        return opener(["open", "-R", action.target])
    if action.kind == "quit_app":
        return opener(
            ["osascript", "-e", f'tell application "{action.target}" to quit']
        )
    if action.kind == "set_clipboard":
        return opener(
            [
                "osascript",
                "-e",
                f"set the clipboard to {_applescript_string(action.target)}",
            ]
        )
    if action.kind == "lock_screen":
        return opener(
            [
                "osascript",
                "-e",
                'tell application "System Events" to keystroke "q" using {control down, command down}',
            ]
        )
    if action.kind == "sleep_mac":
        return opener(["pmset", "sleepnow"])
    if action.kind == "mute_volume":
        return opener(["osascript", "-e", "set volume output muted true"])
    if action.kind == "unmute_volume":
        return opener(["osascript", "-e", "set volume output muted false"])
    if action.kind == "set_volume":
        return opener(["osascript", "-e", f"set volume output volume {action.target}"])
    if action.kind == "screenshot":
        return opener(["/usr/sbin/screencapture", "-x", action.target])
    return False


class PendingComputerActionStore:
    """In-memory actions waiting for user approval."""

    def __init__(self) -> None:
        self._next = 0
        self._by_approval_id: dict[str, ComputerAction] = {}

    def add(self, action: ComputerAction) -> str:
        self._next += 1
        approval_id = f"computer-control-{self._next}"
        self._by_approval_id[approval_id] = action
        return approval_id

    def pop(self, approval_id: str) -> ComputerAction | None:
        return self._by_approval_id.pop(approval_id, None)

    def get(self, approval_id: str) -> ComputerAction | None:
        return self._by_approval_id.get(approval_id)


def _approval_for_action(
    action: ComputerAction,
    *,
    approval_id: str,
    now_ms: int,
) -> Approval:
    return Approval(
        approval_id=approval_id,
        prompt=f"Allow Deskmate to {action.display}?",
        priority=Priority.P0,
        created_at_ms=now_ms,
        surface_id=f"approval:{approval_id}",
        extras={
            "source": "computer_control",
            "action_kind": action.kind,
            "target": action.target,
            "display": action.display,
            "app": action.app,
        },
    )


async def resolve_pending_computer_action(
    approval: Approval,
    *,
    pending_actions: PendingComputerActionStore,
    opener: Opener | None = None,
) -> str | None:
    action = pending_actions.pop(approval.approval_id)
    if action is None:
        action = _action_from_approval(approval)
    if action is None:
        return None
    if approval.decision is not ApprovalDecision.ALLOW:
        return f"Skipped {action.display}."
    if _run_action(action, opener=opener or _default_opener):
        return _success_message(action)
    return _failure_message(action)


def _action_from_approval(approval: Approval) -> ComputerAction | None:
    extras = approval.extras if isinstance(approval.extras, dict) else {}
    if extras.get("source") != "computer_control":
        return None
    kind = str(extras.get("action_kind") or "").strip()
    target = str(extras.get("target") or "").strip()
    display = str(extras.get("display") or "").strip()
    app_raw = extras.get("app")
    app = str(app_raw).strip() if app_raw is not None else None
    if not kind or not target or not display:
        return None
    if kind not in _RESTORABLE_APPROVAL_ACTION_KINDS:
        return None
    if kind == "set_volume" and not target.isdigit():
        return None
    if kind == "path_in_app" and not app:
        return None
    return ComputerAction(
        kind=kind,
        target=target,
        display=display,
        requires_approval=True,
        app=app,
    )


def _default_opener(args: list[str]) -> bool:
    try:
        result = subprocess.run(  # noqa: S603
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _default_clock() -> int:
    import time

    return int(time.time() * 1000)


def _resolve_app(raw_name: str) -> str:
    normalized = raw_name.strip().lower().removesuffix(".app")
    normalized = normalized.removeprefix("the ")
    normalized = normalized.removeprefix("app ")
    if not normalized:
        return ""
    return _APP_ALIASES.get(normalized, "")


def _is_safe_url(target: str) -> bool:
    parsed = urlparse(target)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _clean_target(value: str) -> str:
    return value.strip().strip("\"'“”‘’")


def _clean_clipboard_text(value: str) -> str:
    stripped = value.strip()
    quote_pairs = {
        '"': '"',
        "'": "'",
        "“": "”",
        "‘": "’",
    }
    if len(stripped) >= 2 and quote_pairs.get(stripped[0]) == stripped[-1]:
        return stripped[1:-1]
    return stripped


def _success_message(action: ComputerAction) -> str:
    if action.kind == "app":
        return f"Opened {action.display}."
    if action.kind == "focus_app":
        return f"Focused {action.target}."
    if action.display.startswith("search: "):
        return f"Searching {action.display.removeprefix('search: ')}."
    if action.kind == "url":
        return f"Opened {action.display}."
    if action.kind == "path":
        return f"Opened {action.display}."
    if action.kind == "settings_pane":
        return f"Opened {action.display}."
    if action.kind == "path_in_app":
        return f"Opened {action.target} in {action.app}."
    if action.kind == "reveal_path":
        return f"Revealed {action.target} in Finder."
    if action.kind == "quit_app":
        return f"Quit {action.target}."
    if action.kind == "set_clipboard":
        return "Updated the clipboard."
    if action.kind == "lock_screen":
        return "Locked the screen."
    if action.kind == "sleep_mac":
        return "Put this Mac to sleep."
    if action.kind == "mute_volume":
        return "Muted volume."
    if action.kind == "unmute_volume":
        return "Unmuted volume."
    if action.kind == "set_volume":
        return f"Set volume to {action.target}%."
    if action.kind == "screenshot":
        return f"Saved screenshot to {action.target}."
    return "Done."


def _failure_message(action: ComputerAction) -> str:
    if action.kind == "focus_app":
        return f"I couldn't focus {action.target}."
    if action.kind == "path_in_app":
        return f"I couldn't open {action.target} in {action.app}."
    if action.kind == "reveal_path":
        return f"I couldn't reveal {action.target} in Finder."
    if action.kind == "quit_app":
        return f"I couldn't quit {action.target}."
    if action.kind == "set_clipboard":
        return "I couldn't update the clipboard."
    if action.kind == "lock_screen":
        return "I couldn't lock the screen."
    if action.kind == "sleep_mac":
        return "I couldn't put this Mac to sleep."
    if action.kind == "mute_volume":
        return "I couldn't mute volume."
    if action.kind == "unmute_volume":
        return "I couldn't unmute volume."
    if action.kind == "set_volume":
        return f"I couldn't set volume to {action.target}%."
    if action.kind == "screenshot":
        return "I couldn't take a screenshot."
    return f"I couldn't open {action.display}."


def _clean_settings_pane(value: str) -> str:
    return (
        _clean_target(value)
        .lower()
        .replace("system ", "")
        .replace("系统", "")
        .replace("设置", "")
        .strip()
    )


def _applescript_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _default_screenshot_path() -> str:
    return str(
        Path.home()
        / "Desktop"
        / f"deskmate-screenshot-{int(time.time() * 1000)}.png"
    )


__all__ = [
    "ComputerAction",
    "PendingComputerActionStore",
    "computer_control_composer",
    "computer_control_streaming_composer",
    "parse_computer_action",
    "resolve_pending_computer_action",
]
