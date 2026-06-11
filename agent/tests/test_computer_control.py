"""Safe computer-control composer tests."""

from __future__ import annotations

import pytest

from deskmate_agent.approvals import Approval, ApprovalDecision, ApprovalStore
from deskmate_agent.skills import (
    PendingComputerActionStore,
    computer_control_composer,
    computer_control_streaming_composer,
    parse_computer_action,
    resolve_pending_computer_action,
)


def test_parse_open_known_app() -> None:
    action = parse_computer_action("open Terminal")

    assert action is not None
    assert action.kind == "app"
    assert action.target == "Terminal"


def test_parse_open_weather_app() -> None:
    english = parse_computer_action("what's the weather?")
    chinese = parse_computer_action("帮我看一下天气")

    assert english is not None
    assert english.kind == "app"
    assert english.target == "Weather"
    assert chinese is not None
    assert chinese.kind == "app"
    assert chinese.target == "Weather"


def test_parse_open_url() -> None:
    action = parse_computer_action("open https://example.com/docs")

    assert action is not None
    assert action.kind == "url"
    assert action.target == "https://example.com/docs"


def test_parse_search_query() -> None:
    action = parse_computer_action("search for deskmate agent")

    assert action is not None
    assert action.kind == "url"
    assert action.display == "search: deskmate agent"
    assert action.target == "https://www.google.com/search?q=deskmate+agent"


def test_parse_focus_known_app() -> None:
    action = parse_computer_action("switch to Cursor")

    assert action is not None
    assert action.kind == "focus_app"
    assert action.target == "Cursor"
    assert action.display == "focus Cursor"


def test_parse_open_path_in_known_app(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    action = parse_computer_action(f"open {project} in Cursor")

    assert action is not None
    assert action.kind == "path_in_app"
    assert action.target == str(project)
    assert action.app == "Cursor"


def test_parse_reveal_path_in_finder(tmp_path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hi", encoding="utf-8")

    action = parse_computer_action(f"reveal {file_path} in Finder")

    assert action is not None
    assert action.kind == "reveal_path"
    assert action.target == str(file_path)


def test_parse_open_settings_pane() -> None:
    action = parse_computer_action("open privacy settings")

    assert action is not None
    assert action.kind == "settings_pane"
    assert action.target.startswith("x-apple.systempreferences:")
    assert action.display == "privacy settings"


def test_parse_set_clipboard_requires_approval() -> None:
    action = parse_computer_action('copy hello "Deskmate"')

    assert action is not None
    assert action.kind == "set_clipboard"
    assert action.target == 'hello "Deskmate"'
    assert action.display == "set the clipboard"
    assert action.requires_approval


def test_parse_lock_and_sleep_require_approval() -> None:
    lock = parse_computer_action("lock screen")
    sleep = parse_computer_action("put this mac to sleep")

    assert lock is not None
    assert lock.kind == "lock_screen"
    assert lock.requires_approval
    assert sleep is not None
    assert sleep.kind == "sleep_mac"
    assert sleep.requires_approval


def test_parse_volume_actions() -> None:
    mute = parse_computer_action("mute")
    unmute = parse_computer_action("unmute")
    volume = parse_computer_action("set volume to 35%")

    assert mute is not None
    assert mute.kind == "mute_volume"
    assert not mute.requires_approval
    assert unmute is not None
    assert unmute.kind == "unmute_volume"
    assert volume is not None
    assert volume.kind == "set_volume"
    assert volume.target == "35"


def test_parse_screenshot_requires_approval() -> None:
    action = parse_computer_action("take screenshot")

    assert action is not None
    assert action.kind == "screenshot"
    assert action.target.endswith(".png")
    assert "deskmate-screenshot-" in action.target
    assert action.display == "take a screenshot"
    assert action.requires_approval


def test_parse_rejects_unknown_app_and_unsafe_url() -> None:
    assert parse_computer_action("open made up app") is None
    assert parse_computer_action("open javascript:alert(1)") is None
    assert parse_computer_action("open made up settings") is None
    assert parse_computer_action("set volume to 101") is None


def test_parse_quit_known_app_requires_approval() -> None:
    action = parse_computer_action("quit Terminal")

    assert action is not None
    assert action.kind == "quit_app"
    assert action.target == "Terminal"
    assert action.display == "quit Terminal"
    assert action.requires_approval


@pytest.mark.asyncio
async def test_computer_control_composer_runs_open_app() -> None:
    calls: list[list[str]] = []
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    reply = await compose("打开 terminal")

    assert reply == "Opened Terminal."
    assert calls == [["open", "-a", "Terminal"]]


@pytest.mark.asyncio
async def test_computer_control_composer_runs_focus_app() -> None:
    calls: list[list[str]] = []
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    reply = await compose("focus Cursor")

    assert reply == "Focused Cursor."
    assert calls == [
        ["osascript", "-e", 'tell application "Cursor" to activate']
    ]


@pytest.mark.asyncio
async def test_computer_control_composer_runs_open_path_in_app(tmp_path) -> None:
    calls: list[list[str]] = []
    project = tmp_path / "project"
    project.mkdir()
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    reply = await compose(f"open {project} in Windsurf")

    assert reply == f"Opened {project} in Windsurf."
    assert calls == [["open", "-a", "Windsurf", str(project)]]


@pytest.mark.asyncio
async def test_computer_control_composer_runs_reveal_path(tmp_path) -> None:
    calls: list[list[str]] = []
    file_path = tmp_path / "notes.txt"
    file_path.write_text("hi", encoding="utf-8")
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    reply = await compose(f"show {file_path} in Finder")

    assert reply == f"Revealed {file_path} in Finder."
    assert calls == [["open", "-R", str(file_path)]]


@pytest.mark.asyncio
async def test_computer_control_composer_runs_open_settings_pane() -> None:
    calls: list[list[str]] = []
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    reply = await compose("open bluetooth settings")

    assert reply == "Opened bluetooth settings."
    assert calls == [["open", "x-apple.systempreferences:com.apple.BluetoothSettings"]]


@pytest.mark.asyncio
async def test_computer_control_composer_runs_volume_actions() -> None:
    calls: list[list[str]] = []
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
    )

    assert await compose("mute") == "Muted volume."
    assert await compose("unmute") == "Unmuted volume."
    assert await compose("set volume to 35") == "Set volume to 35%."
    assert calls == [
        ["osascript", "-e", "set volume output muted true"],
        ["osascript", "-e", "set volume output muted false"],
        ["osascript", "-e", "set volume output volume 35"],
    ]


@pytest.mark.asyncio
async def test_computer_control_composer_creates_approval_for_quit() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
        approval_store=approvals,
        pending_actions=pending,
        clock=lambda: 42,
    )

    reply = await compose("close Terminal")

    assert reply == "I need your approval to quit Terminal."
    assert calls == []
    approval = approvals.get("computer-control-1")
    assert approval is not None
    assert approval.prompt == "Allow Deskmate to quit Terminal?"
    assert approval.created_at_ms == 42
    assert approval.extras["source"] == "computer_control"
    assert pending.get("computer-control-1") is not None


