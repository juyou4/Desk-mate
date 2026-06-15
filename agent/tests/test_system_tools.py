"""Allowlisted system-tool tests."""

from __future__ import annotations

import json

import httpx
import pytest

from deskmate_agent.skills import (
    CalendarEventRequest,
    create_calendar_event,
    get_weather,
    list_system_tools,
)
from deskmate_agent.skills.llm_chat import openai_compat_composer
from deskmate_agent.skills.tool_calls import DESKMATE_TOOLS, DeskmateToolExecutor


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://mock-ignored",
    )


def test_create_calendar_event_builds_fixed_applescript_command() -> None:
    calls: list[list[str]] = []

    def runner(args: list[str]) -> tuple[int, str, str]:
        calls.append(args)
        return 0, "", ""

    result = create_calendar_event(
        CalendarEventRequest(
            title='Project sync "Desk"',
            start_at="2026-06-16 09:30",
            duration_minutes=45,
            notes="Bring island notes",
            location="Office",
        ),
        runner=runner,
    )

    assert result.status == "created"
    assert result.message == "Calendar event created: Project sync \"Desk\" at 2026-06-16 09:30."
    assert calls[0][0:2] == ["osascript", "-e"]
    script = calls[0][2]
    assert 'tell application "Calendar"' in script
    assert 'summary:"Project sync \\"Desk\\""' in script
    assert 'date "Tuesday, June 16, 2026 at 09:30:00 AM"' in script
    assert 'description:"Bring island notes"' in script
    assert 'location:"Office"' in script


def test_create_calendar_event_reports_permission_error() -> None:
    result = create_calendar_event(
        CalendarEventRequest(
            title="Standup",
            start_at="2026-06-16T09:30:00",
            duration_minutes=30,
        ),
        runner=lambda _args: (1, "", "Not authorized to send Apple events to Calendar"),
    )

    assert result.status == "approval_required"
    assert "permission" in result.message.lower()


def test_create_calendar_event_rejects_missing_duration_or_end() -> None:
    result = create_calendar_event(
        CalendarEventRequest(title="No end", start_at="2026-06-16T09:30:00")
    )

    assert result.status == "failed"
    assert "duration_minutes" in result.message


@pytest.mark.asyncio
async def test_get_weather_uses_cli_friendly_endpoint() -> None:
    seen: list[str] = []

    async_result = await get_weather(
        location="Shanghai",
        fetcher=lambda url: seen.append(url) or "Shanghai: 🌦 +28°C",
    )

    assert async_result == "Weather: Shanghai: 🌦 +28°C"
    assert seen == ["https://wttr.in/Shanghai?format=3"]


def test_list_system_tools_exposes_basic_operations() -> None:
    listing = list_system_tools()

    assert "deskmate_create_calendar_event" in listing
    assert "deskmate_get_weather" in listing
    assert "deskmate_computer_action" in listing


@pytest.mark.asyncio
async def test_tool_executor_runs_calendar_weather_and_discovery_tools() -> None:
    calls: list[list[str]] = []
    executor = DeskmateToolExecutor(
        calendar_runner=lambda args: calls.append(args) or (0, "", ""),
        weather_fetcher=lambda url: f"{url} Sunny 25C",
    )

    calendar = await executor.execute(
        "deskmate_create_calendar_event",
        {
            "title": "Demo",
            "start_at": "2026-06-16 10:00",
            "duration_minutes": 30,
        },
    )
    weather = await executor.execute("deskmate_get_weather", {"location": "Paris"})
    tools = await executor.execute("deskmate_list_system_tools", {})

    assert calendar == "Calendar event created: Demo at 2026-06-16 10:00."
    assert calls and calls[0][0:2] == ["osascript", "-e"]
    assert weather == "Weather: https://wttr.in/Paris?format=3 Sunny 25C"
    assert "deskmate_create_calendar_event" in tools


def test_tool_schema_includes_system_tools() -> None:
    names = {item["function"]["name"] for item in DESKMATE_TOOLS}

    assert "deskmate_create_calendar_event" in names
    assert "deskmate_get_weather" in names
    assert "deskmate_list_system_tools" in names


@pytest.mark.asyncio
async def test_calendar_tool_requires_explicit_user_intent_through_llm_path() -> None:
    calls: list[list[str]] = []
    seen_tool_result = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_tool_result
        body = json.loads(request.content)
        tool_messages = [m for m in body["messages"] if m.get("role") == "tool"]
        if tool_messages:
            seen_tool_result = tool_messages[-1]["content"]
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-calendar",
                                    "type": "function",
                                    "function": {
                                        "name": "deskmate_create_calendar_event",
                                        "arguments": json.dumps(
                                            {
                                                "title": "Team sync",
                                                "start_at": "2026-06-16 10:00",
                                                "duration_minutes": 30,
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
        tool_executor=DeskmateToolExecutor(
            calendar_runner=lambda args: calls.append(args) or (0, "", "")
        ),
    )

    assert await compose("We have a team sync tomorrow.") == "ok"
    assert calls == []
    assert "requires an explicit user request" in seen_tool_result


@pytest.mark.asyncio
async def test_calendar_tool_runs_with_explicit_user_intent_through_llm_path() -> None:
    calls: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if any(m.get("role") == "tool" for m in body["messages"]):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-calendar-ok",
                                    "type": "function",
                                    "function": {
                                        "name": "deskmate_create_calendar_event",
                                        "arguments": json.dumps(
                                            {
                                                "title": "Team sync",
                                                "start_at": "2026-06-16 10:00",
                                                "duration_minutes": 30,
                                            }
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    compose = openai_compat_composer(
        base_url="https://api.test/v1",
        api_key="sk",
        model="m",
        client=_client(handler),
        tool_executor=DeskmateToolExecutor(
            calendar_runner=lambda args: calls.append(args) or (0, "", "")
        ),
    )

    assert await compose("Add Team sync to calendar tomorrow at 10.") == "done"
    assert calls and calls[0][0:2] == ["osascript", "-e"]
