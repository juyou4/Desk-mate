"""Allowlisted system tools for everyday desktop-agent operations.

The functions in this module are intentionally narrow: they expose useful
macOS operations without becoming a generic shell runner.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx

CommandRunner = Callable[[list[str]], tuple[int, str, str]]
WeatherFetcher = Callable[[str], str]

CalendarEventResultStatus = Literal["created", "approval_required", "failed"]

_TOOL_NAMES: tuple[str, ...] = (
    "deskmate_schedule_reminder",
    "deskmate_list_reminders",
    "deskmate_cancel_reminder",
    "deskmate_create_calendar_event",
    "deskmate_get_weather",
    "deskmate_list_system_tools",
    "deskmate_computer_action",
)


@dataclass(frozen=True)
class CalendarEventRequest:
    title: str
    start_at: str
    end_at: str | None = None
    duration_minutes: int | None = None
    calendar: str = "Calendar"
    notes: str = ""
    location: str = ""


@dataclass(frozen=True)
class CalendarEventResult:
    status: CalendarEventResultStatus
    message: str


def list_system_tools() -> str:
    """Return the stable high-level tools available to the LLM/MCP bridge."""
    return "Available Deskmate system tools:\n" + "\n".join(
        f"- {name}" for name in _TOOL_NAMES
    )


def create_calendar_event(
    request: CalendarEventRequest,
    *,
    runner: CommandRunner | None = None,
) -> CalendarEventResult:
    """Create a macOS Calendar event via a fixed AppleScript command."""
    title = _clean(request.title)
    if not title:
        return CalendarEventResult("failed", "Tool error: title is required.")
    start = _parse_datetime(request.start_at)
    if start is None:
        return CalendarEventResult(
            "failed",
            "Tool error: start_at must be an ISO-like datetime.",
        )
    end = _resolve_end(start, request.end_at, request.duration_minutes)
    if end is None:
        return CalendarEventResult(
            "failed",
            "Tool error: provide end_at or duration_minutes >= 1.",
        )
    if end <= start:
        return CalendarEventResult("failed", "Tool error: event end must be after start.")

    script = _calendar_event_script(
        title=title,
        start=start,
        end=end,
        calendar=_clean(request.calendar) or "Calendar",
        notes=_clean(request.notes),
        location=_clean(request.location),
    )
    code, _stdout, stderr = (runner or _run_command)(
        ["osascript", "-e", script]
    )
    if code == 0:
        when = start.strftime("%Y-%m-%d %H:%M")
        return CalendarEventResult("created", f"Calendar event created: {title} at {when}.")
    lowered = stderr.lower()
    if "not authorized" in lowered or "not permitted" in lowered or "privacy" in lowered:
        return CalendarEventResult(
            "approval_required",
            "Calendar permission is required before Deskmate can create events.",
        )
    detail = _clean(stderr) or "Calendar did not accept the event."
    return CalendarEventResult("failed", f"Tool error: {detail}")


async def get_weather(
    *,
    location: str = "",
    fetcher: WeatherFetcher | None = None,
) -> str:
    """Read a compact weather report from a CLI-friendly HTTP endpoint."""
    cleaned = _clean(location)
    query = urllib.parse.quote(cleaned or "")
    url = f"https://wttr.in/{query}?format=3"
    try:
        text = (fetcher or _fetch_weather)(url)
    except (OSError, httpx.HTTPError) as exc:
        return f"Tool error: weather lookup failed: {exc}"
    report = " ".join(text.strip().split())
    if not report:
        return "Tool error: weather lookup returned no data."
    if len(report) > 240:
        report = report[:237].rstrip() + "..."
    return f"Weather: {report}"


def _calendar_event_script(
    *,
    title: str,
    start: datetime,
    end: datetime,
    calendar: str,
    notes: str,
    location: str,
) -> str:
    props = [
        f"summary:{_applescript_string(title)}",
        f"start date:{_applescript_date(start)}",
        f"end date:{_applescript_date(end)}",
    ]
    if notes:
        props.append(f"description:{_applescript_string(notes)}")
    if location:
        props.append(f"location:{_applescript_string(location)}")
    return (
        'tell application "Calendar"\n'
        "activate\n"
        f"tell calendar {_applescript_string(calendar)}\n"
        f"make new event with properties {{{', '.join(props)}}}\n"
        "end tell\n"
        "end tell"
    )


def _parse_datetime(value: str) -> datetime | None:
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    return parsed


def _resolve_end(
    start: datetime,
    end_at: str | None,
    duration_minutes: int | None,
) -> datetime | None:
    if end_at:
        return _parse_datetime(end_at)
    if isinstance(duration_minutes, int) and duration_minutes >= 1:
        from datetime import timedelta

        return start + timedelta(minutes=duration_minutes)
    return None


def _applescript_date(value: datetime) -> str:
    payload = value.strftime("%A, %B %d, %Y at %I:%M:%S %p")
    return f"date {_applescript_string(payload)}"


def _applescript_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _run_command(args: list[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(  # noqa: S603
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout, result.stderr


def _fetch_weather(url: str) -> str:
    with httpx.Client(timeout=5.0, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.text


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


__all__ = [
    "CalendarEventRequest",
    "CalendarEventResult",
    "create_calendar_event",
    "get_weather",
    "list_system_tools",
]