@pytest.mark.asyncio
async def test_computer_control_composer_creates_approval_for_screenshot() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
        approval_store=approvals,
        pending_actions=pending,
        clock=lambda: 42,
    )

    reply = await compose("screenshot")

    assert reply == "I need your approval to take a screenshot."
    assert calls == []
    approval = approvals.get("computer-control-1")
    assert approval is not None
    assert approval.prompt == "Allow Deskmate to take a screenshot?"
    assert approval.extras["action_kind"] == "screenshot"
    assert pending.get("computer-control-1") is not None


@pytest.mark.asyncio
async def test_computer_control_composer_creates_approval_for_clipboard() -> None:
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
        clock=lambda: 42,
    )

    reply = await compose('copy hello "Deskmate"')

    assert reply == "I need your approval to set the clipboard."
    approval = approvals.get("computer-control-1")
    assert approval is not None
    assert approval.prompt == "Allow Deskmate to set the clipboard?"
    assert approval.extras["action_kind"] == "set_clipboard"
    assert pending.get("computer-control-1") is not None


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_runs_only_after_allow() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
    )
    await compose("quit Terminal")
    approval = approvals.resolve("computer-control-1", ApprovalDecision.ALLOW, 100)

    assert approval is not None
    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )

    assert reply == "Quit Terminal."
    assert calls == [
        ["osascript", "-e", 'tell application "Terminal" to quit']
    ]
    assert pending.get("computer-control-1") is None


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_restores_from_approval_extras() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    original_pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=original_pending,
    )
    await compose("quit Terminal")
    approval = approvals.resolve("computer-control-1", ApprovalDecision.ALLOW, 100)

    assert approval is not None
    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=PendingComputerActionStore(),
        opener=lambda args: calls.append(args) is None,
    )

    assert reply == "Quit Terminal."
    assert calls == [
        ["osascript", "-e", 'tell application "Terminal" to quit']
    ]


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_does_not_restore_unknown_kind() -> None:
    calls: list[list[str]] = []
    approval = Approval(
        approval_id="computer-control-missing",
        prompt="Allow bad action?",
        created_at_ms=1,
        decision=ApprovalDecision.ALLOW,
        extras={
            "source": "computer_control",
            "action_kind": "run_shell",
            "target": "rm -rf /",
            "display": "run shell",
        },
    )

    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=PendingComputerActionStore(),
        opener=lambda args: calls.append(args) is None,
    )

    assert reply is None
    assert calls == []


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_sets_clipboard_after_allow() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
    )
    await compose('copy hello "Deskmate"')
    approval = approvals.resolve("computer-control-1", ApprovalDecision.ALLOW, 100)

    assert approval is not None
    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )

    assert reply == "Updated the clipboard."
    assert calls == [
        ["osascript", "-e", 'set the clipboard to "hello \\"Deskmate\\""']
    ]


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_screenshot_after_allow() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
    )
    await compose("screenshot")
    approval = approvals.resolve("computer-control-1", ApprovalDecision.ALLOW, 100)

    assert approval is not None
    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )

    assert reply is not None
    assert reply.startswith("Saved screenshot to ")
    assert calls[0][0:2] == ["/usr/sbin/screencapture", "-x"]
    assert calls[0][2].endswith(".png")
    assert "deskmate-screenshot-" in calls[0][2]


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_locks_and_sleeps_after_allow() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
    )

    await compose("lock screen")
    lock = approvals.resolve("computer-control-1", ApprovalDecision.ALLOW, 100)
    await compose("sleep mac")
    sleep = approvals.resolve("computer-control-2", ApprovalDecision.ALLOW, 200)

    assert lock is not None
    assert sleep is not None
    lock_reply = await resolve_pending_computer_action(
        lock,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )
    sleep_reply = await resolve_pending_computer_action(
        sleep,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )

    assert lock_reply == "Locked the screen."
    assert sleep_reply == "Put this Mac to sleep."
    assert calls == [
        [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "q" using {control down, command down}',
        ],
        ["pmset", "sleepnow"],
    ]


@pytest.mark.asyncio
async def test_resolve_pending_computer_action_skips_after_deny() -> None:
    calls: list[list[str]] = []
    approvals = ApprovalStore()
    pending = PendingComputerActionStore()
    compose = computer_control_composer(
        approval_store=approvals,
        pending_actions=pending,
    )
    await compose("quit Terminal")
    approval = approvals.resolve("computer-control-1", ApprovalDecision.DENY, 100)

    assert approval is not None
    reply = await resolve_pending_computer_action(
        approval,
        pending_actions=pending,
        opener=lambda args: calls.append(args) is None,
    )

    assert reply == "Skipped quit Terminal."
    assert calls == []
    assert pending.get("computer-control-1") is None


@pytest.mark.asyncio
async def test_computer_control_composer_falls_back_for_chat() -> None:
    async def fallback(text: str) -> str:
        return f"chat:{text}"

    calls: list[list[str]] = []
    compose = computer_control_composer(
        opener=lambda args: calls.append(args) is None,
        fallback=fallback,
    )

    reply = await compose("how are you?")

    assert reply == "chat:how are you?"
    assert calls == []


@pytest.mark.asyncio
async def test_computer_control_streaming_composer_runs_before_fallback() -> None:
    calls: list[list[str]] = []

    async def fallback(text: str):
        yield f"chat:{text}"

    compose = computer_control_streaming_composer(
        opener=lambda args: calls.append(args) is None,
        fallback=fallback,
    )

    chunks = [chunk async for chunk in compose("open https://example.com")]

    assert chunks == ["Opened https://example.com."]
    assert calls == [["open", "https://example.com"]]


@pytest.mark.asyncio
async def test_computer_control_streaming_composer_falls_back_for_chat() -> None:
    async def fallback(text: str):
        yield "hello "
        yield text

    compose = computer_control_streaming_composer(fallback=fallback)

    chunks = [chunk async for chunk in compose("hi")]

    assert chunks == ["hello ", "hi"]
